# Plan 019: Cover the scan-history transition-detection logic with tests

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 13074c8..HEAD -- app/agents/scanner/scan_history.py`
> If `scan_history.py` changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `13074c8`, 2026-06-20

## Why this matters

`app/agents/scanner/scan_history.py` decides which tickers are *new* this run and
which just *transitioned into a breakout* — the logic behind the scanner's
"new" and "fresh breakouts" outputs. It is pure set/dict logic over snapshots,
plus a small JSON load/save with a backward-compatibility migration from an old
list-only format. None of it is tested. A regression (e.g. comparing against the
wrong snapshot, or the migration silently dropping data) would corrupt which
tickers get surfaced. These are fast, deterministic functions; this plan locks
their behavior in.

## Current state

File: `app/agents/scanner/scan_history.py`. Reproduced (the functions under test):

```python
Snapshot = dict[str, str]   # one snapshot maps ticker -> entry_zone ("" if unknown)
_MAX_SNAPSHOTS = 30

def load_history() -> dict[str, Snapshot]:
    """Return {date_str: {ticker: zone}} for all past runs."""
    try:
        raw = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except OSError:
        return {}
    result: dict[str, Snapshot] = {}
    for dt, val in raw.items():
        if isinstance(val, list):
            result[dt] = {t: "" for t in val}      # migrate old list format
        else:
            result[dt] = val
    return result

def get_new_tickers(current: list[str], history: dict[str, Snapshot]) -> set[str]:
    """Return tickers absent from the most recent run (all new if no history)."""
    if not history:
        return set(current)
    prev = history[max(history.keys())]
    return {t for t in current if t not in prev}

def get_fresh_breakouts(current_records, history) -> set[str]:
    """Return tickers that just transitioned INTO 'broken_out' this run."""
    broken_out_now = {
        r.ticker for r in current_records
        if r.analysis and r.analysis.entry_zone == "broken_out"
    }
    if not history:
        return broken_out_now
    prev = history[max(history.keys())]
    return {t for t in broken_out_now if prev.get(t) != "broken_out"}

def save_history(current_records, history) -> None:
    """Append today's {ticker: zone} snapshot and write to disk (max 30)."""
    today = date.today().isoformat()
    history[today] = {
        r.ticker: (r.analysis.entry_zone if r.analysis else "")
        for r in current_records
    }
    if len(history) > _MAX_SNAPSHOTS:
        for k in sorted(history.keys())[:-_MAX_SNAPSHOTS]:
            del history[k]
    _HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
```

Facts the tests rely on:
- "Most recent run" = `history[max(history.keys())]`. Keys are date strings; with
  ISO `YYYY-MM-DD` keys, lexical `max` == chronological max. Use ISO date-string
  keys in fixtures.
- `get_new_tickers`: empty history → every current ticker is "new". Otherwise a
  ticker is new iff it is **not a key** in the latest snapshot (the snapshot's
  *zone value* is irrelevant here).
- `get_fresh_breakouts`: only records whose `analysis.entry_zone == "broken_out"`
  qualify; with history, a ticker is fresh iff its previous snapshot zone is
  **not** `"broken_out"` (absent, or any other zone). Empty history → all
  currently-broken-out tickers.
- `save_history` writes to `_HISTORY_FILE` and uses `date.today()`. To test it
  without writing into the real data dir, **monkeypatch `scan_history._HISTORY_FILE`**
  to a `tmp_path` file (see Step 4).

### Building StockRecord test objects

`get_fresh_breakouts`/`save_history` consume `StockRecord` objects with an
optional `.analysis` carrying `.entry_zone`. Follow the exact pattern used in
`tests/test_exit_evaluator.py:20-33` and `:68-77`:

```python
from app.schemas import StockAnalysis, StockRecord, StockScan

def _record(ticker: str, zone: str | None) -> StockRecord:
    rec = StockRecord.model_validate(
        StockScan(
            ticker=ticker, as_of="2024-01-01", price=50.0, volume=1_000_000,
            rel_volume=1.0, high_52w=120.0, low_52w=40.0,
            pct_from_52w_high=-10.0, pct_change_week=0.0,
        ).model_dump()
    )
    if zone is not None:
        rec.analysis = StockAnalysis(
            score=7, stage="Stage 2", entry_zone=zone, summary=""
        )
    return rec
```

A record with `zone=None` leaves `rec.analysis` as its default (no analysis),
which `get_fresh_breakouts` treats as not-broken-out.

### Test conventions in this repo (match these)

- `tests/test_<module>.py`, plain pytest. See `tests/test_exit_evaluator.py`.
- For temp files use the built-in `tmp_path` fixture; redirect module globals via
  `monkeypatch.setattr`.

## Commands you will need

| Purpose   | Command                                          | Expected on success |
|-----------|--------------------------------------------------|---------------------|
| Run new tests | `uv run pytest tests/test_scan_history.py -q` | all pass          |
| Full suite | `uv run pytest -q`                              | all pass (was 98)   |
| Typecheck | `uv run pyrefly check`                            | exit 0              |
| Lint      | `uv run ruff check tests/test_scan_history.py`   | exit 0              |
| Format    | `uv run ruff format tests/test_scan_history.py`  | reformats, exit 0   |

## Scope

**In scope** (the only files you should create/modify):
- `tests/test_scan_history.py` (create)
- `plans/README.md` (status row only)

**Out of scope** (do NOT touch):
- `app/agents/scanner/scan_history.py` — characterization only. If a test reveals
  a likely bug, assert the actual behavior, add a `# NOTE:`, and report it.
