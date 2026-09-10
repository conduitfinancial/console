# Design direction — imported canvas "Conduit Console.dc.html" (2026-08-31)

Source: the commissioned designer canvas; base system "Modernist" plus a
Conduit override layer. This document is the implementable distillation — the
design input. The canvas's own copy proves the designer read
DESIGN.md: honesty lines are preserved verbatim; keep them.

## Token layer (replaces current palette/type in static/styles.css)
- Ground `#ffffff`; surface `#f4f7f5`; ink/text `#103020` (deep green-black).
- Accent `#087838` with ramp: 100 `#eaf6ee` · 200 `#cfe9d8` · 300 `#a6d6b8` ·
  400 `#5fb681` · 500 `#087838` · 600 `#076a31` · 700 `#0a5228` · 800 `#0d3f22` · 900 `#103020`.
- Neutrals (green-biased): 600 `#6c7d72` · 700 `#55655b` · 800 `#35473c`.
- Divider: `color-mix(in srgb, #103020 28%, transparent)`; **2px rules are the
  structural device** (header, section tops, stat band, table header) with 1px
  hairlines inside.
- **Danger/exception `#b00020`** (pill: bg `#fdedf0`, border `#d78c98`) — used ONLY
  for Rejected/Cancelled/Failed + 3px row-left flags. Never the accent.
- Radius: sm 6 / md 8 / lg 12; **controls (buttons/tags/segments) are pill 999px**.
- Type: Archivo everywhere (Google Fonts, weights 400/600/800). H1 46px display;
  section H2 19px UPPERCASE ls 0.04em; lowercase accent eyebrows 11px ls 0.14em;
  micro-labels 10–11px uppercase ls 0.1–0.14em; body 15px; tables 14px with
  11px uppercase th; tabular-nums on all figures. Focus ring: 2px accent.
- Buttons: primary = ink bg (#103020) white text, hover accent-700; secondary =
  1px divider border; pill shape, 18px inline padding.

## Structural patterns (per canvas)
- **Header**: brand UPPERCASE 17px + outline env pill ("sandbox · host"); nav =
  uppercase 13px/800 text-buttons, active = accent + 2px bottom rule. Below: a
  1-line env strip (Development · auth mode · roles) in 10px uppercase.
- **Page head**: lowercase eyebrow (accent) → 46px H1 → one muted sentence
  (the existing honest descriptions, verbatim).
- **Overview**: hero row with primary CTA; 4-col stat band (2px top+bottom
  rules, 1px cell separators, 44px tabular figures, accent only where a number
  wants attention); two-column "Needs attention"/"Pending applications" with
  2px top rules; Open drafts + Recent transactions tables.
- **Filter trays**: surface bg, 1px border, labeled fields, secondary Reset.
- **Result meta**: "SHOWING N OF M" 13px/800 uppercase + muted breakdown +
  right Refresh link.
- **Tables**: hover rows; exception rows carry a 3px left flag (#b00020).
- **Transactions tabs**: flat band between 2px rules; active = accent-200 bg,
  accent-900 text.
- **Pills**: ok-family = accent-100/accent-400 border/accent-700 text;
  exceptions = the #b00020 set; in-progress = transparent + divider border.
  Map existing PILL_TONES semantics onto these three families + keep the
  unknown-status neutral.
- **Detail drawer (new pattern)**: right-fixed 440px panel, 2px left rule,
  label/value rows (110px uppercase micro-label column), primary action +
  Close. Canvas shows it for application quick-view with "Open customer".
  Implementation may scope it to list quick-views; full detail pages remain.
- **Footer**: "Anydollar. Anytime. Anywhere." + the not-a-bank disclaimer line.
- **Empty states**: 800-weight sentence + muted 13px detail + action link.

## IA nuances to reconcile (implementer decides, log in DESIGN.md)
- Canvas nav: Overview · Applications · RFIs · Customers · Accounts ·
  Transactions · Transact · Onboard — no Drafts top-level (drafts live in
  Overview + Onboard). Keep /drafts route; decide nav placement.
- Canvas props hint at display options (relative vs UTC timestamps, short ids,
  exception flags toggle) — treat as optional conveniences, not requirements.
- Canvas "Accounts" copy describes a local-records view — matches an earlier round
  accounts view; align copy.

## Constraints unchanged
Tokens only, one stylesheet, no build step, htmx untouched, all functional
attributes preserved, honesty rules, a11y bar (focus ring stays ≥ current),
PILL semantics drift-pinned tests must be updated WITH the vocabulary, not
loosened. Reference PNGs of prior screens are in the import's uploads/.
