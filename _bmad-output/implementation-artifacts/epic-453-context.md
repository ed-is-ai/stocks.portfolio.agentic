# Epic 453 Context: Make the Portfolio Dashboard Clear at a Glance

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Give the selected account a focused Portfolio dashboard that makes its current
financial position and next actions immediately understandable, then presents
a trustworthy history of total portfolio value. The dashboard must combine
cash and invested holdings without changing authoritative valuations, while
preserving the existing account workflows and efficient chart-range behavior.

## Stories

- Story 1.1 / GH-454: Reframe the Portfolio Dashboard Around Current Position
- Story 1.2 / GH-455: Plot Total Portfolio Value From Cash and Stocks

## Requirements & Constraints

- Present the selected account heading, price-freshness/reporting context, and
  the four authoritative summary metrics before historical detail: Market
  Value, Total Cost, Unrealised P&L, and Cash. Preserve existing calculations,
  currency formatting, tabular numerals, and explicit P&L signs.
- Treat Account and Strategy as one coherent context group. Recommendations is
  the primary action, Add holding is secondary, and Refresh prices is tertiary;
  existing endpoints, authorization, and behavior must not change.
- Plot a named `Portfolio Value` series for each valid snapshot as that
  snapshot's market value plus cash balance. Market Value, Cost Basis, and Cash
  remain available as supporting series without obscuring the total.
- Never silently treat a missing or invalid chart component as zero. Mark the
  total value unavailable or omit the affected point consistently with the
  existing chart-data contract, without mutating stored snapshots.
- Preserve account scoping and the existing `1M / 3M / 12M / 3Y / 5Y`
  behavior: per-browser preference, default `12M`, server-side filtering and
  chronological downsampling to at most 250 points, the 20,000-row history
  safety ceiling, in-window trade markers, empty-range recovery, and chart-only
  refresh.
- Keep range changes lean: they must not rebuild holdings, price, or cash
  context. Repeated chart swaps must destroy the prior Chart.js instance before
  creating another.
- Retain all existing portfolio, holdings, history, import, cash-activity,
  price-refresh, and trade-marker workflows.
- Status and financial meaning must not depend on color alone. Labels, explicit
  signs, line weight/dash treatment, visible focus, and factual fallback copy
  must remain understandable in grayscale and forced-colors modes.
- At 320 CSS pixels, 200% text resize, and 400% zoom, controls and summary
  content must reflow in reading order without clipped text or page-level
  two-axis scrolling. Only the chart or table may use contained horizontal
  overflow when necessary.
- Regression coverage must include service-owned arithmetic, unavailable
  components, chart labels and datasets, fragment replacement, range
  persistence/filtering, account switching, and existing portfolio/chart
  interactions. Run the repository's full test and quality checks after
  focused tests.

## Technical Decisions

- Stay within the existing FastAPI, Jinja2, htmx, Bootstrap 5, and Chart.js
  4.4.4 stack; do not introduce a SPA or another charting library.
- Preserve the layered boundary: routes assemble presentation through services,
  services orchestrate authoritative data, and repositories own persistence.
  Templates and browser JavaScript must not read persistence or recompute
  financial totals.
- Define one service-owned chart projection for total portfolio value. Market
  value and cash for a point must come from the same portfolio-scoped snapshot
  and use deterministic financial arithmetic rather than browser-side addition.
- Reuse the existing chart context and chart partial. The range action continues
  to replace only the stable `#portfolio-chart-card` shell.
- Keep snapshot storage, trade history, cash ledgers, and valuation semantics
  unchanged. The total series is a presentation projection over retained
  snapshot facts.
- Preserve localStorage failure tolerance and the global chart-instance teardown
  contract across htmx swaps.

## UX & Interaction Patterns

- Use a centered, bounded dashboard container of approximately 1440–1600 CSS
  pixels on wide displays, with compact vertical spacing so Holdings begins
  within or immediately after the first desktop viewport where practical.
- Start with a compact selected-account heading and freshness line, followed by
  aligned context controls and a position-led action hierarchy. Place four flat
  bordered summary cards before the chart in the required order.
- Give the chart a balanced desktop height of approximately 320–380 CSS pixels,
  a concise title and legend, and sufficient spacing between axes, labels, and
  the range selector.
- Make `Portfolio Value` visually dominant while keeping supporting series
  distinguishable through names and non-color-only line treatments. Preserve
  Buy and Sell marker behavior.
- Keep the range selector visible and keyboard operable even when too few points
  exist to draw the chart, and expose its selected state programmatically.
- Reuse the established Portfolio visual language: dark-navy chrome, white
  bordered surfaces, Inter and numeric typography, semantic colors, spacing,
  radii, focus treatment, Bootstrap grid, and contained table wrapping.

## Cross-Story Dependencies

- Story 1.1 establishes the shared route, context, dashboard shell, summary
  placement, responsive layout, and action hierarchy that Story 1.2 extends
  with the total-value projection and dominant chart series.
- Both stories depend on the already-delivered selectable-range contract and
  must preserve its server-side windowing/downsampling, browser preference,
  marker filtering, empty state, and chart-only htmx refresh.
