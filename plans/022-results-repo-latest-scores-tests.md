# Plan 022: Cover `ResultsRepository.latest_scores` for multiple tickers

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 7bfeee7..HEAD -- app/repositories/results_repo.py`
> If `results_repo.py` changed since this plan was written, compare the "Current
> state" excerpt against the live code before proceeding; on a mismatch, treat
> it as a STOP condition.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `7bfeee7`, 2026-06-20

## Why this matters

`ResultsRepository.latest_scores()` returns `{ticker: score}` for each ticker's
most-recent `as_of`, via a correlated `MAX(as_of)` subquery. It feeds the
analyst's "latest score per ticker" lookup. The only existing test
(`test_results_save_and_latest_scores`) uses a **single ticker with two rows** —
so the per-ticker `WHERE h2.ticker = analysis_history.ticker` correlation is
never actually exercised across tickers, and the empty-table case is untested.
A regression that dropped the correlation (returning the global latest row, or
one score for all tickers) would pass the current test. This plan adds the
multi-ticker and empty-table cases that make the query's contract explicit.

## Current state

File: `app/repositories/results_repo.py`. The method under test (reproduced):

```python
def latest_scores(self) -> dict[str, int]:
    """Return {ticker: score} from each ticker's most recent ``as_of``."""
    with session(self._connect) as conn:
        rows = conn.execute(
            "SELECT ticker, score FROM analysis_history"
            " WHERE as_of = ("
            "   SELECT MAX(as_of) FROM analysis_history h2"
            "   WHERE h2.ticker = analysis_history.ticker"
            " )"
        ).fetchall()
    return dict(rows)
```

Facts the tests rely on:
- The table's primary key is `(ticker, as_of)` (see `_SCHEMA` in the same file),
  so two rows with the **same** ticker and `as_of` cannot exist — there is no
  tie case to test; do not write one.
- `as_of` values are date/time **strings** (e.g. `"2024-01-02 09:00"`); the
  `MAX(as_of)` comparison is lexical, and ISO-ish strings sort chronologically.
- `save_results` takes a list of 10-field `ResultRow` tuples in this exact
  order: `(ticker, as_of, score, canslim_total, momentum_total, stage, price,
  entry_price, stop_loss, entry_zone)`. Copy the shape from the existing test.
- The repo is constructed with an injectable `Connect` factory pointed at a temp
  DB, then `ensure_schema()` is called. This is already the pattern in the
  existing results test.

### Test conventions in this repo (match these — this is the exact existing pattern)

From `tests/test_repositories.py` (the file you will extend):

```python
def test_results_save_and_latest_scores(tmp_path):
    repo = ResultsRepository(db.make_connect(lambda: tmp_path / "results.db"))
    repo.ensure_schema()
    repo.save_results(
        [
            ("AAPL", "2024-01-01 09:00", 7, 10, 9, "Stage 2", 100.0, 101.0, 95.0, "far"),
            ("AAPL", "2024-01-02 09:00", 8, 11, 9, "Stage 2", 102.0, 103.0, 96.0, "near"),
        ]
    )
    assert repo.latest_scores() == {"AAPL": 8}
```

`db`, `ResultsRepository`, and `pytest` are already imported at the top of
`tests/test_repositories.py`. Use `tmp_path` (the built-in fixture). Never point
the repo at a real database file.

## Commands you will need

| Purpose   | Command                                                  | Expected on success |
|-----------|----------------------------------------------------------|---------------------|
| Run results tests | `uv run pytest tests/test_repositories.py -q -k results` | all pass    |
| Run the file | `uv run pytest tests/test_repositories.py -q`         | all pass            |
| Full suite | `uv run pytest -q`                                       | all pass (no regressions) |
| Typecheck | `uv run pyrefly check`                                    | no NEW errors in `tests/test_repositories.py` (large pre-existing baseline exists) |
| Lint      | `uv run ruff check tests/test_repositories.py`           | exit 0              |
| Format    | `uv run ruff format tests/test_repositories.py`          | reformats, exit 0   |

## Scope

**In scope** (the only files you should modify):
- `tests/test_repositories.py` (append new test functions to the
  `--- ResultsRepository ---` section near the end of the file)
- `plans/README.md` (status row only)

**Out of scope** (do NOT touch):
- `app/repositories/results_repo.py` — characterization only; do not change the
  query. If a test reveals the correlation is broken, assert the **actual**
  observed result with a `# NOTE:` and report it.
