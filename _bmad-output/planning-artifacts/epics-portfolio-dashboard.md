---
stepsCompleted: [1, 2, 3]
inputDocuments:
  - _bmad-output/planning-artifacts/ux-designs/ux-Agents.stocks-2026-08-08/DESIGN.md
  - _bmad-output/planning-artifacts/ux-designs/ux-Agents.stocks-2026-08-08/EXPERIENCE.md
  - _bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md
  - _bmad-output/implementation-artifacts/spec-gh-421-portfolio-chart-range.md
  - approved portfolio-dashboard mockup and conversation requirements (2026-08-31)
---

# Agents.stocks Portfolio Dashboard - Epic Breakdown

## Overview

This document provides the epic and story breakdown for improving the live
Portfolio dashboard's information hierarchy and adding an explicit total
portfolio value series to its history chart. It extends the existing
server-rendered Portfolio surface and preserves the delivered chart-range
behavior from GitHub issue #421.

## GitHub Tracking

- Epic: #453 — Clarify the portfolio dashboard and show total portfolio value
- Story 1.1: #454 — Portfolio dashboard layout and action hierarchy
- Story 1.2: #455 — Total portfolio value chart series

## Requirements Inventory

### Functional Requirements

FR1: Edyau can understand the selected account's current financial position at
a glance through a compact Portfolio header, freshness/context information,
and summary metrics presented before historical detail.

FR2: The Portfolio dashboard presents Market Value, Total Cost, Unrealised P&L,
and Cash as a consistent summary row using the selected account's existing
authoritative values and currency formatting.

FR3: Account and Strategy context controls remain available as one coherent
control group, while Recommendations is the primary workflow action, Add
holding is secondary, and Refresh prices is tertiary.

FR4: The Portfolio value history chart exposes a named total portfolio value
series for every plotted snapshot, calculated as stock market value plus cash
for that same snapshot.

FR5: Market value, cost basis, and cash remain available as supporting chart
context without obscuring the total portfolio value series or changing their
underlying meanings.

FR6: The chart retains the delivered `1M / 3M / 12M / 3Y / 5Y` selector,
per-browser range persistence, server-side time filtering/downsampling,
in-window trade markers, empty-range recovery, and chart-only htmx refresh.

### NonFunctional Requirements

NFR1: The change remains within the existing FastAPI, Jinja2, htmx, Bootstrap
5, and Chart.js 4.4.4 stack; it introduces no SPA framework or charting
library.

NFR2: The total portfolio series is derived deterministically from the same
portfolio-scoped snapshot rows used by the existing chart, with no mutation of
snapshot storage, trade history, cash ledgers, or valuation semantics.

NFR3: Range changes continue to render at most 250 chronologically ordered
points through the existing lean chart fragment and must not rebuild holdings,
prices, or cash context.

NFR4: Existing portfolio/account selection, Recommendations, holding,
price-refresh, range-selection, and trade-marker behavior must remain
regression-tested.

NFR5: Status and financial meaning cannot rely on color alone; signed values,
series names, line styles, focus states, and accessible fallback text must
remain understandable in grayscale and forced-colors contexts.

NFR6: At 320 CSS pixels, 200% text resize, and 400% zoom, non-data controls and
summary content reflow without page-level two-axis scrolling; only the chart or
table may use contained horizontal overflow when genuinely necessary.

### Additional Requirements

- Preserve the brownfield layering: Portfolio routes build presentation
  context through services and existing repositories rather than reading or
  recomputing persistence data inside templates.
- Reuse `chart_fragment_context` and `_portfolio_chart.html`; the range action
  must continue to swap the stable `#portfolio-chart-card` shell only.
- Preserve the existing `PortfolioSnapshotsRepository.history(..., since=...)`
  time-window contract, 20,000-row safety ceiling, and `<= 250` point
  downsampling behavior.
- Define one service-owned chart projection for total portfolio value. Do not
  duplicate cash-plus-market arithmetic in Jinja or JavaScript.
