"""Every `<table>` in the templates sits inside the shared
`.table-scroll` wrapper (static/styles.css) — the fix for the pre-existing
list-table overflow at 375/768/1024 (DESIGN.md's finding).

Static, over the template SOURCE, not the rendered page: there is no shared
table-rendering macro to hang a runtime assertion off, the 27 templates that
carry a `<table>` need wildly different fixtures to render at all, and the
question — "is this literal tag preceded by the wrapper div" — is answered
identically either way. A grep is the honest form of this pin.

The last test reads the stylesheet the same way, for the one constant the
wrapper shares with `.shell` and cannot inherit from it.
"""

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
STYLES = Path(__file__).resolve().parents[1] / "static" / "styles.css"


def _px(length: str) -> float:
    m = re.fullmatch(r"([0-9.]+)(rem|px)", length.strip())
    assert m, f"unparseable CSS length: {length!r}"
    # 16 because nothing sets a root font size — asserted by
    # `test_no_root_font_size_reopens_the_gap_this_test_closes`, not assumed here.
    return float(m.group(1)) * (16 if m.group(2) == "rem" else 1)


def _html_files() -> list[Path]:
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def test_every_table_opens_inside_the_scroll_wrapper():
    offenders = []
    for path in _html_files():
        text = path.read_text()
        for m in re.finditer(r"<table\b", text):
            preceding = text[: m.start()].rstrip()
            if not preceding.endswith('<div class="table-scroll">'):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(TEMPLATES_DIR)}:{line}")
    assert offenders == [], f"<table> not immediately inside <div class=\"table-scroll\">: {offenders}"


def test_every_table_closes_inside_the_scroll_wrapper():
    offenders = []
    for path in _html_files():
        text = path.read_text()
        for m in re.finditer(r"</table>", text):
            following = text[m.end():].lstrip()
            if not following.startswith("</div>"):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(TEMPLATES_DIR)}:{line}")
    assert offenders == [], f"</table> not immediately followed by </div>: {offenders}"


def test_at_least_one_table_found():
    # A non-vacuity guard: if the templates directory ever stopped carrying
    # any <table>, the two assertions above would pass on nothing.
    total = sum(t.read_text().count("<table") for t in _html_files())
    assert total >= 40


def _rules(css: str):
    """Every `selector { declarations }` in `css`, at any nesting depth.

    Depth matters: a root font size inside an `@media` block still moves the
    root at that viewport, so a scanner that only saw top-level rules would
    miss it.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    stack: list[str] = []
    buf = ""
    for ch in css:
        if ch == "{":
            stack.append(buf)
            buf = ""
        elif ch == "}":
            prelude = stack.pop() if stack else ""
            if not prelude.strip().startswith("@"):
                yield prelude, buf
            buf = ""
        else:
            buf += ch


def _matches_the_root(selector: str) -> bool:
    """Whether any selector in this list can match `<html>` — conservatively."""
    for one in selector.split(","):
        one = one.strip()
        if not one:
            continue
        if re.search(r"(?<![\w-])(?:html|:root)(?![\w-])", one) or re.match(r"^\*", one):
            return True
    return False


def test_no_root_font_size_reopens_the_gap_this_test_closes():
    """The one premise `_px` rests on, policed rather than commented.

    A `rem` inside `@media` resolves against the *initial* font size, not the
    root element's, so a root font size would move `.shell` and leave the media
    query where it was — and the tie below would still pass, converting both at 16.
    """
    offenders = [
        f"{sel.strip().splitlines()[-1].strip()} {{ … }}"
        for sel, decls in _rules(STYLES.read_text())
        if _matches_the_root(sel)
        and re.search(r"(?<![\w-])font(?:-size)?\s*:", decls)
    ]
    assert offenders == [], (
        "a root font size makes `rem` mean different things to `.shell` and to the "
        f"media query, and _px's 16 stops being true: {offenders}"
    )


def test_the_wrappers_media_query_uses_the_shells_own_max_width():
    # The two constants are one decision — the wrapper scrolls exactly where a
    # table can outgrow the shell — but `@media` cannot read a custom property,
    # so the width is written twice. This is what ties them: edit either alone
    # and the band stops matching the shell it was derived from.
    css = STYLES.read_text()
    shell = re.search(r"^\.shell\s*\{(.*?)^\}", css, re.S | re.M)
    assert shell, "no top-level .shell rule in styles.css"
    shell_max = re.search(r"max-width:\s*([^;]+);", shell.group(1))
    assert shell_max, ".shell no longer declares a max-width"
    query = re.search(
        r"@media\s*\(\s*max-width:\s*([^)]+?)\s*\)\s*\{[^}]*?\.table-scroll\s*\{[^}]*?overflow-x:\s*auto",
        css,
        re.S,
    )
    assert query, "no `.table-scroll { overflow-x: auto }` media query in styles.css"
    assert _px(shell_max.group(1)) == _px(query.group(1)), (
        f".table-scroll scrolls up to {query.group(1).strip()} but .shell caps at "
        f"{shell_max.group(1).strip()}; the wrapper's band has to be the shell's own width"
    )