- The real scan-history JSON data file — never write to it; use `tmp_path`.

## Git workflow

- Branch: `advisor/019-scan-history-diff-tests`
- Conventional-commit style, e.g.
  `test(scanner): characterize scan-history transition detection`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Create the test file with builders

Create `tests/test_scan_history.py` with the `_record` helper above and imports
of `app.agents.scanner.scan_history as sh`.

**Verify**: `uv run pytest tests/test_scan_history.py -q`
→ collects 0 tests, exits 0.

### Step 2: Test `get_new_tickers`

- **No history → all current are new**:
  `sh.get_new_tickers(["A", "B"], {}) == {"A", "B"}`.
- **Some seen, some new**: history
  `{"2024-01-01": {"A": "broken_out"}}`, current `["A", "B"]` → `{"B"}`.
- **Uses the latest snapshot only**: history with two dates where the *older*
  snapshot contains `B` but the *newer* one does not → `B` is still "new"
  (the older snapshot is ignored). Confirms `max(keys)` selection.

**Verify**: `uv run pytest tests/test_scan_history.py -q -k new`
→ all pass.

### Step 3: Test `get_fresh_breakouts`

- **No history → all currently-broken-out**: records
  `[_record("A", "broken_out"), _record("B", "approaching")]`, history `{}` →
  `{"A"}`.
- **Was already broken out → not fresh**: history
  `{"2024-01-01": {"A": "broken_out"}}`, current `[_record("A", "broken_out")]`
  → `set()`.
- **Transitioned into breakout → fresh**: history
  `{"2024-01-01": {"A": "approaching"}}`, current `[_record("A", "broken_out")]`
  → `{"A"}`.
- **Absent last run, broken out now → fresh**: history
  `{"2024-01-01": {"X": "broken_out"}}`, current `[_record("A", "broken_out")]`
  → `{"A"}`.
- **No analysis → ignored**: a `_record("C", None)` is never returned.

**Verify**: `uv run pytest tests/test_scan_history.py -q -k fresh`
→ all pass.

### Step 4: Test `load_history` migration + `save_history` round-trip (tmp file)

Redirect the module's history file to a temp path:

```python
def test_load_history_migrates_old_list_format(tmp_path, monkeypatch):
    f = tmp_path / "history.json"
    f.write_text('{"2024-01-01": ["A", "B"]}', encoding="utf-8")
    monkeypatch.setattr(sh, "_HISTORY_FILE", f)
    assert sh.load_history() == {"2024-01-01": {"A": "", "B": ""}}
```

Add:
- **Missing file → `{}`**: point `_HISTORY_FILE` at a non-existent path → `{}`.
- **New dict format preserved**: a file containing
  `{"2024-01-01": {"A": "broken_out"}}` loads unchanged.
- **`save_history` round-trip**: monkeypatch `_HISTORY_FILE` to `tmp_path`, call
  `sh.save_history([_record("A", "broken_out")], {})`, then `sh.load_history()`
  and assert today's date key maps to `{"A": "broken_out"}`. (Use
  `datetime.date.today().isoformat()` to build the expected key.)

**Verify**: `uv run pytest tests/test_scan_history.py -q -k "load or save or migrat"`
→ all pass.

### Step 5: Format, lint, typecheck, full suite

- `uv run ruff format tests/test_scan_history.py`
- `uv run ruff check tests/test_scan_history.py` → exit 0
- `uv run pyrefly check` → exit 0
- `uv run pytest -q` → full suite green

Then set this plan's row in `plans/README.md` to DONE.

## Test plan

- New file `tests/test_scan_history.py` covering: `get_new_tickers`
  (no-history/partial/latest-snapshot-only); `get_fresh_breakouts`
  (no-history/already-broken/transitioned/absent/no-analysis); `load_history`
  (old-list migration/missing-file/new-format) and a `save_history` round-trip —
  all using `tmp_path`, never the real data file.
- Structural pattern: `tests/test_exit_evaluator.py` for record builders;
  `tmp_path` + `monkeypatch` for the file functions.
- Verification: `uv run pytest tests/test_scan_history.py -q` → all pass;
  `uv run pytest -q` → still green.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uv run pytest tests/test_scan_history.py -q` passes with ≥11 new tests
- [ ] `uv run pytest -q` exits 0 (no regression in the existing 98)
- [ ] `uv run pyrefly check` exits 0
- [ ] `uv run ruff check tests/test_scan_history.py` exits 0
- [ ] `git status` shows only `tests/test_scan_history.py` and
      `plans/README.md` changed (the real scan-history JSON must be untouched)
- [ ] `plans/README.md` status row for plan 019 updated to DONE

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `scan_history.py` changed since `13074c8` and the
  function bodies no longer match the excerpts.
- `StockScan`/`StockAnalysis`/`StockRecord` reject the `_record` builder fields
  (the schema changed) — report the validation error; do not guess at new fields.
- Any test writes to the real scan-history data file instead of `tmp_path` —
  fix the monkeypatch before proceeding.

## Maintenance notes

- If the snapshot key format ever stops being ISO date strings, the `max(keys)`
  "most recent" assumption breaks — the latest-snapshot tests guard exactly that.
- A reviewer should confirm every file test uses `tmp_path` and monkeypatches
  `_HISTORY_FILE` (no writes to the real data dir).
- Deferred: `_MAX_SNAPSHOTS` trimming in `save_history` is exercised implicitly;
  add a dedicated >30-snapshot trim test if that path becomes load-bearing.