- Each total-value point must use market value and cash from the same snapshot.
  Missing or invalid components must produce an explicit unavailable value or
  omit the affected point according to the existing chart-data contract; the
  implementation must never silently substitute zero.
- Retain `window.__portfolioChart` teardown before every Chart.js recreation so
  htmx swaps cannot leak or collide with a prior chart instance.
- Preserve localStorage failure tolerance and the default `12M` range.
- Add focused service/template/route tests for total-series arithmetic,
  labels/datasets, fragment behavior, missing components, and retained range
  behavior, followed by the repository's full test and quality checks.

### UX Design Requirements

UX-DR1: On wide displays, the Portfolio content uses a centered, bounded
container (approximately 1440-1600 CSS pixels maximum) instead of stretching
controls and data across the full viewport.

UX-DR2: The page begins with a compact `SIPP Portfolio`-style heading for the
selected account and a nearby price-freshness/reporting context line; it avoids
large empty bands between navigation, controls, and content.

UX-DR3: Account and Strategy selectors read as one labelled context group with
clear alignment and spacing, while action hierarchy is visible through
position, styling, and labels rather than color alone.

UX-DR4: Recommendations is visually primary; Add holding is secondary; Refresh
prices is a quieter tertiary action. Existing authorization and interaction
behavior remains unchanged.

UX-DR5: Four flat bordered summary cards appear before the chart in this order:
Market Value, Total Cost, Unrealised P&L, and Cash. Monetary figures use the
existing tabular numeric typography and explicit signs where applicable.

UX-DR6: The history chart has a balanced readable height (target approximately
320-380 CSS pixels on desktop), a concise title and legend, and enough internal
spacing that axes, series labels, and the range selector do not compete.

UX-DR7: `Portfolio Value` (cash plus stocks) is the dominant named line.
Supporting Market Value, Cost Basis, and Cash series use visually subordinate
and non-color-only distinctions such as line weight or dash patterns.

UX-DR8: The range selector remains keyboard operable, exposes its selected
state programmatically, and stays visible when the selected range has too few
points to draw a chart.

UX-DR9: Holdings content begins within or immediately after the first desktop
viewport where practical; dashboard framing must not create excessive
whitespace that pushes the primary table out of sight.

UX-DR10: The dashboard reuses the existing dark-navy chrome, white bordered
surfaces, Inter typography, numeric font, semantic colors, spacing, radii,
focus treatment, Bootstrap grid, and `.tbl-wrap`; it introduces no separate
Portfolio visual brand.

UX-DR11: Compact and narrow layouts stack summary cards and action groups in a
predictable reading order, wrap long labels, preserve 24x24 CSS-pixel pointer
targets or the WCAG spacing exception, and keep focus visible.

UX-DR12: Empty, partial, or unavailable chart data is stated in terse factual
copy. A chart-data problem does not remove the selector or imply that the
portfolio itself is empty.

### FR Coverage Map

FR1: Epic 1 - Give the selected account a compact, legible portfolio overview.

FR2: Epic 1 - Surface the four authoritative financial metrics before
historical detail.

FR3: Epic 1 - Make account/strategy context and action priority coherent.

FR4: Epic 1 - Show total portfolio value as stock market value plus cash at
each snapshot.

FR5: Epic 1 - Retain market value, cost basis, and cash as supporting chart
context.

FR6: Epic 1 - Preserve the existing selectable-range and lean chart-refresh
contract.

## Epic List

### Epic 1: Make the Portfolio Dashboard Clear at a Glance

Edyau can open a focused, readable account dashboard that puts current
financial position, clear actions, and a trustworthy total portfolio value
history ahead of secondary detail, while retaining the existing chart controls
and portfolio workflows.

**FRs covered:** FR1, FR2, FR3, FR4, FR5, FR6

**Implementation notes:** This is one end-to-end dashboard epic because both
outcomes use the same Portfolio route, context service, chart partial, and
template. Deliver it as two ordered stories: the layout and interaction
hierarchy first, then the service-owned cash-plus-stocks series. Preserve the
existing FastAPI/Jinja/htmx/Chart.js stack, #421 time-window/downsampling
contract, account scope, and chart-only fragment refresh.

