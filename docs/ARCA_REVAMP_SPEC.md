# Arca revamp — consolidated specification

**Status:** proposal, not yet implemented. Written 2026-09-02;
questions §8.1–§8.5 answered by the owner the same day.
**Inputs:** the owner's Arca direction brief; a live front-end audit of this
console at 1440 / 1024 / 768 / 375; `DESIGN.md` and `DESIGN_DIRECTION.md` read
in full; the reference mockup ("Arca, in confident type") read as source,
not as a screenshot.
**Relationship to `DESIGN.md`:** this document is the *plan*. `DESIGN.md` stays
the source of truth. Every slice below lands its own dated section under a new
"Arca direction" heading plus its Decisions Log rows, exactly as earlier
rounds did. Nothing here is authoritative until it has been written there.

---

## 0. The consolidated finding

The brief asks for a visual override. The audit found that this console's visual
layer is already its strongest part — a token system with contrast ratios
computed and annotated per token, a locked accent, a documented radius scale,
and one committed theme. What it found broken was
everywhere *else*: the failure paths, the responsive collapse, and the reading
load.

Those two facts combine into one conclusion, and it is the main
recommendation of this document:

> **A pure re-skin would repaint the good half and ship the broken half.**
> Most of the fixes live in the same files the re-skin already has to open.
> Fold them into the slices. Do not run them as a separate project.

### 0.1 The audience, precisely

Owner's ruling, 2026-09-02: **the scope has not changed.** This remains an
internal tool. What changed is *whose* internal tool — it is deployed to
client organizations and used by their **treasury teams**, and it has to feel
like a real product rather than something built for the team that wrote it.

That is a specific and well-understood category: professional B2B treasury
software (Modern Treasury, Kyriba, Trovata are the shape). It is **not**
consumer fintech, and several conclusions follow that a "customer-facing"
reading would have got wrong:

| | Consumer reading (wrong) | Treasury-team reading (correct) |
|---|---|---|
| Users | Occasional, untrained | **Trained, daily, high-volume** |
| Primary device | Phone | **Desktop, often two monitors** |
| Density | A cost to be reduced | **A feature. They scan hundreds of rows.** |
| Navigation | Four-item top bar | **Persistent left nav — what the category uses** |
| Scrutiny | App-store polish | **Procurement, security review, accessibility questionnaires** |

So `DESIGN.md`'s density risk (#1), its monospace-as-identity risk (#3) and its
compact table spacing all **survive intact**. What does not survive is the
"handful of *our own* operators" premise underneath the missing skip link, the
missing dark mode, the desktop-only responsive story and the deployment
debug info on every page.

### 0.2 Four audit findings, re-ranked against that audience

| Audit finding | Severity | Why |
|---|---|---|
| Upstream errors render Conduit's API-doc prose verbatim | **Blocking** | A client's treasury team reading your vendor's developer docs is a support ticket and a trust hit. This is the loudest "internal tool" signal in the app. |
| `404` / `500` render raw JSON with no chrome | **Blocking** | Nothing says *unfinished* louder than a framework default. Cheapest fix in the plan. |
| No skip link, by the 2026-09-02 "mouse-first operators" ruling | **Restore** | Not because customers deserve it, but because enterprise procurement asks for a VPAT and this is a documented gap with ~18 links ahead of content on every page. |
| Ribbon collapse below `60rem` scrambles into interleaved columns | **Fix, don't optimise** | Treasury teams do not run payment batches on a phone. Mobile must not be *broken*; it need not be *good*. A single-column stack closes it. |

The first two are the whole "feels like a real product" gap. They are also the
cheapest work in the plan, which is why step 3 sits ahead of step 4.

### 0.3 The one sentence that has to change first

`DESIGN.md`'s **Product context** currently reads:

> **Who it's for:** a handful of trained internal operators who use it daily
> and scan hundreds of table rows per session. *Not customers, not prospects.*

The first clause survives and is load-bearing — trained, daily, high-volume is
still exactly right, and it is what keeps the density risk, the compact tables
and the monospace identity in the document. What has to go is *"a handful"* and
*"not customers"*: these are another organization's treasury staff, they did not
choose this tool, and nobody can walk over to their desk and explain a screen.

That distinction is the whole revamp in one line. Everything justified by
*trained and daily* stays. Everything justified by *ours and few* — the missing
skip link, the missing dark mode, the desktop-only responsive story, the API
host and auth mode printed on every page — is reopened.

**Rewrite Product context in step 0, before a single token moves**, or each of
those gets re-argued one at a time against a premise no longer in the document.

The memorable thing survives unchanged and gets harder to honour, not easier:
*the console never lies about the state of money.* A reader who cannot ask you
in person has less to recover a lie with.

---

## 1. Token layer — verified

Every ratio below was computed, not eyeballed, per the brief's instruction and
the `#9fe870` precedent. Each is quoted **on paper and on the darkest
ground the token is legally allowed to sit on**, because that second number is
the one that actually binds.

### 1.1 Light

```
--page          #f5f5f7   window ground (new role, see §2.1)
--paper         #ffffff   shell ground
--surface-2     #eef0f3   readonly boxes, avatar fills, inset chrome
--line          #e3e4e8   1.27:1 on paper — non-text, structural only
--ink           #0b0e14   19.32:1 paper · 16.92:1 surface-2
--muted         #475569   7.58:1 paper · 6.64:1 surface-2      (was ~7.5 in brief)
--neutral-600   #5b6472   5.98:1 paper · 5.24:1 surface-2      (brief quoted the surface-2 figure)
--accent        #3355ff   5.41:1 paper · 4.74:1 surface-2      passes AA as link text everywhere
--accent-ink    #ffffff   5.41:1 on the accent fill
--ok            #0b7a61   5.29:1 paper · 4.63:1 surface-2
--warn          #a5680c   4.58:1 paper · 4.01:1 surface-2      ⚠ see §1.3
--bad           #b00020   7.33:1 paper · 6.42:1 surface-2      unchanged, per brief
--processing    #2a41cc   7.70:1 paper · 6.75:1 surface-2
```

`--neutral-600` keeps its existing contract verbatim: **borders and rules only,
never text.** The hex changed; the rule did not.

### 1.2 Dark

The mockup carries **three** dark grounds, not two. The brief's token list
conflates `--paper` and `--surface` at `#0b0e14`; the mockup separates the
window ground from the shell it floats on, and the shell is what text actually
sits on. Adopt the mockup's three:

```
--page          #0b0e14   window ground
--paper         #12161e   shell ground  ← the brief omits this one
--surface-2     #1a1f29
--line          #242a35   1.34:1 on page — non-text
--ink           #f5f6f8   17.86 page · 16.75 shell · 15.27 surface-2
--muted         #b7bec9   10.32 · 9.68 · 8.82
--neutral-600   #8791a0   6.06 · 5.68 · 5.18        borders only, as in light
--accent        #3355ff   3.57 · 3.35 · 3.05        ⚠ fill and ring ONLY, see §1.3
--accent-ink    #eef1ff   4.80:1 on the accent fill (mockup's dark value, not #ffffff)
--ok            #22d3a6   10.06 · 9.43 · 8.60
--warn          #f2b84b   10.79 · 10.12 · 9.22
--processing    #c7d1ff   12.87 · 12.07 · 11.00
--bad           #b00020   2.64 · 2.47 · 2.25        ⚠ FAILS, see §1.3
```

### 1.3 Three defects in the brief's palette, and the fixes

The brief warned that a colour can look fine and not be. Three of its own values
are that case.

**(a) `--bad: #b00020` is unusable in dark mode. 2.64:1 on the page ground,
2.47:1 on the shell.** That fails AA text (4.5:1) and fails even the 3:1 that
1.4.11 asks of a non-text indicator. This is the exact shape of an earlier round
`#9fe870` incident.

The brief's two instructions collide here: *"keep existing `#b00020` … exactly
as documented"* and *"build the dark variant too."* Both cannot hold. The
resolution that preserves the **reasoning** rather than the hex:

> `bad` stays the one unambiguous red, reserved for terminal failure and
> nothing else, in both themes. The *reservation* is the documented decision;
> the hex was only ever its light-theme expression. Dark mode gets
> `--bad: #ff6b7d` (7.04 page · 6.60 shell · 6.02 surface-2). No other token
> in either theme is allowed near that hue.

**Flag for the owner:** this is a deviation from a literal instruction, taken
because the literal instruction is unshippable. Confirm the hex or name another
that clears 4.5:1 on `#12161e`.

**(b) `--accent: #3355ff` cannot be text in dark mode.** 3.57:1 on page, 3.05:1
on surface-2. It clears 1.4.11's 3:1, so it is fine as a **focus ring** and fine
as a **fill** with `--accent-ink` on top. It is not fine as link text, and this
console sets `--link: var(--accent-700)` today — every link on every page.

Split the token in dark mode only:

```
--accent       #3355ff   fill + focus ring, both themes   (brief is right)
--link         light: #3355ff (5.41:1) · dark: #93a5ff (8.34 page · 7.13 surface-2)
```

Note the 3.05:1 on `surface-2` leaves no margin — a dark surface one step
lighter drops the ring below 3:1. The existing `outline-offset: 2px` is what
saves it, by measuring the ring against the ground rather than the control's
own fill. **That earlier decision survives the override and should be
re-logged, not silently inherited.**

**(c) `--warn: #a5680c` is 4.01:1 on `surface-2`** — under AA for normal text.
It passes on paper (4.58:1) but status text lands on tinted grounds. Either
darken to `#8a5a00` (5.93 paper · 5.51 on a warm tint) — which is the value
this console already ships — or forbid `warn` from ever sitting on `surface-2`.
Recommend the darker hex; it costs nothing visually and removes a rule nobody
will remember.

### 1.4 The gap the brief does not fill: status backgrounds

The five-state taxonomy in this codebase is **fg / bg / line triples** — fifteen
values per theme, thirty across both. The brief supplies eight foregrounds. The
remaining twenty-two have to be originated.

Proposed, measured, and following the brief's stated principle (*flat unless
it is the one deliberate accent surface*) by tinting only the two states that
must interrupt:

| State | Light fg / bg / line | Dark fg / bg / line | Measured |
|---|---|---|---|
| `ok` | `#0b7a61` / transparent / `--line` | `#22d3a6` / transparent / `--line` | 5.29 · 10.06 |
| `warn` | `#8a5a00` / `#fdf6e7` / `#8a5a00` | `#f2b84b` / `#2a2110` / `#f2b84b` | 5.51 · 8.87 |
| `bad` | `#b00020` / `#fdecef` / `#d78c98` | `#ff6b7d` / `#2a1116` / `#ff6b7d` | 6.43 · 6.43 |
| `wait` | `--ink` / transparent / `--line` | `--ink` / transparent / `--line` | 19.32 · 17.86 |
| `neutral` | `--muted` / transparent / `--line` | `--muted` / transparent / `--line` | 7.58 · 10.32 |

The taxonomy is untouched — five states, same strict meanings, same
`PILL_TONES` mapping, same "unknown enum renders `neutral`" rule, same
drift-pinned tests. What changes is that three of the five stop carrying a
fill, which is exactly what the mockup does (`.status.settled` and friends are
bare coloured mono text, not chips). See §3.2 for the component consequence and
the question it raises.

---

## 2. Structure — what the mockup actually specifies, and what it breaks

Reading the mockup's CSS rather than its screenshot turns up two structural
facts the brief does not mention.

### 2.1 The shell is a new layout primitive

```css
.shell { background: var(--paper); border: 1px solid var(--line);
         border-radius: 20px; overflow: hidden; box-shadow: var(--shell-shadow); }
--shell-shadow: 0 1px 2px rgba(11,14,20,.05), 0 14px 32px rgba(11,14,20,.07);  /* light */
--shell-shadow: none;                                                          /* dark */
```

The window ground is grey; the app is a white rounded card floating on it. This
is where the brief's *"radius spent deliberately on a small number of
surfaces"* actually goes — the shell and the one accent block, and nowhere
else. It also means `body` gains a ground colour distinct from the content
ground, which the current stylesheet has no concept of.

Note the shadow is `none` in dark mode. Depth is carried by the `--paper` /
`--page` step, not by a glow. This is compatible with `DESIGN.md`'s existing
"depth by hairline, never by grey fill" rule and should be logged as such.

### 2.2 The mockup's navigation cannot hold this app's IA

The mockup nav is a horizontal top bar with **four** links (Overview, Payments,
Cards, Reports) and, at `max-width: 720px`:

```css
@media (max-width:720px){ .navlinks{ display:none; } }
```

It deletes the navigation with no replacement. That is fine for a one-screen
design mockup and is not a shippable mechanism.

