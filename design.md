# Design — Studied DNA: Bloomberg UK homepage

Locked design system. Future Hallmark runs read this file first; pages defer
to it. Amend intentionally — the file is the rule.

## System
- Genre · editorial (dense-utilitarian variant)
- Macrostructure · Index-First
- Theme · studied-DNA (source: image — see Provenance)
- Axes · light / grotesk-sans (weight-only, no serif) / neutral-decorative + functional dual-tone data color (see Notes)

## Macrostructure family (app pages)
This project is 100% app pages — no marketing or content pages exist (7 tabs:
Watchlist, Portfolio, Trade History, Realised P&L, Run Log, Strategy Manager,
Settings). One family covers all of them:

- App pages · Index-First, built from **F3 Tabular spec sheet** for
  table-heavy views (Watchlist, Portfolio, Trade History, Realised P&L,
  Run Log) and dense activity-card patterns for status/activity views
  (Strategy Manager). Nav: the existing top bar (wordmark-left + horizontal
  tab row) is already **N1b**-shaped structurally — restyle onto the new
  tokens, don't restructure. Footer: none — authenticated internal tool,
  no marketing sitemap to index.

## Per-page allowances
- No page may use hero enrichment (E1-E8) — function carries every page.
- Existing HTMX wiring, Bootstrap JS (modals/dropdowns — see Notes), and
  interaction/data logic are out of scope for every page. Only the CSS
  framework (Bootstrap's stylesheet) and visual language get replaced.
- Semantic/functional data coloring (green=up/buy, red=down/sell,
  amber=caution, blue=info) is shared infrastructure — do not invent a new
  color per page; extend the existing `--green`/`--red`/`--amber`/`--blue`
  tokens below.

## What pages MUST share
- The retokenized `:root` custom properties below (same variable *names*
  the app already used pre-redesign: `--primary`, `--border`, `--muted`,
  `--green`, `--red`, `--amber`, `--blue`, etc. — only their *values*
  change). This is what makes the redesign safe to roll out incrementally:
  every template already consumes these names via `var(--x)`.
- The masthead/tab-bar nav shape and voice.
- Flat, non-rounded card/table/button geometry (0px radius on structural
  containers) and hairline-rule section dividers.
- Weight-only type hierarchy (bold Inter for display/labels, regular Inter
  for body — no serif introduced anywhere).

## What pages MAY differ on
- Table vs. activity-card layout, per the page's actual content shape.
- Column density / count (each page's data is different).

## Provenance
- Source mode · image (two screenshots, user-attached — an earlier URL-mode
  fetch of bloomberg.com was blocked with HTTP 403, so this DNA comes from
  the attached captures, not live CSS)
- Source · Bloomberg UK homepage (bloomberg.com/uk) — public reference site,
  not the user's own brand
- Date extracted · 2026-08-24
- Confidence · Tokens below are **estimated** from source-image colour bands
  (image mode), not exact values. Fonts are **role-based candidates** from
  the Hallmark canon, not confirmed typeface names. Rhythm (density,
  asymmetry) is **directly observed** from the screenshots — high
  confidence: dense, hairline-divided, left-biased asymmetric.

## Tokens (canonical · `tokens.css` is the source of truth)
```css
:root {
  --color-paper:      oklch(98% 0.005 90);   /* white page background */
  --color-paper-2:    oklch(16% 0 0);        /* masthead / ticker / footer band */
  --color-ink:        oklch(20% 0 0);        /* headlines, primary text */
  --color-ink-2:      oklch(45% 0.005 90);   /* deks, secondary text */
  --color-rule:       oklch(90% 0.005 90);   /* hairline dividers between blocks */
  --color-accent:     oklch(20% 0 0);        /* no decorative accent in source — reuses ink */
  --color-accent-ink: oklch(98% 0 0);        /* text on dark/accent chips */
  --color-focus:      oklch(60% 0.15 250);   /* not observed in source — Hallmark default */

  /* Functional data-color layer — NOT a decorative accent, do not merge
     into --color-accent. See Notes. */
  --color-data-up:    oklch(55% 0.15 145);   /* price-up / positive delta */
  --color-data-down:  oklch(55% 0.20 25);    /* price-down / live indicator */

  --font-display: "Inter", system-ui, sans-serif;   /* app already self-hosts Inter — reuse it, don't add Inter Tight */
  --font-body:    "Inter", system-ui, sans-serif;   /* same family, weight does the work (matches source's weight-only hierarchy) */
  --font-mono:    "Inter", ui-monospace, monospace; /* app's existing --font-num stack; no mono introduced */

  /* 4-pt spacing scale, named: --space-3xs … --space-4xl. See tokens.css.   */
  /* Type scale, 1.25 (major-third) ratio: --text-xs … --text-display.       */

  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --dur-fast: 180ms;  --dur-base: 240ms;  --dur-slow: 320ms;

  --radius-card: 0px;   /* flat rectangular cards, no rounding observed */
  --radius-pill: 0px;   /* no pill shapes observed */
  --radius-input: 4px;  /* not observed — small default */
}
```

## Implementation mapping (this codebase)
Rather than introduce Hallmark's `--color-*` names alongside the app's own,
`app/api/static/css/tokens.css` retokenizes the app's **existing** `:root`
variable names in place — every template already consumes them via
`var(--x)`, so this is what makes an incremental page-by-page rollout safe:

| Hallmark canonical | App variable | New value (was) |
| --- | --- | --- |
| `--color-paper` | `--surface`, body bg | `#f8f9fa` flat (was radial-gradient teal wash) |
| `--color-paper-2` | `--primary` (masthead bg) | `#111418` near-black (was navy gradient `#0f2744`→`#123a63`) |
| `--color-ink` | `--primary` (text/heading use) | `#111418` |
| `--color-ink-2` | `--muted` | `#5b6470` |
| `--color-rule` | `--border` | `#dfe3e8` |
| `--color-accent` | `--accent` | `#111418` (was `#10b981` emerald — no decorative accent in source, see Notes) |
| — | `--accent-2` (focus rings, kept) | `#2563eb` (was cyan `#22d3ee` — needs ≥3:1 contrast on white) |
| `--color-data-up` | `--green` | `#0a7d34` (was `#16a34a` — slightly deepened for flat-background contrast) |
| `--color-data-down` | `--red` | `#c81e1e` (was `#dc2626`) |
| — | `--amber`, `--blue`, `--purple`, `--orange` | unchanged hues, flattened backgrounds (see tokens.css) |
| `--radius-card` / `--radius-input` | new `--radius-sm` (badges/inputs only) | `4px` — structural containers (`.navbar`, `.stat-card`, `.tbl-wrap`, buttons) go to `0` |

**Status: all 7 tabs converted.** Shell (navbar/tab-bar), Watchlist, and the
remaining 6 tabs (Portfolio, Trade History, Realised P&L, Run Log, Strategy
Manager, Settings) are all on the new system. Two mechanisms did the work:
(1) the retokenized `:root` values cascade through every template via the
existing `var(--x)` names, and (2) overriding Bootstrap's own
`--bs-border-radius*` variables in `tokens.css` flattened every unconverted
page's native `.card`/`.badge`/`.btn`/`.alert`/`.dropdown-menu` sitewide
without touching their markup. A handful of orphaned hardcoded hex values
(old navy/emerald brand colors, stale badge colors) were swept to `var()`
references in `theme.css`, `watchlist.css`, and 4 templates. Bootstrap's
CSS `<link>` and JS bundle both remain loaded — removing the CDN CSS link
is future work once every template's Bootstrap *utility* classes (grid,
`d-flex`, spacing, etc. — not just component styling) are also converted.

## CTA voice
- Primary · solid fill (near-black on white masthead → white-on-black in
  context, e.g. "Subscribe") · 0px radius · tight padding, no pill shape
- Secondary · ghost text link, no border (e.g. "Sign In") · same 0px radius

## Motion stance
- motion-cut — nothing observed in either capture (static homepage, no
  visible transitions/reveals)
- Reduced-motion fallback · ≤150 ms opacity crossfade (Hallmark default;
  not exercised by the source)

## Notes — carry forward with care
- **Ft3 footer is normally an AI-fingerprint tell** (Hallmark's own
  anti-patterns flag a 4-5 column link footer as one of the most
  recognizable slop signals). On this source it's *earned* — Bloomberg
  genuinely has 5 top-level destinations (Home/News/Work & Life/Market
  Data/Explore). Only reuse Ft3 when a rebuild has that many genuine
  sections; don't reach for it by default just because this DNA has it.
- **No single decorative accent hue.** The only chromatic color in the
  source is functional: green = price-up, red = price-down/live. Don't
  invent a decorative brand accent to satisfy Hallmark's usual
  single-accent-hue expectation — keep `--color-data-up` /
  `--color-data-down` as a separate functional layer, and let
  `--color-accent` stay neutral (ink-based) for interactive/focus purposes.
- **Weight-only type hierarchy is intentional**, not an oversight — one
  grotesque family (bold for display/labels, regular for body), no serif
  anywhere. Don't pair in an editorial serif when building with this DNA;
  that fights the source's utilitarian-newsroom identity.
- **No section eyebrows/kickers.** Section labels ("Stories for You",
  "Latest News", "In Focus") sit directly above their content with no tag
  treatment. Carry that restraint forward.
- **F6 Product-card-grid is reused at three densities** (2-col+dek,
  4-col+dek, 4-col no-dek) rather than three different archetypes — the
  source's actual discipline is one card grammar, varied by column count
  and caption presence, not archetype variety.
- **Bootstrap's JS bundle stays.** This app's Buy/Sell/Adjust trade modals
  and the notification dropdown are driven by Bootstrap's JS
  (`bootstrap.Modal`, `data-bs-toggle="dropdown"`), not just its CSS.
  Per explicit user decision, only Bootstrap's *stylesheet* is being
  replaced — its JS component behavior (open/close/focus-trap/backdrop)
  stays wired exactly as-is. Do not touch `bootstrap.bundle.min.js` or the
  `data-bs-*` attributes on modals/dropdowns in any future page conversion
  under this system.
- **Bootstrap's CSS/CDN link stays loaded for now.** 32 of 33 templates
  still depend on it structurally (grid utilities, `.btn`, `.card`,
  `.alert`, etc.). Only remove the `bootstrap.min.css` `<link>` once every
  template has been converted off Bootstrap classes — removing it earlier
  breaks every unconverted tab immediately.
- **Nav chrome was undersized relative to table content.** `.navbar-brand`
  (16px) and `.nav-tabs .nav-link` (13.12px) rendered barely larger than
  regular table body text (13.12px) and only slightly above 11px badges —
  no real hierarchy between "chrome you navigate with" and "dense data
  you're scanning." Fixed in `index.html`: brand → `--fs-lg` (18.4px),
  `.navbar-tag` → `--fs-sm` (13.12px), tabs → `--fs-md` (16px). Table/badge
  sizes are untouched — dense micro-text there is expected of the DNA.
- **Heading scale overridden sitewide.** Bootstrap's own h1-h6/.h1-.h6
  defaults (calc()-based, ~16px-40px, responsive-vw) were completely
  disconnected from the app's `--fs-*` badge/label scale — this is what
  produced the "font sizes don't look right" complaint (huge page titles
  sitting directly above 11-13px labels with nothing in between). Fixed by
  extending `--fs-*` upward (`--fs-2xl: 1.75rem`, `--fs-display: 2.25rem`)
  and overriding `h1-h6`/`.h1-.h6` in `theme.css` to snap onto that one
  scale, weight-only (bold/semibold Inter, no size is decorative-huge) —
  consistent with the studied DNA. This is sitewide (theme.css is shared),
  so it applies even to the 25+ raw `<h2>`/`.h5`/`.h3` headings across
  pages that otherwise weren't touched individually.

## Exports
`tokens.css` (in this project) is the source of truth. For Tailwind v4
`@theme`, DTCG `tokens.json`, or shadcn/ui CSS variables, ask *"extend
design.md with Tailwind exports"* (or the format you want) — Hallmark will
append them per `export-formats.md`.
