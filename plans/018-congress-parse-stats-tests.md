# Plan 018: Pin the congressional-trading HTML parser with fixture tests

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 13074c8..HEAD -- app/integrations/congress.py`
> If `congress.py` changed since this plan was written, compare the "Current
> state" excerpts against the live code before proceeding; on a mismatch, treat
> it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `13074c8`, 2026-06-20

## Why this matters

`CongressClient._parse_stats` scrapes QuiverQuant HTML to count Buy/Sell
transactions by Congress members, feeding the analyst's congressional-trading
signal. It is a brittle regex-over-HTML parser with several silent-skip branches
(no chamber match, no type match, no date, unparseable date, outside the 12-month
window). It has **zero coverage**: if the site markup shifts, the parser quietly
returns all-zero stats and nothing flags it. This plan pins the current parsing
behavior against a synthetic HTML fixture so a markup-shape change surfaces as a
test failure rather than a silently dead signal. It is pure string→dataclass
logic — no network.

## Current state

File: `app/integrations/congress.py`. The relevant module-level regexes and the
method under test:

```python
_LOOKBACK_DAYS = 365

_CHAMBER_RE = re.compile(r"congresstrading/trade/(Senate|House)-")
_TYPE_RE = re.compile(r"<span[^>]*>\s*(Purchase|Sale|Exchange)\b")
_DATE_RE = re.compile(r"([A-Z][a-z]+ \d+, \d{4})")


@dataclass
class CongressStats:
    buys: int = 0
    sells: int = 0
    senate_buys: int = 0
    senate_sells: int = 0


class CongressClient:
    def _parse_stats(self, html: str) -> CongressStats:
        """Parse buy/sell counts by chamber from HTML table rows."""
        cutoff = date.today() - timedelta(days=_LOOKBACK_DAYS)
        counts: Counter[str] = Counter()

        for row in html.split("<tr>"):
            m_chamber = _CHAMBER_RE.search(row)
            if not m_chamber:
                continue
            chamber = m_chamber.group(1)

            m_type = _TYPE_RE.search(row)
            if not m_type:
                continue
            txn_type = m_type.group(1).strip()

            # Last date match in row is the Traded date (5th column)
            dates = _DATE_RE.findall(row)
            if not dates:
                continue
            try:
                txn_date = datetime.strptime(dates[-1], "%b %d, %Y").date()
            except ValueError:
                continue

            if txn_date < cutoff:
                continue

            counts[f"{chamber}_{txn_type}"] += 1

        return CongressStats(
            buys=counts["House_Purchase"] + counts["Senate_Purchase"],
            sells=counts["House_Sale"] + counts["Senate_Sale"],
            senate_buys=counts["Senate_Purchase"],
            senate_sells=counts["Senate_Sale"],
        )
```

Facts the tests rely on:
- The parser splits on the literal `<tr>` token. Each "row" must contain, to be
  counted: a `congresstrading/trade/(Senate|House)-...` substring, a
  `<span ...>Purchase|Sale|Exchange` substring, and at least one date matching
  `[A-Z][a-z]+ \d+, \d{4}` (e.g. `Jan 5, 2024`). The **last** date in the row is
  used as the traded date.
- `buys` counts `Purchase` rows; `sells` counts `Sale` rows. `Exchange` is
  matched by `_TYPE_RE` but is **not** added to buys/sells/senate_* (it only
  lands in the `Counter` under e.g. `House_Exchange`, which the returned
  `CongressStats` ignores). Assert this current behavior.
- Dates older than `today - 365 days` are skipped.
- `cutoff` uses `date.today()` at call time. To stay deterministic, **build the
  fixture dates relative to `date.today()`** at test time using
  `date.strftime("%b %d, %Y")` — do NOT hardcode calendar dates.
- `CongressClient()` constructs a `requests.Session` but makes **no** network
  call; instantiating it in a test is safe. `_parse_stats` never touches the
  network.

### Test conventions in this repo (match these)

- `tests/test_<module>.py`, plain pytest. See `tests/test_exit_evaluator.py` for
  the helper-builder + focused-assert style.
- Keep fixtures small and inline — build HTML rows with an f-string helper rather
  than checking in a large HTML file.

## Commands you will need

| Purpose   | Command                                       | Expected on success |
|-----------|-----------------------------------------------|---------------------|
| Run new tests | `uv run pytest tests/test_congress.py -q` | all pass            |
| Full suite | `uv run pytest -q`                           | all pass (was 98)   |
| Typecheck | `uv run pyrefly check`                         | exit 0              |
| Lint      | `uv run ruff check tests/test_congress.py`    | exit 0              |
| Format    | `uv run ruff format tests/test_congress.py`   | reformats, exit 0   |

## Scope

**In scope** (the only files you should create/modify):
- `tests/test_congress.py` (create)
- `plans/README.md` (status row only)

**Out of scope** (do NOT touch):
- `app/integrations/congress.py` — characterization only. If a test reveals a
  likely bug (e.g. the `Exchange` handling), assert the **actual current**
  behavior, add a `# NOTE:`, and report it.
- `_fetch_html`, `get_stats`, `get_congress_buys` and the network/caching path —
  not in scope. Only `_parse_stats`.

## Git workflow

- Branch: `advisor/018-congress-parse-stats-tests`
- Conventional-commit style, e.g.
  `test(integrations): characterize congress HTML parser`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Create the test file and an HTML-row builder

