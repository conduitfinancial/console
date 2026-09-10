"""The `?msg=`/`?err=` flash banner carries a `msgsig`/`errsig`
companion that `redirect()` signs (`app/web/__init__.py`) and that `base.html`
must both unseal AND match against the plaintext AND the current page before
rendering — so a crafted link cannot forge the green/red banner a
server-issued redirect earns, and a genuine flash cannot be copy-pasted from
the page it was minted for onto another one.

The plaintext param stays plaintext (the address bar's own honesty,
`redirect()`'s docstring); the signature is what makes it trustworthy.
Everything else — a bare query param with no signature, a tampered signature,
an expired one, a valid signature stolen from different text, or a valid
signature replayed on a different page — must render no banner at all, never
an empty one.
"""

from __future__ import annotations

import html as html_module
from urllib.parse import quote, quote_plus, urlencode

from starlette.requests import Request

from app.auth import tokens
from app.web import redirect
from tests.payments_fixtures import CID, page
from tests.web_harness import make_app, post, signed_in, stub

PHISH = "All payouts approved and sent"


def no_banner(body: str) -> bool:
    """No flash div at all — not the phishing sentence, and not an empty
    `class="flash ..."` shell either."""
    return PHISH not in body and 'class="flash' not in body


def bare_request() -> Request:
    """A `Request` with nothing this test cares about — `redirect()` only
    reads `hx-request` off it, and a plain browser navigation (a 303, not a
    204) is exactly what these tests want to inspect the `Location` of."""
    return Request({"type": "http", "method": "GET", "headers": []})


async def test_a_bare_msg_query_param_renders_no_banner():
    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get(f"/drafts?msg={quote(PHISH)}")

    assert response.status_code == 200
    assert no_banner(response.text), response.text


async def test_a_server_issued_redirect_still_renders_its_flash():
    app = make_app(stub({("GET", f"/v2/customers/{CID}/whitelist-recipients"): page([])}))
    async with signed_in(app) as web:
        response = await post(web, f"/customers/{CID}/contacts/not-a-real-id/archive")
        assert response.status_code == 204, response.text
        location = response.headers["hx-redirect"]
        assert "err=" in location and "errsig=" in location, location
        rendered = await web.get(location)

    assert rendered.status_code == 200
    assert "No such contact for this customer." in rendered.text
    assert 'class="flash err"' in rendered.text


async def test_a_signature_from_the_wrong_secret_renders_no_banner():
    """The real attacker model: someone without this app's session secret,
    not someone who happens to flip a byte in a genuine one. Forging by
    mutating bytes turned out flaky — base64's final character (and others)
    carry redundant bits, so a single flipped character sometimes decodes to
    the *same* signature bytes and stays valid (measured directly: ~6% of the
    time for the last character of a real token). Signing with a different
    secret has no such coin flip: it is unambiguously invalid, every time."""
    app = make_app(stub({}))
    async with signed_in(app) as web:
        forged = tokens.sign(
            {"text": PHISH, "path": "/drafts"}, secret="not-the-app-session-secret", ttl=300
        )
        # The precondition this class of flake needs: if a "forged" token ever
        # turns out to still unseal, the test below would pass for the wrong
        # reason (no banner because nothing was requested to render one, not
        # because the forgery was caught) — fail loudly here instead.
        assert tokens.unseal(forged) is None, "the forged token is still valid; this test would prove nothing"
        response = await web.get(f"/drafts?msg={quote(PHISH)}&msgsig={quote(forged)}")

    assert response.status_code == 200
    assert no_banner(response.text), response.text


async def test_a_byte_tampered_signature_renders_no_banner():
    """Extra coverage alongside the wrong-secret case above: a single mutated
    character within the signature segment. Deliberately the middle of it,
    not the last character — that one's redundant bits are exactly what made
    the original version of this test ~6% flaky."""
    app = make_app(stub({}))
    async with signed_in(app) as web:
        sig = tokens.seal({"text": PHISH, "path": "/drafts"}, ttl=300)
        body, signature = sig.split(".")
        mid = len(signature) // 2
        tampered = "a" if signature[mid] != "a" else "b"
        forged = f"{body}.{signature[:mid]}{tampered}{signature[mid + 1:]}"
        assert tokens.unseal(forged) is None, "the forged token is still valid; this test would prove nothing"
        response = await web.get(f"/drafts?msg={quote(PHISH)}&msgsig={quote(forged)}")

    assert response.status_code == 200
    assert no_banner(response.text), response.text


