"""The vendored type layer, pinned where the browser cannot see it.

`font-display: swap` means a missing woff2, a typo in a `src: url()` or a
container image that forgot to copy `static/fonts` all render *perfectly*, in
the fallback, in silence. `tests/browser` proves the two OFL faces actually
load and instance; this file is the cheap half — every `@font-face` in the
stylesheet names a file that is really there.

The exception is the point of the phase. **Founders Grotesk is Klim's and is
commercial**: it is not in this repo, not copied from Conduit's CDN and not
stood in for. Its two rules name files that are expected to be ABSENT, with
DM Sans named behind them in `--font-heading` so headings render in the body
face until the licence holder drops the real ones in (deploy/README.md
§ Fonts). When that happens this test flips one way: delete `EXPECTED_ABSENT`.
"""

from __future__ import annotations

import re

from tests.conftest import ROOT

STYLES = ROOT / "static" / "styles.css"
FONTS = ROOT / "static" / "fonts"

# The licensed drop-in. Absent on purpose; see the module docstring.
EXPECTED_ABSENT = {
    "fonts/founders-grotesk-regular.woff2",
    "fonts/founders-grotesk-medium.woff2",
}

SRC = re.compile(r'@font-face\s*\{[^}]*?src:\s*url\("([^"]+)"\)', re.S)


def srcs() -> list[str]:
    found = SRC.findall(STYLES.read_text())
    assert found, "no @font-face src found — the regex or the stylesheet moved"
    return found


def test_every_font_face_src_resolves_to_a_vendored_file():
    missing = [
        s for s in srcs() if s not in EXPECTED_ABSENT and not (STYLES.parent / s).is_file()
    ]
    assert not missing, f"@font-face points at files that are not here: {missing}"


def test_founders_grotesk_is_expected_absent_with_dm_sans_named_behind_it():
    """Not "is not referenced" — it IS referenced, deliberately, and the console
    must keep working while the files are missing. Two halves: the files really
    are absent (nobody quietly vendored a commercial face or a look-alike), and
    the heading stack really does name DM Sans next."""
    declared = set(srcs())
    assert EXPECTED_ABSENT <= declared, "the Founders Grotesk rules were removed"
    for name in EXPECTED_ABSENT:
        assert not (STYLES.parent / name).is_file(), f"{name} is a commercial face — remove it"

    stack = re.search(r"--font-heading:\s*([^;]+);", STYLES.read_text()).group(1)
    assert '"Founders Grotesk"' in stack and '"DM Sans"' in stack, stack
    assert stack.index("Founders Grotesk") < stack.index("DM Sans"), stack


# The design pass's payload ruling, in the only unit that survives a re-vendor.
# Ceilings, not equalities: a byte-exact hash would fail on any legitimate
# version bump and teach the next person to delete the test.
CEILINGS = {
    # Body face, upstream whole. Latin + Latin-Extended, two variable axes
    # (opsz × wght). Not subset: it takes human names, so its coverage IS the
    # console's fallback boundary.
    "dm-sans.woff2": 95_000,
    # Mono, SUBSET. Upstream is 111 KB of Cyrillic, Greek, APL, box-drawing and
    # code ligatures; this console sets nothing in mono but machine values. The
    # exact pyftsubset command is in the `@font-face` comment in styles.css.
    "jetbrains-mono.woff2": 40_000,
}


def test_the_vendored_faces_stay_inside_their_payload_ceilings():
    """The one thing that silently undoes the subsetting ruling is someone
    re-downloading upstream and copying it over the file. Nothing else in the
    suite would notice: it renders identically and costs 80 KB more on every
    cold load."""
    over = {
        name: (FONTS / name).stat().st_size
        for name, cap in CEILINGS.items()
        if (FONTS / name).stat().st_size > cap
    }
    assert not over, (
        f"a vendored face grew past its ceiling {CEILINGS} — if this is a "
        f"deliberate re-vendor, re-run the pyftsubset command recorded in the "
        f"@font-face comment in static/styles.css: {over}"
    )


def test_each_vendored_family_ships_its_licence():
    """DM Sans and JetBrains Mono are both OFL but they are different projects
    with different copyright holders, so one shared OFL.txt cannot cover them."""
    for name, holder in (
        ("OFL-dm-sans.txt", "DM Sans"),
        ("OFL-jetbrains-mono.txt", "JetBrains Mono"),
    ):
        text = (FONTS / name).read_text()
        assert holder in text.splitlines()[0], (name, text.splitlines()[0])
        assert "SIL OPEN FONT LICENSE Version 1.1" in text, name