- The existing `test_results_save_and_latest_scores` — leave it unchanged; add
  new tests alongside it.
- Any other test or `app/` file.

## Git workflow

- Branch: `advisor/022-results-repo-latest-scores-tests`
- Conventional-commit style, e.g.
  `test(repositories): cover latest_scores across multiple tickers`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add a multi-ticker test

Append to the `--- ResultsRepository ---` section of
`tests/test_repositories.py`. Build a small `ResultRow` helper inside the test
(or inline the tuples) covering two tickers, each with two `as_of` values, where
the later `as_of` has a different score:

- `AAPL`: `2024-01-01` score 7, `2024-01-03` score 9 → expect 9
- `MSFT`: `2024-01-02` score 5, `2024-01-04` score 6 → expect 6

Assert `repo.latest_scores() == {"AAPL": 9, "MSFT": 6}`. This is the case that
actually exercises the per-ticker correlation (each ticker resolves to *its own*
latest row, not a global latest).

**Verify**: `uv run pytest tests/test_repositories.py -q -k results` → all pass.

### Step 2: Add an empty-table test

Add a test that creates the schema but saves nothing:

```python
def test_results_latest_scores_empty(tmp_path):
    repo = ResultsRepository(db.make_connect(lambda: tmp_path / "results.db"))
    repo.ensure_schema()
    assert repo.latest_scores() == {}
```

**Verify**: `uv run pytest tests/test_repositories.py -q -k results` → all pass.

### Step 3: Add a single-row-per-ticker test (optional but recommended)

A test with one row each for two tickers, confirming both are returned
(`{"AAPL": 7, "MSFT": 5}`) — guards against an accidental `LIMIT 1` or a global
`MAX` that would return only one ticker.

**Verify**: `uv run pytest tests/test_repositories.py -q -k results` → all pass.

### Step 4: Format, lint, typecheck, full suite

- `uv run ruff format tests/test_repositories.py`
- `uv run ruff check tests/test_repositories.py` → exit 0
- `uv run pyrefly check` → no new errors referencing `tests/test_repositories.py`
- `uv run pytest -q` → full suite green

Then update this plan's row in `plans/README.md` to DONE (unless a reviewer
maintains the index).

## Test plan

- Extend `tests/test_repositories.py` with **≥2 new tests** (3 if you include
  Step 3): multi-ticker latest, empty table, single-row-per-ticker.
- Structural pattern: the existing `test_results_save_and_latest_scores` in the
  same file — same construction, same `ResultRow` tuple order, `tmp_path`.
- Verification: `uv run pytest tests/test_repositories.py -q -k results` → all
  pass; `uv run pytest -q` → still green.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uv run pytest tests/test_repositories.py -q -k results` passes with the
      new tests (≥2 added)
- [ ] `uv run pytest -q` exits 0 (no regression)
- [ ] `uv run ruff check tests/test_repositories.py` exits 0
- [ ] `uv run pyrefly check` introduces no new errors in `tests/test_repositories.py`
- [ ] `git status` shows only `tests/test_repositories.py` modified (and
      `plans/README.md` if you updated the index)

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `results_repo.py` changed since `7bfeee7` and the
  `latest_scores` query or the `ResultRow` field order no longer matches the
  excerpt.
- The multi-ticker assertion fails — e.g. `latest_scores()` returns a single
  ticker, or the wrong score for a ticker. That is a real correlation bug: leave
  the test asserting the **actual** observed dict with a `# NOTE:` and report
  it; do not change the source query.

## Maintenance notes

- If `as_of` ever stops being a lexically-sortable timestamp string, the
  `MAX(as_of)` "latest" assumption breaks — these multi-ticker tests would catch
  a regression where the wrong row's score is returned.
- A reviewer should confirm the multi-ticker test uses *distinct* later scores
  per ticker (so a global-MAX bug can't accidentally produce the expected dict),
  and that no test points the repo at a real `results.db`.
- Deferred: `save_results` upsert/`INSERT OR REPLACE` semantics and the
  `ensure_schema` additive migration are not in scope here.
