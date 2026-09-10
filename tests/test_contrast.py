"""The Arca palette's contrast, recomputed from the stylesheet on every run.

## Why this file exists

`static/styles.css` quotes a contrast ratio in a comment beside almost every
colour token. A comment is a claim, and the `#9fe870` incident is what
happens when one is wrong: an accent shipped at 1.47:1 because nobody had
multiplied it out. `docs/ARCA_REVAMP_SPEC.md` §1.3 caught three more of exactly
that shape in its own source brief before a line of it was typed.

So the numbers are not trusted here. This module **parses the shipped hexes out
of the stylesheet** and computes WCAG 2.x relative luminance and contrast from
them, for both themes, on the darkest ground each token is legally allowed to
sit on — which is the number that actually binds, not the flattering one on
paper. A palette edit that lowers a ratio below its floor fails here rather than
reaching an operator.

Three floors, and one deliberate non-assertion:

* **4.5:1 — text.** Every token a rule may assign to `color`, on every ground it
  may land on.
* **3.0:1 — non-text (WCAG 1.4.11).** The focus ring, measured on the GROUND
  because `outline-offset: 2px` puts it there, and the status hues where they
  are drawn as a line (a flash border, a problem box's edge, the exception row's
  3px flag, the production badge's outline).
* **The two dark blocks must be identical.** `@media (prefers-color-scheme:
  dark) :root:not([data-theme="light"])` and `:root[data-theme="dark"]` carry
  the same values by hand; this asserts the duplication cannot drift.
* **The status `*-line` tints are NOT asserted** — recorded in
  `test_decorative_lines_are_decorative` instead. They measure 1.19–1.53:1 on
  their own fills by design: a status pill's state is carried by its own word at
  AA (DESIGN.md, 2026-08-31), and the border is the tint around it, never the
  indicator. Asserting 3:1 there would be asserting a role these values do not
  have — and the test that pins the *actual* indicator is the 4.5 floor on the
  foreground, which is above.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_PATH = Path(__file__).resolve().parents[1] / "static" / "styles.css"

_DECL = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")


def _block(css: str, marker: str) -> str:
    """The body of the brace-balanced block that `marker` opens."""
    i = css.index(marker) + len(marker)
    depth, j = 1, i
    while depth:
        depth += {"{": 1, "}": -1}.get(css[j], 0)
        j += 1
    return css[i : j - 1]


def _decls(body: str) -> dict[str, str]:
    return {k: v.strip() for k, v in _DECL.findall(body)}


@pytest.fixture(scope="module")
def css() -> str:
    """The stylesheet with its comments removed.

    Every assertion here is about what the browser is handed, and the comments
    in `styles.css` deliberately *name* the tokens this slice deleted, in order
    to say why they are gone. Parsing them would make the file's own explanation
    of the deletion into evidence that it did not happen.
    """
    return re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)


@pytest.fixture(scope="module")
def themes(css: str) -> dict[str, dict[str, str]]:
    light = _decls(_block(css, ":root {"))
    dark = _decls(_block(css, ':root[data-theme="dark"] {'))
    return {"light": light, "dark": {**light, **dark}}


def _resolve(tokens: dict[str, str], name: str) -> str:
    """Follow `var(--x)` aliases down to the literal hex."""
    seen: set[str] = set()
    val = tokens[name]
    while val.startswith("var("):
        ref = val[4:].split(")")[0].strip()
        assert ref not in seen, f"alias cycle at {ref}"
        seen.add(ref)
        val = tokens[ref]
    assert re.fullmatch(r"#[0-9a-fA-F]{3,8}", val), f"{name} is not a hex: {val}"
    return val


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    parts = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# The three page grounds, in both themes. `--page` is the window, `--paper` the
# shell, `--surface-2` the inset step — the darkest ground in light and the
# lightest in dark, and in both the one that binds.
GROUNDS = ("--paper", "--page", "--surface", "--surface-2")

# Every token a rule may put in `color`, against every ground it may land on.
TEXT_ON = {
    "--ink": GROUNDS,
    "--neutral-700": GROUNDS,
    "--muted": GROUNDS,
    "--link": GROUNDS,
    "--ok": (*GROUNDS, "--ok-bg"),
    "--warn": (*GROUNDS, "--warn-bg"),
    "--bad": (*GROUNDS, "--bad-bg"),
    "--wait": (*GROUNDS, "--wait-bg"),
    "--neutral": (*GROUNDS, "--neutral-bg"),
    # The accent fill's label, at rest and pressed.
    "--accent-ink": ("--accent", "--accent-hover"),
    # The ribbon's RFI count is knocked out of a `--bad` fill.
    "--page": ("--bad",),
}

# Non-text indicators: the ring, and the status hues where they are drawn as a
# line rather than as a word.
LINE_ON = {
    "--accent": GROUNDS,
    "--ok": (*GROUNDS, "--ok-bg"),
    "--warn": (*GROUNDS, "--warn-bg"),
    "--bad": (*GROUNDS, "--bad-bg"),
}

DECORATIVE = {"--ok-line": "--ok-bg", "--warn-line": "--warn-bg", "--bad-line": "--bad-bg"}


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("token,grounds", sorted(TEXT_ON.items()))
def test_text_clears_aa(themes, theme, token, grounds):
    """4.5:1 for every text token on every ground it is allowed to sit on."""
    tokens = themes[theme]
    fg = _resolve(tokens, token)
    for ground in grounds:
        ratio = contrast(fg, _resolve(tokens, ground))
        assert ratio >= 4.5, (
            f"{theme}: {token} ({fg}) on {ground} ({_resolve(tokens, ground)}) "
            f"is {ratio:.2f}:1, under AA's 4.5"
        )


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("token,grounds", sorted(LINE_ON.items()))
def test_lines_and_ring_clear_1411(themes, theme, token, grounds):
    """3:1 for the focus ring and for every status hue drawn as a line.

    The ring is measured against the ground rather than against the control it
    surrounds, which is what `outline-offset: 2px` buys and why the ring can be
    a single layer. Dark `--accent` on `--surface-2` is 3.05:1 — the tightest
    number in the system, and the reason the token block forbids a dark surface
    lighter than `#1a1f29` without a re-measurement.
    """
    tokens = themes[theme]
    fg = _resolve(tokens, token)
    for ground in grounds:
        ratio = contrast(fg, _resolve(tokens, ground))
        assert ratio >= 3.0, (
            f"{theme}: {token} ({fg}) on {ground} ({_resolve(tokens, ground)}) "
            f"is {ratio:.2f}:1, under 1.4.11's 3.0"
        )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_decorative_lines_are_decorative(themes, theme):
    """The pill borders are tints, and this records that they are.

    They sit between 1.1 and 2.0 on their own fills in both themes. If one ever
    climbs past 3:1 it has become an indicator by accident, and the state it
    would then be signalling has no rule behind it — so the ceiling is asserted
    as well as the floor.
    """
    tokens = themes[theme]
    for line, bg in DECORATIVE.items():
        ratio = contrast(_resolve(tokens, line), _resolve(tokens, bg))
        assert 1.1 <= ratio < 3.0, f"{theme}: {line} on {bg} is {ratio:.2f}:1"


def test_accent_is_never_text_in_dark(css: str):
    """`--accent` is 3.05:1 on dark `--surface-2`: a ring and a fill, never a word.

    `--link` exists precisely so no rule has to remember this. The check is
    syntactic and deliberately blunt — any `color:` (or `--…-fg`-shaped
    property) taking `var(--accent)` fails, in either theme, because a rule
    cannot know which theme it will be painted in.
    """
    offenders = [
        line.strip()
        for line in css.splitlines()
        if re.match(r"\s*(-webkit-text-fill-)?color\s*:\s*var\(--accent\)\s*;", line)
    ]
    assert not offenders, f"--accent assigned to a text property: {offenders}"


def test_the_two_dark_blocks_are_identical(css: str):
    """Dark is declared twice by hand; this is what stops the copies drifting.

    `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` is the
    system preference and `:root[data-theme="dark"]` is the explicit choice, and
    both must carry the same values — the guard on the first is what lets an
    explicit *Light* win over a dark system preference, which a single combined
    selector list could not express.
    """
    media = _decls(_block(css, ':root:not([data-theme="light"]) {'))
    attr = _decls(_block(css, ':root[data-theme="dark"] {'))
    assert media == attr


def test_retired_tokens_are_gone(css: str):
    """Deleted, not left defined and unused (spec §3.1, §9.4, §9.6).

    A token that is merely unused is a token a future rule can quietly reach
    for; `--r-pill` is the one this system actually lost that way, and the
    reversal only holds if the name is absent. Comments *about* the deletions
    are fine and are what the exclusion below allows — the assertion is on
    declarations and `var()` references.
    """
    for token in (
        "--r-pill",
        "--processing",
        "--rule",
        "--neutral-800",
        # The two aliases `--rule` left behind. `--divider-soft` had no consumer
        # at all and `--divider` had one, which now names `--line`: an alias
        # that outlives its reason is a name a later rule reaches for without
        # knowing which of the two it is meant to say.
        "--divider",
        "--divider-soft",
        *(f"--accent-{n}00" for n in range(1, 10)),
    ):
        assert f"var({token})" not in css, f"{token} still has a consumer"
        assert not re.search(rf"^\s*{re.escape(token)}\s*:", css, re.M), f"{token} still declared"


def test_the_radius_scale_is_the_arca_one(themes):
    """Five radii, decided per component (§3.1), and none of them 999px."""
    light = themes["light"]
    assert {k: v for k, v in light.items() if k.startswith("--r-")} == {
        "--r-xs": "4px",
        "--r-sm": "8px",
        "--r-md": "12px",
        "--r-lg": "20px",
        "--r-round": "50%",
    }


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_surface_is_a_step_off_the_ground_it_sits_on(themes, theme):
    """`--surface` must never equal the ground the rules that use it sit on.

    Seventeen rules spend `--surface`, and sixteen of them are "one step off
    `--paper`": the table-head band, the filter tray, the ribbon's active item,
    `.problem.wait`'s fill, and every hover including the ledger's row hover.
    The spec's dark block sets it *equal* to `--paper`, which makes all sixteen
    silent no-ops the moment A2 puts the shell on `--paper` — a failure a
    screenshot of A1 cannot show, because today the page ground is `--page` and
    the step is visible by accident.

    `--surface-2` is not the substitute: it is byte-identical to `--wait-bg` and
    `--neutral-bg`, so a row hover painted in it would erase the pending and
    draft chips the cursor passed over. Hence a value of its own, asserted here
    as distinct from both grounds and ordered between `--paper` and
    `--surface-2` so the three steps keep going the same way.
    """
    t = themes[theme]
    paper, surface, surface_2 = (_resolve(t, k) for k in ("--paper", "--surface", "--surface-2"))
    assert surface != paper, f"{theme}: --surface is --paper; every hover and tray is a no-op"
    step, span = contrast(surface, paper), contrast(surface_2, paper)
    assert 1.0 < step < span, (
        f"{theme}: --surface is {step:.3f} off --paper and --surface-2 is {span:.3f}; "
        "the inset step must lie beyond the tray step, not on or before it"
    )


def test_each_theme_tells_the_UA_which_one_it_is(css: str):
    """`color-scheme`, once per theme block — the browser's own pixels.

    Scrollbars, autofill, a `<select>`'s option list and the date field's picker
    indicator and panel are painted by the UA, not by this stylesheet, and they
    follow `color-scheme` alone. Without it the ledger's filter bar renders a
    near-black calendar glyph on a near-black field in dark mode, which nothing
    else here would catch: every rule in the file is correct and the control is
    still unusable.
    """
    assert re.search(r"^\s*color-scheme:\s*light;", css, re.M), "light never declares its scheme"
    assert len(re.findall(r"^\s*color-scheme:\s*dark;", css, re.M)) == 2, (
        "each dark branch needs its own color-scheme"
    )


def test_no_status_hue_is_spent_on_the_environment_badge(css: str):
    """The badge names an install, not a state any money is in (§6).

    `--ok` is the colour of a settled payment. Sandbox wore it, which put the
    taxonomy's loudest green permanently in the chrome — and in dark made it the
    most saturated thing on screen, louder than the one accent-filled action.
    Production keeps `--warn`, because that badge has something to warn about.
    """
    rules = dict(re.findall(r"\.badge\.(ok|warn)\s*\{([^}]*)\}", css))
    assert "var(--ok)" not in rules["ok"], "the sandbox badge is wearing the settled-payment green"
    assert "var(--warn)" in rules["warn"], "the production badge stopped warning"


def test_the_select_chevron_is_updated_twice(css: str):
    """One data-URI per theme, because `var()` cannot reach inside `url()`.

    Pinned because the light hex is `--muted`'s value written out by hand: a
    palette change has to come back here and say so, which is the whole reason
    the original carried a Decisions Log row. The dark rule is the second half —
    without it every `<select>` gets a near-invisible slate chevron on a
    near-black field.
    """
    assert css.count("stroke='%23475569'") == 1, "the light chevron is not --muted's hex"
    assert css.count("stroke='%23b7bec9'") == 2, "the dark chevron needs one rule per dark branch"
    assert "%2355655b" not in css, "the retired green-biased muted survives in the chevron"