Create `tests/test_congress.py`:

```python
"""Unit tests for CongressClient._parse_stats (pure HTML parsing)."""

from __future__ import annotations

from datetime import date, timedelta

from app.integrations.congress import CongressClient, CongressStats


def _row(chamber: str, txn_type: str, when: date) -> str:
    """Build one QuiverQuant-style table row the parser will recognize."""
    when_str = when.strftime("%b %d, %Y")
    return (
        "<tr>"
        f'<a href="/congresstrading/trade/{chamber}-Smith">link</a>'
        f"<span class='x'>{txn_type}</span>"
        f"<td>{when_str}</td>"
    )


def _html(*rows: str) -> str:
    return "<table>" + "".join(rows) + "</table>"


def _recent() -> date:
    return date.today() - timedelta(days=30)


def _stale() -> date:
    return date.today() - timedelta(days=400)
```

**Verify**: `uv run pytest tests/test_congress.py -q`
→ collects 0 tests, exits 0.

### Step 2: Test counting by chamber and type

Add tests (instantiate `client = CongressClient()` in each):
- **House purchase + sale within window**: one `_row("House", "Purchase", _recent())`
  and one `_row("House", "Sale", _recent())` → `buys == 1`, `sells == 1`,
  `senate_buys == 0`, `senate_sells == 0`.
- **Senate purchase + sale**: `senate_buys == 1`, `senate_sells == 1`, and these
  also roll up into `buys`/`sells` (so two Senate rows → `buys == 1`,
  `sells == 1`, `senate_buys == 1`, `senate_sells == 1`).
- **Mixed House + Senate**: assert the aggregate `buys`/`sells` sum across both
  chambers while `senate_*` counts only the Senate rows.

**Verify**: `uv run pytest tests/test_congress.py -q -k "chamber or count or senate"`
→ all pass.

### Step 3: Test the skip branches

- **Stale date excluded**: a `Purchase` row with `_stale()` → `buys == 0`.
- **No chamber link → skipped**: a row with a `<span>Purchase</span>` and a date
  but no `congresstrading/trade/...` substring → all zeros.
- **No type span → skipped**: a row with chamber + date but no
  `<span>...Purchase</span>` → all zeros.
- **No date → skipped**: a row with chamber + type but no date string → all zeros.
- **Unparseable date → skipped**: a row whose only date-like text fails
  `strptime("%b %d, %Y")` (e.g. inject `Zzz 99, 9999` — it matches the regex
  shape `[A-Z][a-z]+ \d+, \d{4}` but `strptime` raises `ValueError`) → that row
  is skipped. Confirm a sibling valid row in the same HTML is still counted.
- **Empty HTML → all zeros**: `_parse_stats("")` returns
  `CongressStats(0, 0, 0, 0)`.

**Verify**: `uv run pytest tests/test_congress.py -q -k skip`
→ all pass.

### Step 4: Pin the `Exchange` behavior (current quirk)

Add a test: a single `_row("House", "Exchange", _recent())` → `buys == 0`,
`sells == 0` (the parser matches `Exchange` as a type but the returned
`CongressStats` ignores it). Add a `# NOTE:` comment that this documents current
behavior, not necessarily intended behavior.

**Verify**: `uv run pytest tests/test_congress.py -q -k exchange`
→ all pass.

### Step 5: Format, lint, typecheck, full suite

- `uv run ruff format tests/test_congress.py`
- `uv run ruff check tests/test_congress.py` → exit 0
- `uv run pyrefly check` → exit 0
- `uv run pytest -q` → full suite green

Then set this plan's row in `plans/README.md` to DONE.

## Test plan

- New file `tests/test_congress.py` covering: House/Senate purchase+sale counts,
  Senate roll-up into totals, mixed chambers, stale-date exclusion, and each
  skip branch (no chamber / no type / no date / unparseable date / empty), plus
  the `Exchange` quirk.
- Structural pattern: `tests/test_exit_evaluator.py` (builders + focused asserts).
- Verification: `uv run pytest tests/test_congress.py -q` → all pass;
  `uv run pytest -q` → still green.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uv run pytest tests/test_congress.py -q` passes with ≥10 new tests
- [ ] `uv run pytest -q` exits 0 (no regression in the existing 98)
- [ ] `uv run pyrefly check` exits 0
- [ ] `uv run ruff check tests/test_congress.py` exits 0
- [ ] `git status` shows only `tests/test_congress.py` and `plans/README.md` changed
- [ ] `plans/README.md` status row for plan 018 updated to DONE

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `congress.py` changed since `13074c8` and the regexes,
  the `_parse_stats` body, or `CongressStats` no longer match the excerpts.
- Constructing `CongressClient()` triggers a network call or hangs (it should
  not) — report it.
- A characterization test fails in a way that looks like a real source bug —
  leave the test asserting actual behavior with a `# NOTE:` and report it.

## Maintenance notes

- This pins parsing against synthetic HTML, not the real QuiverQuant page. If the
  real markup changes, these tests stay green but production returns zeros — pair
  this with a periodic live smoke check if the signal becomes load-bearing.
- A reviewer should confirm fixture dates are built relative to `date.today()`
  (so the suite doesn't rot) and that no test reaches the network.
- Deferred: `_fetch_html`/`get_stats` caching and the live HTTP path — out of
  scope here.