This console's ribbon carries **~18 destinations** in four labelled groups, a
browse/act divider, a badge count, an env strip and a tour link. It does not
fit a four-item top bar, and the audit already found the *existing* collapse
below `60rem` is broken — bare `.item` anchors and five-item `.group` columns
are siblings in one `flex-wrap: wrap` row, so at 768px the heading **ACTIONS**
lands on the same baseline as *Onboard* (which belongs to it) and *RFIs* (which
does not), while **TRANSACT** wraps away from its own rule.

Three options were considered:

| Option | Desktop | Mobile | Cost |
|---|---|---|---|
| **A. Keep the ribbon, re-skin it** | Left column, Arca tokens | Single-column stack, no wrap | Low |
| **B. Adopt the top bar, group the IA** | 5–6 top items, sub-nav per section | Drawer or sheet | High. Two nav systems. |
| **C. Hybrid** | Top bar for 5 primaries, browse/act as page-level sub-nav | One nav, disclosed | Medium |

**Resolved: A.** Keep the ribbon.

An earlier draft of this document recommended **C**, on a reading of the
audience as consumer-facing. The owner's ruling in §0.1 makes that wrong.
Persistent left navigation *is* what professional treasury software looks
like — it is the category's convention, not a legacy of this app's internal
origins — and it is the only one of the three that holds ~18 destinations in
four labelled groups without inventing a second navigation layer.

**What the revamp takes from the mockup is its shell, its type, its colour and
its flatness. Not its navigation**, which was drawn for a four-section
consumer dashboard and does not survive contact with this IA. That is a
deliberate rejection and gets its own Decisions Log row.

The mobile collapse is still fixed in step 2 — a `display:none` nav does not
ship at any audience — but as correctness, not as a mobile-first rebuild.

### 2.3 Density and hero numerals

The brief's split is right and the mockup confirms it precisely: `.balance .num`
is `clamp(2.4rem, 5vw, 3.4rem)` at `-.02em` in the display face with
`tabular-nums`, while `.activity .amt` is **14px, weight 500** — barely larger
than body. Editorial numerals belong to account and treasury summaries only.
List rows stay compact.

Apply the big-numeral treatment to: the Overview stat band, account balance
headers, batch totals. **Not** to: any table cell, anywhere.

---

## 3. What the mockup does not answer, resolved

The mockup has no form input, no button, no table, no empty state, no
destructive action, no loading indicator and no focus ring. This console has all
of them, most with logged rationale. Below is the extension, derived from the
brief's stated principles rather than invented pixel values.

### 3.1 Radius — decided per component, not find-replaced

Current `DESIGN.md` makes **every** control `--r-pill: 999px`. Arca's instinct is
the opposite. Seven rules consume `--r-pill` today (`static/styles.css`
lines 266, 318, 664, 941, 1165, 1245/1247, 1302). Each was reconsidered:

| Component | Today | Arca | Why |
|---|---|---|---|
| Page shell | n/a | **20px** | The mockup's one deliberate rounded surface |
| Accent block | n/a | **20px** | The second, and the last |
| Card / flash / problem | 8px | **12px** | Softer, still clearly subordinate to the shell |
| Button `.btn` | pill | **8px** | The brief's central behavioural change |
| Input `.field` | 6px | **8px** | Matches buttons; a form should read as one system |
| Chip, `.pill-group label`, `.segmented` | pill | **8px** | Selection is not a different material from action |
| Status `.pill` | pill | **4px** | Nearly flat; see §3.2 |
| Env `.badge` | pill | **4px** | Same family as status; see §4 for whether it survives at all |
| Ribbon count | pill | **stays round** | It is a count on a dot, the one place round is the meaning |
| Avatar / initials | n/a | **50%** | New component, from the mockup |

Proposed scale: `--r-xs: 4px · --r-sm: 8px · --r-md: 12px · --r-lg: 20px ·
--r-round: 50%`. **`--r-pill` is retired** — deleted, not left defined and
unused, so no future rule can quietly reach for it. That deletion needs its own
Decisions Log row, because it reverses a decision imported verbatim
from `DESIGN_DIRECTION.md`.

### 3.2 Status pills — flatter shape, quieter tints, all five kept

The mockup renders status as bare coloured uppercase mono text at 10px
(`.status.settled`), with no chip, no border, no fill. This console renders a
bordered, tinted pill. Both cannot be true, and this is a legibility decision
rather than a styling one.

**Resolved: do not flatten. Keep all five states tinted.**

An earlier draft proposed resolving `ok`, `wait` and `neutral` to a transparent
background, following the mockup. Against the audience in §0.1 that is the
wrong trade. The mockup renders five activity rows; a treasury team scans
twenty-five to a hundred, and a tinted chip is findable down a column in a way
coloured text is not. `DESIGN.md`'s density risk (#1) exists precisely because
this audience scans, and that risk survives the override.

What Arca gets instead is **quieter tints and a flatter shape** — the same
five families, same fg/bg/line triples, same `PILL_TONES` mapping, same
drift-pinned tests, at `--r-xs: 4px` instead of a 999px pill, with tints pulled
back toward the ground:

| State | Light fg / bg | Measured on tint |
|---|---|---|
| `ok` | `#0b7a61` / `#eef7f4` | 4.85:1 |
| `warn` | `#8a5a00` / `#fbf5e9` | 5.46:1 |
| `bad` | `#b00020` / `#fdf0f2` | 6.60:1 |
| `wait` | `--ink` / `#eef0f3` | 16.92:1 |
| `neutral` | `--muted` / `#eef0f3` | 6.64:1 |

This supersedes the transparent-background proposal in §1.4 for `ok`, `wait`
and `neutral`; the table above is the one to implement.

### 3.3 Buttons and destructive actions

Primary keeps its existing contract — **one forward action per page.** Under
Arca the fill becomes `--accent` with `--accent-ink` (5.41:1 light, 4.80:1
dark) rather than ink-filled. The reasoning is not "Arca has an interaction
colour"; it is that dropping `--processing` removes the collision that forced
the ink fill in the first place. See **§9.5**, which is the authoritative
statement.

Destructive buttons stay **outlined, never filled**, and the 2026-08-29
rationale carries over word for word: colour means state, a button is an
affordance, nothing has gone wrong yet, and a row of filled-red buttons reads
as a page full of failures. In dark mode the outline takes `#ff6b7d` per
§1.3(a).

Text-as-affordance survives and gets *more* room: the mockup's `.quick` block is
three hairline-separated text links with an arrow and no button chrome at all.
Where this console already prefers a quiet link over a button, keep it.