## Epic 1: Make the Portfolio Dashboard Clear at a Glance

Edyau can open a focused, readable account dashboard that puts current
financial position, clear actions, and a trustworthy total portfolio value
history ahead of secondary detail, while retaining the existing chart controls
and portfolio workflows.

### Story 1.1: Reframe the Portfolio Dashboard Around Current Position

As a portfolio owner,
I want the Portfolio tab to present account context, important actions, and my
current position in a coherent visual hierarchy,
So that I can understand and act on my portfolio without scanning a stretched,
cluttered page.

**Acceptance Criteria:**

**Given** I open the Portfolio tab for an existing account on a desktop or
laptop display
**When** the partial renders
**Then** its content is centered within a bounded dashboard container and
starts with a compact selected-account heading plus price-freshness/reporting
context
**And** it does not add large empty vertical bands before the dashboard's
primary content.

**Given** I need to change portfolio context or take an action
**When** I view the dashboard controls
**Then** Account and Strategy controls form one aligned context group
**And** Recommendations is visibly primary, Add holding is secondary, and
Refresh prices is tertiary without changing their existing endpoints,
authorization, or behavior.

**Given** the selected account has holdings or a recorded cash balance
**When** its summary renders
**Then** Market Value, Total Cost, Unrealised P&L, and Cash appear before the
history chart as four consistently styled cards in that order
**And** values retain existing authoritative calculations, tabular monetary
formatting, and explicit positive/negative signs.

**Given** the account has chart history and holdings
**When** the dashboard is viewed at a typical desktop height
**Then** the chart has a readable balanced height and Holdings begins in or
immediately after the first viewport
**And** existing history, import, cash activity, and table workflows remain
available.

**Given** I use keyboard navigation, 200% text resize, 400% zoom, a 320 CSS
pixel viewport, or forced-colors mode
**When** the dashboard renders
**Then** controls and summary content reflow in reading order without
page-level two-axis scrolling or clipped text
**And** focus, labels, action hierarchy, P&L signs, and statuses remain
understandable without color alone.

### Story 1.2: Plot Total Portfolio Value From Cash and Stocks

As a portfolio owner,
I want the Portfolio history chart to show my combined cash and stock value,
So that its main trend reflects the value of my whole account rather than only
the invested holdings.

**Acceptance Criteria:**

**Given** a selected portfolio has two or more retained historical snapshots
with market value and cash balance
**When** its history chart renders
**Then** it contains a dominant line named `Portfolio Value`
**And** each plotted value equals that snapshot's market value plus its cash
balance, calculated once in the Portfolio service projection rather than in
Jinja or browser JavaScript.

**Given** the chart renders Portfolio Value
**When** I inspect the legend, tooltip, or series distinctions
**Then** Market Value, Cost Basis, and Cash remain available as supporting
context
**And** Portfolio Value remains distinguishable without color alone through
series label and line treatment, while Buy and Sell markers retain their
existing behavior.

**Given** a snapshot has no valid cash balance or market value component
**When** chart data is prepared
**Then** that Portfolio Value point is explicitly unavailable according to the
chart-data contract
**And** the implementation never silently treats the missing component as zero
or changes persisted snapshot data.

**Given** I select any supported chart range or switch accounts
**When** the chart refreshes
**Then** Portfolio Value uses the same selected, portfolio-scoped, chronologically
ordered and server-downsampled snapshot set as the supporting series
**And** the `1M / 3M / 12M / 3Y / 5Y` choice, localStorage preference,
out-of-window marker filtering, no-data state, and chart-only htmx swap remain
unchanged.

**Given** the chart fragment is swapped repeatedly
**When** Chart.js is recreated or the selected range has insufficient points
**Then** the previous chart instance is destroyed before replacement
**And** the chart shell and keyboard-operable range selector remain available.

**Given** the implementation is complete
**When** focused service, route, template, and regression tests run
**Then** they cover total-value arithmetic, missing component handling,
datasets/labels, retained range behavior, and existing account/chart workflows
**And** the repository test and quality suites report no new failures.
