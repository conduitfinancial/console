"""OIDC authorization-code login: discovery, token exchange, ID-token checks.

No authlib, no joserfc. RS256 verification is ~30 lines against
`cryptography`, which is already a dependency (Fernet), and discovery/token
exchange are two httpx calls. Ceiling: **RS256 only** — an issuer signing with
ES256/EdDSA, or returning an encrypted (JWE) ID token, is refused with a clear
error; add joserfc and swap the signature check in `verify` if one ever has to
be supported.

The classic JWT footguns are closed explicitly: `alg` is checked against a
one-item allowlist before anything else (so `none` and HMAC-with-the-public-key
are impossible), the key is looked up by `kid` in the issuer's JWKS (never taken
from the token), and iss/aud/exp/nonce are all mandatory.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.auth.tokens import b64d, b64e
from app.config import Settings, https_or_loopback

ALGORITHM = "RS256"
CLOCK_SKEW_SECONDS = 60


def challenge(verifier: str) -> str:
    """PKCE S256 (RFC 7636): a code stolen at the callback is useless without the
    verifier, which never leaves this app."""
    return b64e(hashlib.sha256(verifier.encode("ascii")).digest())


class OIDCError(Exception):
    """Login failed. The message is for the audit trail and the log, not the
    browser — it can name claims, so it never reaches a rendered page."""


@dataclass(frozen=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


class OIDCClient:
    """One per process. `transport`/`now` exist for tests (stubbed issuer)."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now=time.time,
    ) -> None:
        self.settings = settings
        self._now = now
        self._http = httpx.AsyncClient(transport=transport, timeout=settings.op_client_timeout)
        self._discovery: Discovery | None = None
        self._keys: dict[str, dict] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def discover(self) -> Discovery:
        if self._discovery is None:
            issuer = self.settings.oidc_issuer.rstrip("/")
            document = await self._json(f"{issuer}/.well-known/openid-configuration")
            # Issuer mix-up defense: the document must claim the issuer we asked for.
            if str(document.get("issuer", "")).rstrip("/") != issuer:
                raise OIDCError("discovery document issuer does not match OIDC_ISSUER")
            try:
                discovered = Discovery(
                    issuer=issuer,
                    authorization_endpoint=document["authorization_endpoint"],
                    token_endpoint=document["token_endpoint"],
                    jwks_uri=document["jwks_uri"],
                )
            except KeyError as exc:
                raise OIDCError(f"discovery document is missing {exc.args[0]}") from exc
            # An https issuer that then points at http endpoints is a downgrade:
            # the token exchange would carry the client secret, the code and the
            # PKCE verifier in the clear.
            for name, url in (
                ("authorization_endpoint", discovered.authorization_endpoint),
                ("token_endpoint", discovered.token_endpoint),
                ("jwks_uri", discovered.jwks_uri),
            ):
                if not https_or_loopback(str(url)):
                    raise OIDCError(f"discovery document {name} is not https")
            self._discovery = discovered
        return self._discovery

    async def authorization_url(
        self, *, state: str, nonce: str, redirect_uri: str, verifier: str
    ) -> str:
        endpoint = (await self.discover()).authorization_endpoint
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.oidc_client_id,
                "redirect_uri": redirect_uri,
                "scope": self.settings.oidc_scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    async def exchange(self, *, code: str, redirect_uri: str, verifier: str) -> str:
        """Authorization code → raw ID token. client_secret_post: every IdP
        accepts it, and it needs no extra header assembly."""
        endpoint = (await self.discover()).token_endpoint
        try:
            response = await self._http.post(
                endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self.settings.oidc_client_id,
                    "client_secret": self.settings.oidc_client_secret.get_secret_value(),
                    "code_verifier": verifier,
                },
            )
        except httpx.HTTPError as exc:
            raise OIDCError(f"token endpoint: {type(exc).__name__}") from exc
        if response.status_code != 200:
            # No body echo: token endpoints repeat the request, secret included.
            raise OIDCError(f"token endpoint returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise OIDCError("token endpoint response was not JSON") from exc
        token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise OIDCError("token response carried no id_token")
        return token

    async def verify(self, id_token: str, *, nonce: str) -> dict:
        """Verified claims, or OIDCError. Never returns unverified claims."""
        parts = id_token.split(".")
        if len(parts) != 3:
            raise OIDCError("id_token is not a signed JWT")
        header_b64, payload_b64, signature_b64 = parts
        try:
            header = json.loads(b64d(header_b64))
            claims = json.loads(b64d(payload_b64))
            signature = b64d(signature_b64)
        except Exception as exc:  # noqa: BLE001
            raise OIDCError("id_token is malformed") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise OIDCError("id_token is malformed")
        if header.get("alg") != ALGORITHM:
            raise OIDCError(f"unsupported id_token alg {header.get('alg')!r}")

        key = await self._public_key(header.get("kid"))
        try:
            key.verify(
                signature,
                f"{header_b64}.{payload_b64}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise OIDCError("id_token signature is invalid") from exc

        issuer = (await self.discover()).issuer
        if str(claims.get("iss", "")).rstrip("/") != issuer:
            raise OIDCError("id_token iss mismatch")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        client_id = self.settings.oidc_client_id
        if client_id not in audiences:
            raise OIDCError("id_token aud mismatch")
        # `azp` names the party the token was actually issued *to*. A permissive
        # issuer can mint a multi-audience token for another client that still
        # lists us in `aud`; without this check that token logs its holder in
        # here.
        authorized_party = claims.get("azp")
        if (authorized_party is not None or len(audiences) > 1) and authorized_party != client_id:
            raise OIDCError("id_token azp is not this client")
        # Identity has to come from the immutable subject, never from a mutable
        # email that an IdP admin can move between accounts.
        if not str(claims.get("sub") or "").strip():
            raise OIDCError("id_token carries no sub")
        try:
            expires = float(claims["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OIDCError("id_token has no usable exp") from exc
        if expires + CLOCK_SKEW_SECONDS <= self._now():
            raise OIDCError("id_token is expired")
        if not nonce or claims.get("nonce") != nonce:
            raise OIDCError("id_token nonce mismatch")
        return claims

    async def _public_key(self, kid: object) -> rsa.RSAPublicKey:
        if not isinstance(kid, str) or not kid:
            raise OIDCError("id_token has no kid")
        jwk = self._keys.get(kid)
        if jwk is None:  # unknown kid: the issuer may have rotated keys
            await self._load_keys()
            jwk = self._keys.get(kid)
        if jwk is None:
            raise OIDCError("id_token was signed by an unknown key")
        if jwk.get("kty") != "RSA" or jwk.get("use") == "enc":
            raise OIDCError("id_token key is not an RSA signing key")
        try:
            numbers = rsa.RSAPublicNumbers(
                e=int.from_bytes(b64d(jwk["e"]), "big"),
                n=int.from_bytes(b64d(jwk["n"]), "big"),
            )
            return numbers.public_key()
        except Exception as exc:  # noqa: BLE001
            raise OIDCError("issuer published an unusable JWKS key") from exc

    async def _load_keys(self) -> None:
        document = await self._json((await self.discover()).jwks_uri)
        self._keys = {
            k["kid"]: k
            for k in document.get("keys", [])
            if isinstance(k, dict) and isinstance(k.get("kid"), str)
        }

    async def _json(self, url: str) -> dict:
        """Every failure mode here is an OIDCError, never a bare exception: an
        issuer serving an HTML error page must produce an audited sign-in
        failure, not an unaudited 500."""
        where = url.rsplit("/", 1)[-1]
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            raise OIDCError(f"{where}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise OIDCError(f"{where}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise OIDCError(f"{where}: response was not JSON") from exc
        if not isinstance(body, dict):
            raise OIDCError(f"{where}: not a JSON object")
        return body