### 3.4 Focus ring

Collapses from two layers to one, and is *better* than the current one.
`--accent #3355ff` measures 5.41 / 4.97 / 4.74 on the three light grounds and
3.57 / 3.35 / 3.05 on the three dark ones — clearing 1.4.11's 3:1 everywhere,
including against an accent-filled button, because `outline-offset: 2px` puts it
on the ground rather than on the control. The two-layer ring existed
only because `#9fe870` was 1.47:1; that reason is gone.

**Constraint to log:** no dark surface lighter than `#1a1f29` may be
introduced without re-measuring, because the ring is at 3.05:1 there.

### 3.5 htmx states, autosave, empty states

- **In-flight.** `--t-fast: 120ms ease-out`, the `.htmx-request` dim and the
  inline muted "Working…" all survive unchanged. Text, not a spinner, remains
  right: the mockup has no motion of any kind.
- **Autosave / `.save-status`.** Survives. Re-skin to `--muted` mono micro-label.
- **Empty states.** Survive *and are an asset*. "Nothing is waiting on a human.
  Every mutation this console sent has an answer." is better copy than most
  treasury software ships. Re-skin only; do not rewrite.
- **`prefers-reduced-motion`** stays wired to the lot.

### 3.6 Typography

| Role | Today | Arca |
|---|---|---|
| Display | Archivo 800 | **Bricolage Grotesque 600**, `-.02em` on hero numerals |
| Body / UI | Archivo 400/500/600 | **Hanken Grotesk 400/500/600** |
| Eyebrows, micro-labels, status, timestamps, ids, amounts | mono stack | **system mono stack, unchanged** |

Note the weight drop: the mockup's heaviest weight is **600**, against this
system's 800 display weight. That single change carries most of the
"confident, not shouty" difference and should not be lost while chasing hexes.

`font-variant-numeric: tabular-nums` and mono-for-machine-values carry forward
unchanged, per the brief and per the mockup's own usage.

**Vendoring tradeoff, flagged as the brief asks.** Bricolage Grotesque and
Hanken Grotesk are both OFL and both vendorable exactly the way Archivo was
(fetch woff2 per subset into `static/fonts/`, licence alongside, `@font-face`
with the provenance comment block). That is 2 new families, roughly 6 files,
against Archivo's 3. IBM Plex Mono is a third face for marginal gain — the
brief itself calls the system mono stack a reasonable substitute, and this
recommendation takes it.

> **Decision for the owner:** vendor two faces now, or keep Archivo and treat
> the typeface swap as a later slice? Archivo at weight 600 instead of 800 gets
> a surprising amount of the way there for zero new bytes. This is an identity
> choice, so it is being asked rather than decided.

---

## 4. Audience decisions — resolved

Each of these falls out of §0.1's ruling rather than from the visual override.

1. **The environment badge — keep the environment, drop the hostname.**
   `sandbox · api.sandbox.conduit.financial · ceiling 5000` is the best
   risk-design decision in the app and a treasury operator must never be
   uncertain which environment their money is moving in. But
   `api.sandbox.conduit.financial` is *your* infrastructure, not their
   information, and printing a vendor hostname on every page is one of the
   things that makes this read as internal tooling. **Ship
   `Sandbox · ceiling 5000`.** The word and the ceiling are the money facts;
   the host is deployment trivia. In production the badge earns its strongest
   form: `Production`, with no ceiling and no host.
2. **The env strip — split it.** `AUTH_MODE=disabled` is deployment debug
   output and does not belong on a client's screen at all; it moves to a
   health or settings surface for whoever operates the install. The actor's
   **display name and roles stay** — a treasury user needs to know why a
   button is not there, and an earlier round rule (a forbidden affordance is not
   drawn) only works if you can see what you hold.
3. **One app, one audience.** Resolved in §0.1: professional treasury teams.
   Every "which audience wins" question in this document collapses to that
   answer, and the density risk survives every one of them.
4. **The skip link — restore it.** The premise of the 2026-09-02 removal
   ("mouse-first payment operators") does not survive the audience being
   another organization's staff. The stronger argument is procurement: B2B
   financial software gets accessibility questionnaires, and this is a
   documented gap sitting behind ~18 links on every page. One line of markup
   and one CSS block, both recoverable from the git history of the comment
   in `base.html`.
5. **The teaching prose — demote to disclosure, do not delete.** A daily user
   does not need "Ledger reference: the reference this console stamps on
   everything it sends" on every visit, and the ~90 words between the filter
   bar and the first row of `/transactions` are pure cost after week one. One
   sentence in the lede, the rest inside a `<details>` beside the filter bar.
   **The copy itself is an asset** — it is more honest than most treasury
   software manages — so it moves, it does not get rewritten.
6. **Prose measure — widen to 72–75ch.** Measured live: lede 533px inside a
   1217px column. Adopting the mockup's `max-width: 1180px` page instead of
   this console's 1360 fixes the ratio on its own and is the cheaper change.

### 4.1 The three patterns the brief asked about — all three survive

- **Wizard mono step markers (`.step-n`, `.form-section`).** *Survives.*
  A place-in-a-61-field-form device, and mono micro-labels are exactly the
  register the mockup uses for this. Re-skin only. The counter stays a
  *remaining* count with no colour on completion — a direct application of
  "colour means money state", untouched by the override.
- **Two-zone timestamp** (local in the text, UTC in `title`). *Survives, and
  the audience strengthens the case rather than weakening it.* An earlier
  draft flagged this as needing a ruling, on the theory that UTC is an
  operator's concern. It is not: **treasury reconciliation against bank
  statements and support conversations with Conduit are both quoted in UTC**,
  and a treasury team does that work daily. Keep both zones, both labelled,
  exactly as the 2026-08-31 row specifies.
- **"What happens next" column.** *Survives and should be promoted.* One muted
  sentence per operation state answering "whose problem is this". For a client
  team who cannot walk over to whoever operates the install, that column is
  the difference between a wait and a support ticket. Widen its coverage
  rather than trimming it.

---

## 5. Slice plan

Following the established pattern: token layer first, verify on real
pages, then outward by traffic. No slice touches more than it can verify.