async def test_an_expired_flash_signature_renders_no_banner():
    app = make_app(stub({}))
    async with signed_in(app) as web:
        sig = tokens.seal({"text": PHISH, "path": "/drafts"}, ttl=-1)
        response = await web.get(f"/drafts?msg={quote(PHISH)}&msgsig={quote(sig)}")

    assert response.status_code == 200
    assert no_banner(response.text), response.text


async def test_a_valid_signature_for_different_text_renders_no_banner():
    """The mismatch case the equality check in `flash()` exists for: a
    signature that unseals cleanly, but for text other than the `msg` param it
    rides alongside — a stale or borrowed `msgsig` validating a swapped-in
    `msg` — must not be treated as a live flash."""
    app = make_app(stub({}))
    async with signed_in(app) as web:
        sig = tokens.seal({"text": "Payout sent.", "path": "/drafts"}, ttl=300)
        response = await web.get(f"/drafts?msg={quote(PHISH)}&msgsig={quote(sig)}")

    assert response.status_code == 200
    assert "Payout sent." not in response.text
    assert no_banner(response.text), response.text


async def test_special_characters_survive_the_url_round_trip_and_still_match():
    """`redirect()`'s own docstring: Conduit problem titles and asset codes
    carry `&`, `%`, `=` and `+`. The comparison in `flash()` happens after the
    browser's query string round-trips through `quote_plus`/Starlette's
    decoding, so a value built from exactly those characters must still match
    its signature — a mismatch here would silently suppress a real error
    banner in production rather than a forged one."""
    text = "Route refused: rail=fedwire & fee=1%2 + surcharge"
    app = make_app(stub({}))
    async with signed_in(app) as web:
        sig = tokens.seal({"text": text, "path": "/drafts"}, ttl=300)
        query = urlencode({"msg": text, "msgsig": sig}, quote_via=quote_plus)
        response = await web.get(f"/drafts?{query}")

    assert response.status_code == 200
    unescaped = html_module.unescape(response.text)
    assert text in unescaped
    assert 'class="flash msg"' in response.text


async def test_a_valid_signature_for_a_different_page_renders_no_banner():
    """The path-binding this ticket added: a genuine, correctly-matched
    signature for the right text, minted for a *different* page, must not
    render here. Without this check, an operator's own real "Payout sent."
    banner — copied straight out of their address bar on the page it was
    earned on — would replay as an unearned banner on any other page for the
    rest of `FLASH_TTL`, which is the same scenario at a higher bar."""
    app = make_app(stub({}))
    async with signed_in(app) as web:
        sig = tokens.seal({"text": PHISH, "path": "/some/other/page"}, ttl=300)
        response = await web.get(f"/drafts?msg={quote(PHISH)}&msgsig={quote(sig)}")

    assert response.status_code == 200
    assert no_banner(response.text), response.text


async def test_a_redirect_to_a_path_that_already_has_a_query_string_still_renders_its_flash():
    """`redirect()`'s `url` sometimes already carries its own query string
    before `msg`/`msgsig` are appended (`?registered=`, `?operation=` at real
    call sites). `urlsplit` must take the path alone for what gets sealed —
    not `url` whole, which would bind the flash to a path+query combination
    the browser never round-trips identically — and the landing page's own
    `request.url.path` must still equal exactly that when the pair is
    checked."""
    request = bare_request()
    response = redirect(request, "/drafts?limit=50", msg="Payout sent.")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/drafts?limit=50&msg=Payout+sent.&msgsig=")

    app = make_app(stub({}))
    async with signed_in(app) as web:
        rendered = await web.get(location)

    assert rendered.status_code == 200
    assert "Payout sent." in rendered.text
    assert 'class="flash msg"' in rendered.text