| # | Slice | Contents | Gate |
|---|---|---|---|
| **0** | **Premise** | Rewrite `DESIGN.md` Product context (§0.3) and Aesthetic direction. All seven questions in §8 are answered; nothing is blocking. | No code. Owner sign-off. |
| **1** | **Tokens** | Light + dark custom properties per §1, three-branch theme mechanism (`:root` → `@media` guarded by `:not([data-theme="light"])` → `[data-theme="dark"]`), fonts vendored per §3.6, radius scale per §3.1, `--r-pill` deleted. | Renders sanely on Overview + Transactions in both themes. |
| **2** | **Shell + nav** | `base.html`: the `--page`/`--paper` shell per §2.1, the nav decision from §2.2, **the mobile collapse fix**, skip link restored. | Nav is usable and correct at 375 / 768 / 1024 / 1440. |
| **3** | **Failure paths** | `404` / `403` / `500` templates extending `base.html`. Translate upstream problems at the boundary (`app/conduit/client.py:173` → `problem()` in `macros.html:221`); bind field errors to their fields with `aria-invalid`. | A mistyped URL and a bad country both stay inside the product, in the product's voice. |
| **4** | **High traffic** | `dashboard.html`, `customers/`, `transactions/`. Hero numerals per §2.3. Prose demotion per §4.5. | Both themes, all four widths. |
| **5** | **Long tail** | `onboarding/`, `batches/`, `payouts/`, `drafts`, `operations/`, `rfis/`, the rest. | Per §6. |

Step 3 is deliberately ahead of step 4. It is the cheapest work in the plan
and it closes the two findings that are unshippable to a customer; there is no
reason for it to wait behind three list pages.

---

## 6. Per-slice acceptance

A slice is not done until every line is honestly ticked.

- [ ] Every new text/background pairing measured, ratio in a comment beside the
      token — on paper **and** on the darkest ground it may sit on.
- [ ] AA (4.5:1) for body text, 3:1 for every non-text state indicator
      including the focus ring on all six grounds.
- [ ] Renders correctly in **both** themes. Neither was shipped unseen.
- [ ] Renders correctly at 375 / 768 / 1024 / 1440. No horizontal overflow at
      any of them.
- [ ] `prefers-reduced-motion` and `prefers-color-scheme` both honoured.
- [ ] The five-state taxonomy is intact: same states, same meanings, same
      `PILL_TONES` mapping, unknown enum still renders `neutral`. Drift-pinned
      tests updated **with** the vocabulary, never loosened.
- [ ] No status hue used for anything that is not that state. No colour used for
      navigation or decoration.
- [ ] `tabular-nums` and mono still on every id, amount, IBAN and timestamp.
- [ ] Server-rendered Jinja + htmx untouched. One stylesheet. No build step. No
      request off this origin.
- [ ] Empty, loading and error states all present — not just the success state.
- [ ] `DESIGN.md` gains its dated "Arca direction" section **and** its Decisions
      Log rows before the slice is called done.

---

## 7. Decisions Log rows this plan will owe

Drafted here so the reasoning is not reconstructed later. Each lands in
`DESIGN.md` with the slice that implements it.

| Decision | Rationale to record |
|---|---|
| Product context rewritten: still an internal tool, now another organization's treasury team | Step 0. The palette is downstream of the audience, not the other way round. *Trained, daily, high-volume* survives and keeps density risk #1, the compact tables and the monospace identity. *A handful of ours* does not, and it was the sole justification for the missing skip link, the missing dark mode, the desktop-only responsive story and the deployment debug info on every page. Reopening those four at once beats re-arguing each against a premise no longer in the document. |
| Dark mode ships from day one, reversing the 2026-08-29 "not in v1" row | Step 1. That row's reasoning was explicitly "YAGNI for a handful of internal operators". The audience changed; the YAGNI did not survive it. |
| `--bad` becomes `#ff6b7d` in dark mode, against a literal instruction to keep `#b00020` | Step 1. `#b00020` measures **2.64:1** on `#0b0e14` and **2.47:1** on the `#12161e` shell — below AA text and below 1.4.11's 3:1 for non-text. What was documented and worth keeping is the *reservation* (one unambiguous red, terminal failure only, never an affordance); the hex was that reservation's light-theme expression. Same failure shape as the `#9fe870` at 1.47:1, caught the same way — by measuring. |
| `--link` splits from `--accent` in dark mode (`#93a5ff`, 8.34:1) | Step 1. `#3355ff` is 3.57:1 on the dark ground: sufficient as a fill or a ring under 1.4.11, insufficient as text under AA. The accent keeps both jobs it can do and loses the one it cannot. |
| `--warn` stays `#8a5a00`, not the brief's `#a5680c` | Step 1. `#a5680c` is 4.01:1 on `surface-2`, and status text lands on tinted grounds. The darker hex is 5.51:1 on the same ground for no visible cost, and removes a "never place warn here" rule nobody would remember. |
| `--r-pill` deleted, radius decided per component | Step 1. Reverses an earlier round row imported verbatim from `DESIGN_DIRECTION.md` ("every control is pill 999px"). Arca spends radius on two surfaces — the shell and the accent block — and keeps the rest near-flat. The token is removed rather than left defined-but-unused so no later rule can reach for it without coming here first. |
| The focus ring returns to one layer | Step 1. The two-layer ring existed solely because `#9fe870` was 1.47:1 on paper and could colour a ring but not be one. `#3355ff` is 5.41:1 light and 3.57:1 dark and carries the contrast itself. `outline-offset: 2px` is retained and load-bearing: it measures the ring against the ground, which is the only reason an accent-filled button's ring still clears 3:1. |
| Ribbon → *(nav decision from §2.2)* | Step 2. The mockup's four-link top bar cannot hold ~18 destinations in four groups, and its mobile rule is `display:none` with no replacement. Records which option was taken and why. |
| Mobile navigation fixed, not merely re-skinned | Step 2. Below `60rem` the current wrap puts bare `.item` anchors and five-item `.group` columns in one `flex-wrap` row, so section heads and their own links land on different rows: at 768px the **ACTIONS** head sits beside *Onboard* and *RFIs*, and **TRANSACT** wraps away from its rule. Tolerable while the readers sat nearby; not shippable to another organization, even one that will almost always be on a desktop. Fixed as correctness, not rebuilt as mobile-first. |
| Skip link restored | Step 2. The 2026-09-02 removal was reasoned from "mouse-first payment operators". The reasoning is gone with the audience, and the ribbon has since grown to ~18 links ahead of content on every page. |
| Upstream problem details translated at the boundary | Step 3. `problem()` renders `title`/`detail`/`resolution` straight off the wire, so a treasury operator typing an unknown country is shown *"Check the 'errors' array in the response … also inspect the optional 'field' member"* — Conduit's developer documentation, shown to a client's finance staff about their own country entry. The console's own `local_problem` copy is already in the right register, which is the proof the register was never the problem; the pass-through path just never got the same pass. |
| `404` / `403` / `500` get templates | Step 3. A mistyped URL currently renders `{"detail":"Not Found"}` as raw JSON with no chrome and no way back — the framework default showing through the one surface nobody styled. Every other surface in this app is careful about never stranding the reader. |
| Status pills keep all five tints; only the shape and the tint strength change | Step 4. The mockup renders status as bare coloured mono text, and an earlier draft of this plan followed it. Rejected: the mockup shows five activity rows and this audience scans twenty-five to a hundred, where a tinted chip is findable down a column and coloured text is not. Density risk #1 is the reason, and it survives the override. The taxonomy, the `PILL_TONES` mapping and the drift-pinned tests are untouched; the pill goes from 999px to 4px and the tints pull back toward the ground. |
| The mockup's top-bar navigation is deliberately rejected | Step 2. Arca's nav is four links with `display:none` below 720px — drawn for a four-section consumer dashboard. This IA is ~18 destinations in four labelled groups plus a browse/act seam, and persistent left navigation is the professional-treasury-software convention rather than a legacy of this app's origins. The revamp takes the mockup's shell, type, colour and flatness; it does not take its chrome. |
| The env badge keeps the environment and the ceiling, drops the API hostname | Step 2. A treasury operator must never be uncertain which environment their money is moving in, so the word and the ceiling stay — they are money facts. `api.sandbox.conduit.financial` is the vendor's infrastructure rather than the reader's information, and a hostname printed on every page is one of the things that makes a product read as internal tooling. |
| `AUTH_MODE` leaves the page; actor name and roles stay | Step 2. `auth AUTH_MODE=disabled` is deployment debug output for whoever operates the install, and it is on every screen a client's treasury team sees. The name and roles stay, because the rule that a forbidden affordance is not drawn only works if the reader can see what they hold. |
| `--processing` dropped from the palette entirely | Step 1. The brief supplies it as a sixth colour, but the taxonomy is five states and `wait` already covers "in flight, resolving without you" — deliberately with no fill, because a payment merely on its way has not become anything yet. Worse, `#2a41cc` against the `#3355ff` accent measures **1.42:1**: the same colour to a reader, which would put a blue status pill beside a blue primary button and break the one rule this system is built on. |
| The primary button moves from an ink fill to an accent fill, restoring the two-sided colour rule | Step 1. The current "the forward action is not a hue" row was forced, not chosen: the accent green *was* the `ok`-status green, so action had to leave colour entirely to stay legible against state. Arca's accent is blue and, once `--processing` is dropped, no status hue is blue-dominant — so **accent = you can act here, status hue = the state of money** becomes true again. A restoration, not an invention. |
| Uppercase migrates from `h2` down to the mono micro-label tier | Step 1. `h2` at 19px/800 uppercase with 0.04em tracking is the industrial register being overridden, and it is the single most visible change in the revamp. The shape still marks a section; it does it at 17px/600 in sentence case. Uppercase now belongs to mono micro-labels, eyebrows and table headers and to nothing else, which is a rule rather than a leftover. |
| The `.eyebrow` loses `--accent` and becomes muted mono | Step 1. An accent-coloured eyebrow is decoration, and DESIGN.md's own rule is that colour is information and never decoration. It survived only because the accent was also the ok-status green and nobody read it as a claim about money. Under an unambiguous interaction blue it would read as one. |
| Teaching prose demoted to disclosure | Step 4. ~90 words in four blocks between the filter bar and the first row of `/transactions`. Written for a trained operator's first day and never collapsing, which makes it pure cost from week two onward for an audience that is in here daily. One sentence in the lede, the rest in a `<details>`. The copy is an asset and moves rather than being rewritten. |

---

## 8. Questions — all resolved

| # | Question | Answer | Where |
|---|---|---|---|
| 1 | One app or two surfaces? | **One.** Professional treasury teams at client organizations. Density, monospace-as-identity and compact tables all survive. | §0.1 |
| 2 | Does the env badge / env strip survive? | **Badge yes, minus the hostname. `AUTH_MODE` no; actor name and roles yes.** | §4.1, §4.2 |
| 3 | Nav architecture A, B or C? | **A — keep the ribbon.** Reverses this document's first draft. | §2.2 |
| 4 | Vendor new faces, or keep Archivo at 600? | **Vendor both, risk split.** Hanken Grotesk body, Bricolage Grotesque display only. | §9.2 |
| 5 | Dark-mode `bad` hex? | **`#ff6b7d`** (7.04 / 6.60 / 6.02). Headroom on a failure colour beats a shade of taste. | §1.3a |
| 6 | Do `ok` / `wait` / `neutral` pills flatten? | **No.** Quieter tints and a 4px shape instead. | §3.2 |
| 7 | Does the two-zone timestamp keep its second zone? | **Yes**, and the audience strengthens the case: treasury reconciles in UTC. | §4.1 |

Two further decisions were forced during the component mapping in §9 and are
recorded there rather than here: **`--processing` is dropped** (§9.4) and, in
consequence, **the primary button moves to an accent fill** (§9.5). Both are
logged in §7.

---

## 9. Implementation reference — step 1

Everything above is reasoning. This section is what gets typed.

### 9.1 The token block, paste-ready

Replaces the `:root` block in `static/styles.css` from `--fs-micro` through
`--t-fast`. Ratios are quoted **on paper and on the darkest ground the token may
legally sit on**; the second number is the binding one.

```css
:root {
  /* ---- Type scale (see §9.3 for what moved and why) ---- */
  --fs-micro: 0.6875rem;  /* 11px — mono micro-labels, eyebrows, table th */
  --fs-xs:    0.75rem;    /* 12px — meta lines, correlation ids */
  --fs-sm:    0.8125rem;  /* 13px — help text, pills, badges */
  --fs-table: 0.875rem;   /* 14px — table cells. Density survives. */
  --fs-base:  0.9375rem;  /* 15px — body, forms, nav */
  --fs-md:    1.0625rem;  /* 17px — h2 (was 19px uppercase 800) */
  --fs-lg:    2rem;       /* 32px — h1 (was 46px 800) */
  --fs-hero:  clamp(2.25rem, 4vw, 3rem);  /* 36-48px — summary figures ONLY */

  --ls-micro:   0.1em;    /* mono micro-labels */
  --ls-eyebrow: 0.09em;   /* mockup value */
  --ls-display: -0.02em;  /* h1 and hero figures */
  --fw-display: 600;      /* was 800. This carries most of the direction. */

  --font-display: "Bricolage Grotesque", system-ui, sans-serif;
  --font-ui:      "Hanken Grotesk", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono:    ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;

  /* ---- Spacing: unchanged. 4px base, compact density. ---- */
  --sp-1:.25rem; --sp-2:.5rem; --sp-3:.75rem; --sp-4:1rem; --sp-6:1.5rem; --sp-8:2rem;

  /* ---- Ground ---- */
  --page:      #f5f5f7;   /* window ground — NEW role, see §2.1 */
  --paper:     #ffffff;   /* shell ground */
  --surface:   #f5f5f7;   /* filter trays, row hover */
  --surface-2: #eef0f3;   /* readonly boxes, avatar fills */

  /* ---- Ink and neutrals ---- */
  --ink:         #0b0e14; /* 19.32 paper · 16.92 surface-2 */
  --neutral-700: #475569; /*  7.58 paper ·  6.64 surface-2 */
  --neutral-600: #5b6472; /*  5.98 paper ·  5.24 surface-2 — BORDERS ONLY, never text */
  --muted:       var(--neutral-700);

  /* ---- Interaction. Blue means "you can act here" and nothing else.
         No status hue is blue-dominant (§9.4), so this is unambiguous. ---- */
  --accent:     #3355ff;  /*  5.41 paper ·  4.74 surface-2 */
  --accent-ink: #ffffff;  /*  5.41 on the accent fill */
  --link:       var(--accent);

  /* ---- Structure. 1px hairlines; the 2px structural rule is retired
         with the industrial direction that introduced it. ---- */
  --line:      #e3e4e8;   /* 1.27 on paper — non-text, structural */
  --line-soft: #eef0f3;
  --divider:      var(--line);       /* alias kept: every consumer resolves unchanged */
  --divider-soft: var(--line-soft);

  /* ---- Five states, four visual families. Taxonomy unchanged. ---- */
  --ok:      #0b7a61;  --ok-bg:      #eef7f4;  --ok-line:      #cfe7de; /* 4.85 on tint */
  --warn:    #8a5a00;  --warn-bg:    #fbf5e9;  --warn-line:    #e4d3ae; /* 5.46 on tint */
  --bad:     #b00020;  --bad-bg:     #fdf0f2;  --bad-line:     #edc4cc; /* 6.60 on tint */
  --wait:    var(--ink); --wait-bg:  #eef0f3;  --wait-line:    var(--line); /* 16.92 */
  --neutral: var(--muted); --neutral-bg: #eef0f3; --neutral-line: var(--line); /* 6.64 */

  /* ---- Radius. --r-pill is deleted, not left unused (§3.1). ---- */
  --r-xs: 4px;    /* status pills, env badge */
  --r-sm: 8px;    /* buttons, inputs, chips, segments */
  --r-md: 12px;   /* cards, flash, problem */
  --r-lg: 20px;   /* the shell and the one accent block. Nothing else. */
  --r-round: 50%; /* avatars, the ribbon count */

  --shell-shadow: 0 1px 2px rgb(11 14 20 / .05), 0 14px 32px rgb(11 14 20 / .07);
  --t-fast: 120ms ease-out;
}

/* Dark. Three grounds, not two: text sits on the shell, not on the window
   (§1.2). Guarded with :not([data-theme="light"]) so the explicit toggle wins
   in both directions. */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { /* values below */ }
}
:root[data-theme="dark"] { /* the same values again */ }
```

The dark values, applied to **both** selectors above:

```css
  --page: #0b0e14;  --paper: #12161e;  --surface: #12161e;  --surface-2: #1a1f29;
  --ink:         #f5f6f8;  /* 17.86 page · 16.75 shell · 15.27 surface-2 */
  --neutral-700: #b7bec9;  /* 10.32 · 9.68 · 8.82 */
  --neutral-600: #8791a0;  /*  6.06 · 5.68 · 5.18 — borders only, as in light */
  --accent:      #3355ff;  /*  3.57 · 3.35 · 3.05 — FILL and RING only, never text */
  --accent-ink:  #eef1ff;  /*  4.80 on the accent fill */
  --link:        #93a5ff;  /*  8.34 page · 7.13 surface-2 — the accent cannot be text here */
  --line:        #242a35;  --line-soft: #1a1f29;
  --ok:   #22d3a6;  --ok-bg:   #0d2a24;  --ok-line:   #1c4a40;  /* 7.96 on tint */
  --warn: #f2b84b;  --warn-bg: #2a2110;  --warn-line: #4a3a1c;  /* 8.87 on tint */
  --bad:  #ff6b7d;  --bad-bg:  #2a1116;  --bad-line:  #4a2028;  /* 6.43 on tint */
  --wait: var(--ink); --wait-bg: #1a1f29; --wait-line: var(--line);
  --neutral: var(--muted); --neutral-bg: #1a1f29; --neutral-line: var(--line);
  --shell-shadow: none;   /* depth is the page/shell step, not a glow */
```

**Two constraints to log with the block.** No dark surface lighter than
`#1a1f29` may be introduced without re-measuring, because the focus ring is at
3.05:1 there. And `--accent` must never be assigned to a text property in dark
mode; `--link` exists precisely so no rule has to think about it.

### 9.2 Font vendoring

Two families, following the Archivo procedure in `static/styles.css` exactly.
**Do not invent the woff2 URLs** — Google's paths carry version hashes
(`.../archivo/v25/...`) that cannot be guessed. The procedure:

1. Fetch each stylesheet with a modern-browser `User-Agent` (a bare `curl` gets
   the TTF fallback, not woff2):
   `https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@400;500;600&display=swap`
   `https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600&display=swap`
2. Read the `src: url(...)` values out of the response. Those are the real,
   current URLs. Record them verbatim in the `@font-face` provenance comment
   with the fetch date, as the Archivo block does.
3. Download to `static/fonts/`. Keep Google's `unicode-range` values unedited —
   a codepoint outside them falling back to the system stack is the mechanism
   working, not a gap.
4. `static/fonts/OFL.txt` currently covers Archivo alone. Both new families are
   OFL 1.1; ship each licence as its own file
   (`OFL-bricolage.txt`, `OFL-hanken.txt`) rather than assuming one covers all
   three, and keep `OFL.txt` for as long as Archivo is still referenced.
5. `font-display: swap` on every face, per the existing rationale: an operator
   reads the fallback immediately and never waits on chrome to learn the state
   of money.

Expect roughly 6 files where Archivo needed 3. Both families are variable, so
the three weights are one file per subset, declared honestly as
`font-weight: 100 900` — the same shape as the existing block.

**Archivo is removed once nothing references it.** Do not leave three unused
woff2 files in the repo; that is bytes served to nobody and a licence file for
a font that is gone.

### 9.3 Type scale — what moved, and why

| Role | Was | Is | Reason |
|---|---|---|---|
| `h1` | 46px / **800** / -0.015em | **32px / 600 / -0.02em** | The weight drop needs a size to match, and under Arca the loudest thing on a page is the **number**, not the page title. A 46px/800 heading beside a 48px balance figure is two things shouting at once. |
| `h2` | 19px / 800 / **UPPERCASE** / +0.04em | **17px / 600 / sentence case** | Uppercase tracked headings are the industrial register being overridden. This is the most visible single change in the revamp. |
| Hero figures | (none) | **`--fs-hero`, display face, 600, -0.02em, tabular-nums** | New. Summary and treasury balances only — never a table cell (§2.3). |
| Body / tables | 15px / 14px | **unchanged** | Density is a feature for this audience. Do not spend the revamp here. |
| `.eyebrow` | 11px, **lowercase, `--accent`** | **11px mono, UPPERCASE, `--muted`** | Two fixes in one. Uppercase migrates *down* to the micro-label tier as h2 gives it up, and an accent-coloured eyebrow was decoration — which DESIGN.md's own rule forbids. |

The rule that falls out and should be stated in `DESIGN.md`:
**uppercase belongs to mono micro-labels and nothing else.** No heading is
uppercase; every eyebrow, table header and status label is.

### 9.4 `--processing` is dropped

The brief lists `processing/info #2a41cc`. It does not survive, for two
reasons that compound:

1. **It duplicates a state that already exists and is deliberately hueless.**
   The taxonomy is five states, and `wait` is documented as "in flight,
   resolving without you — no fill at all, because a payment that is merely on
   its way has not *become* anything yet." `processing` is that state with a
   colour bolted on, and the brief's own instruction is not to restructure the
   taxonomy.
2. **It is the same colour as the accent.** `#2a41cc` against `#3355ff`
   measures **1.42:1** — indistinguishable as *meanings* to a reader. Shipping
   both puts a blue status pill beside a blue primary button, which is exactly
   the confusion DESIGN.md's core rule exists to prevent.

Drop it. Map anything the mockup would have coloured `processing` onto `wait`.

### 9.5 The primary button moves to an accent fill — and this restores a rule

`DESIGN.md` currently says *"the forward action is not a hue"*, and ink-fills
the primary button. That decision was forced: the accent green **was**
the `ok`-status green, so action had to move off colour entirely to stay
distinguishable from state.

Arca removes the collision. With `--processing` dropped (§9.4), **no remaining
status hue is blue-dominant** — `ok` is green, `warn` amber, `bad` red, `wait`
and `neutral` hueless. So blue is free to mean one thing, and the earlier
two-sided rule comes back:

> **Accent blue = you can act here. Status hue = the state of money.**

This is a restoration, not a new invention, and it should be logged as such.
`button.primary` takes `--accent` fill with `--accent-ink` (5.41:1 light,
4.80:1 dark). One primary per page, unchanged. Destructive stays **outlined**,
with the 2026-08-29 rationale intact and `#ff6b7d` as its dark-mode stroke.

### 9.6 Component map — every rule step 1 touches

| `styles.css` | Component | Change |
|---|---|---|
| 60-145 | `:root` | Replaced wholesale per §9.1. `--accent-100…900`, `--neutral-800`, `--rule`, `--r-pill` all deleted. |
| 148 | `body` | Gains `background: var(--page)`; the shell carries `--paper`. |
| ~403 | `h1` | `--fs-lg` 32px, `--font-display`, 600, `-0.02em`. |
| ~409 | `h2` | 17px, 600, **`text-transform` and `letter-spacing` removed**. |
| ~480 | `.eyebrow` | mono, uppercase, `--ls-eyebrow`, `--muted`. Loses `--accent`. |
| ~660 | `.pill`, `#save-state > span` | `--r-xs`. **Note the coupling**: the wizard autosave state is styled through this rule *by position* (`#save-state > span`, id specificity 1,0,1) because the endpoint does not emit a class. Changing `.pill`'s selector list silently restyles autosave. |
| ~1172 | `button, .btn` | `--r-sm`. Primary takes `--accent` / `--accent-ink` per §9.5. Danger stays outlined. |
| ~1308 | `.pill-group label` | `--r-sm`. |
| ~1245 | `.segmented` | `--r-sm` on the outer corners only. |
| ~266 | ribbon `.count` | **stays `--r-round`** — a count on a dot is the one place round is the meaning. |
| ~318 | `.badge` (env) | `--r-xs`; template drops the host per §4.1. |
| ~1016 | `.field` | `--r-sm`, to match buttons. |
| `--shadow-1` | cards, `.bar` | Re-derived from `--line-soft` at the new palette. The "depth by hairline, never by grey fill" rule is unchanged and now also covers the shell. |
| 355 | `@media (max-width: 60rem)` | Ribbon collapse — **step 2**, not here. Do not touch it while re-tokenising. |

### 9.7 Step 1 is done when

- [ ] Overview and Transactions render correctly in **both** themes at 1440.
- [ ] Every ratio in §9.1 re-measured against the shipped hexes, in a comment.
- [ ] No `--r-pill`, `--accent-500`, `--processing` or `--rule` survives a grep.
- [ ] The select chevron is updated **twice**. It is a `background-image`
      carrying a hard-coded `--muted` literal (`%2355655b`, `styles.css:998`),
      pinned by a Decisions Log row precisely so a palette change has to come
      back and say so. Light becomes `%23475569`. **Dark needs its own
      declaration** (`%23b7bec9`) — a `var()` cannot be interpolated into a
      `url()`, so the dark blocks must restate the whole rule or every
      `<select>` gets a near-invisible slate chevron on a near-black field.
      This is the one place dark mode costs a duplicated rule rather than a
      duplicated token.
- [ ] Drift-pinned `PILL_TONES` tests pass unchanged. The vocabulary did not
      move; only the hexes did.
- [ ] `DESIGN.md` has its dated "Arca direction — step 1" section and its
      Decisions Log rows.
