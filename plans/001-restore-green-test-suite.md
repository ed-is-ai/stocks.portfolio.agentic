# Plan 001: Restore a green, network-free root test suite

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat f823039..HEAD -- agents/trader/trader_agent.py agents/alert/alert_agent.py agents/scanner/scanner_agent.py orchestrator.py tests/`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts below against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW (test-only changes + one small additive method; no behavior change to agents)
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `f823039`, 2026-06-13

## Why this matters

The root `tests/` suite is red on `main`: **6 tests fail and `tests/test_scanner.py`
hangs** because several tests make live network calls (yfinance, TradingView).
The project's own CLAUDE.md mandates "run tests before commits", but right now a
developer cannot tell a real regression from the existing breakage — the safety
net is effectively off. Worse, plans 002 and 003 modify the money-handling code
and need this suite as their verification gate. This plan makes the suite green
and hermetic (no network) **without changing any agent behavior** — every failure
is caused by tests that drifted from intentional code changes, not by bugs in the
code. (The one exception is a tiny, genuinely-useful helper method the tests
already document: `get_latest_trade`.)

## Current state

Confirmed failures (from `python -m pytest tests/ -o addopts=""`):

```
tests/test_trader_agent.py::test_correct_latest_trade  -> AttributeError: 'TraderAgent' object has no attribute 'get_latest_trade'
tests/test_alert.py::test_should_alert_high_score      -> assert False is True
tests/test_alert.py::test_should_alert_boundary_cases  -> assert False is True
tests/test_alert.py::test_run_method_with_alerts       -> assert 0 == 3
tests/test_smoke.py::test_full_pipeline_execution      -> AttributeError: module 'scanner_agent' has no attribute 'fetch_finviz_tickers'
tests/test_smoke.py::test_agent_chaining               -> AttributeError: ... 'fetch_finviz_tickers'
tests/test_scanner.py::test_run_method (+ default)     -> HANGS on live network calls
```

### Root cause per failure (read these — they explain WHY the test, not the code, is wrong)

**(a) `get_latest_trade` was removed but a test still calls it.**
`tests/test_trader_agent.py:50` calls `agent.get_latest_trade("TEST1")`. No such
method exists in `agents/trader/trader_agent.py` (confirmed: `git log -S "def
get_latest_trade"` returns nothing). The sibling method that does exist:

```python
# agents/trader/trader_agent.py:240
def get_trade_history(self, ticker: str | None = None) -> list[Trade]:
    """Return all trades, newest first. Optionally filter by ticker."""
```

Fix: add a tiny `get_latest_trade(ticker)` helper (newest trade for a ticker, or
None) — the test documents intended API and the helper is genuinely useful.

**(b) `alert_trigger` no longer fires on score alone — it fires on breakout events.**
This is by-design (git commit `ff7e4a2 feat(alert): suppress low-conviction buy
alerts`). The current contract:

```python
# agents/alert/alert_agent.py:171
def alert_trigger(self, stock: StockRecord) -> str | None:
    if not stock.analysis:
        return None
    a = stock.analysis
    is_fresh = a.fresh_breakout
    is_myb = a.multiyear_breakout
    if is_fresh and is_myb: return "VCP + Multi-Year Base Breakout"
    if is_fresh:            return "VCP Breakout"
    if is_myb:             return f"Multi-Year Base Breakout {pivot}"
    return None

# should_alert delegates to it (line 309):
def should_alert(self, stock, conn) -> bool:
    if self.alert_trigger(stock) is None:
        return False
    if self.was_recently_alerted(conn, stock.ticker):
        return False
    return True
```

The `test_alert.py` fixtures build high-`score` stocks but never set
`fresh_breakout`/`multiyear_breakout` (both default `False` in `StockAnalysis`),
so `alert_trigger` correctly returns `None`. The tests assert the obsolete
"score ≥ 8 ⇒ alert" model.

Also note `AlertAgent.run()` (line 91) **no longer sends per-stock emails** — it
queues buy alerts into `self._buy_alerts` and returns the count:

```python
# agents/alert/alert_agent.py:91-110 (abridged)
def run(self, payload) -> int:
    ...
    for stock in results:
        if not self.should_alert(stock, conn): continue
        trigger = self.alert_trigger(stock)
        self._buy_alerts.append((stock, trigger))
        self.record_alert(conn, stock)
    return len(self._buy_alerts)
```

So `test_run_method_with_alerts` asserting `send_email.call_count == 3` and a
`'Momentum Alert: HIGH'` subject is obsolete — `run()` sends nothing and returns
an int.

**(c) Scanner's source functions were renamed; tests patch a function that's gone.**
`scanner_agent.run()` (line 321) calls these external-data functions — none is
named `fetch_finviz_tickers`:

```python
# agents/scanner/scanner_agent.py — module-level functions called by run():
fetch_vcp_screener_tickers()            # line 101  (subprocess; returns [] without FMP_API_KEY)
fetch_tv_screener_tickers()             # imported from tv_extractor (line 33) — LIVE network
fetch_tv_screener_tickers_uk()          # imported from tv_extractor (line 33) — LIVE network
# and per-ticker, inside scan_watchlist():
_fetch_fundamentals(ticker)             # line 180 — yf.Ticker().info  — LIVE network
_congress_client.get_stats(ticker)      # line 479 — LIVE network
_fetch_spy_context()                    # line 155 — yf.download (covered by yfinance.download mock)
```

`tests/test_smoke.py:21,75` decorate with
`@patch('agents.scanner.scanner_agent.fetch_finviz_tickers', ...)` → AttributeError
at patch time. And `tests/test_scanner.py::test_run_method` /
`test_run_method_default_watchlist` mock only `yfinance.download`, leaving
`fetch_tv_screener_tickers*`, `_fetch_fundamentals`, and `_congress_client`
making live calls → the hang.

### The scan-output assertions in `test_smoke.py` are CORRECT — do not change them.
`orchestrator.pipeline()` writes exactly the grouped shape the test asserts:

```python
# orchestrator.py:554-556
scan_payload = {"as_of": datetime.now().isoformat(timespec="seconds")}
for src, items in grouped.items():
    scan_payload[src] = {"_comment": _SOURCE_COMMENTS.get(src, src), "results": items}
```

i.e. `as_of`, then `ww_extraction` → `{_comment, results:[{ticker, price, ...}]}`.
The smoke tests fail only at the `@patch` decorator, never reaching these asserts.

### Repo conventions to match
- Tests use `pytest` + `unittest.mock.patch`/`MagicMock`. Mock external calls by
  patching the **name as imported into the module under test** (e.g.
  `agents.scanner.scanner_agent.fetch_tv_screener_tickers`), not the origin module.
- Type hints required; line length ≤ 88. snake_case functions, f-strings.
- A shared fixture file exists at `tests/conftest.py` (e.g. `sample_stock_data`).

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Run one test file | `python -m pytest tests/test_alert.py -o addopts="" -q -p no:cacheprovider` | all pass |
| Run a single test | `python -m pytest "tests/test_alert.py::TestAlertAgent::test_should_alert_high_score" -o addopts="" -q` | passes |
| Run full root suite (timed) | `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` | all pass, **completes in < 30s** |
| Confirm no network hang | (same as above; if it does not finish in 30s, a network call is unmocked) | finishes fast |

> Note: `-o addopts=""` overrides `pytest.ini`'s `--json-report` (the
> `pytest-json-report` plugin may be absent). Always pass it.

## Scope

**In scope** (the only files you may modify):
- `tests/test_smoke.py`
- `tests/test_scanner.py`
- `tests/test_alert.py`
- `tests/test_trader_agent.py`
- `tests/conftest.py` (only if a shared fixture needs a `fresh_breakout` flag added)
- `agents/trader/trader_agent.py` (ONLY to add the additive `get_latest_trade` method — no other change)

**Out of scope** (do NOT touch — these are working as intended):
- Any logic in `agents/alert/alert_agent.py`, `agents/scanner/scanner_agent.py`,
  `orchestrator.py`. The behavior is correct; the tests are stale.
- `skills/*` tests (they have separate, pre-existing import errors — not this plan).
- The scan-output JSON shape assertions in `test_smoke.py` (they are correct).

## Git workflow

- Branch: `advisor/001-restore-green-test-suite`
- Commit per step; conventional-commit style (matches repo `git log`, e.g.
  `test(alert): realign alert fixtures to breakout-trigger contract`).
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add `get_latest_trade` to TraderAgent

In `agents/trader/trader_agent.py`, add this method (place it right after
`get_trade_history`, ~line 256), matching surrounding style:

```python
def get_latest_trade(self, ticker: str) -> Trade | None:
    """Return the most recent trade for a ticker, or None if none exist."""
    history = self.get_trade_history(ticker)
    return history[0] if history else None
```

`get_trade_history` already returns newest-first, so `[0]` is the latest.

**Verify**: `python -m pytest tests/test_trader_agent.py -o addopts="" -q` → `2 passed`

### Step 2: Realign the three alert tests to the breakout-trigger contract

In `tests/test_alert.py`:

1. **Fixtures**: find the `sample_high_score_stocks` fixture (builds the
   `StockAnalysis` objects). For each high-score analysis it builds, set
   `fresh_breakout=True` in the `StockAnalysis(...)` constructor (this is the flag
   `alert_trigger` keys on). Leave `sample_low_score_stocks` as-is
   (`fresh_breakout` stays `False` → correctly no alert).

2. **`test_should_alert_boundary_cases`** (line 104): the score-8-vs-6 boundary no
   longer governs alerts. Rewrite it to assert the breakout contract instead:
   - a `StockAnalysis(..., fresh_breakout=True)` ⇒ `should_alert(...) is True`
   - the same analysis with `fresh_breakout=False, multiyear_breakout=False`
     ⇒ `should_alert(...) is False`
   Keep using `mock_conn` with `fetchone.return_value = None` (so
   `was_recently_alerted` is False).

3. **`test_run_method_with_alerts`** (line 187): `run()` no longer calls
   `send_email`; it returns the number of queued buy alerts. Rewrite to:
   ```python
   agent = AlertAgent(db_path=str(tmp_path / "alerts.db"))
   count = agent.run(sample_high_score_stocks)
   assert count == len(sample_high_score_stocks)
   assert len(agent._buy_alerts) == len(sample_high_score_stocks)
   ```
   Remove the `@patch(...send_email)` decorator and the `call_count`/subject
   assertions. (Requires the Step-2 fixture change so the stocks trigger.)

**Verify**: `python -m pytest tests/test_alert.py -o addopts="" -q` → all pass
(was 3 failed, 8 passed → expect `11 passed`).

### Step 3: Make the smoke tests hermetic

In `tests/test_smoke.py`, on BOTH `test_full_pipeline_execution` (line 24) and
`test_agent_chaining` (line 78):

1. Remove the stale decorator
   `@patch('agents.scanner.scanner_agent.fetch_finviz_tickers', return_value=[])`.
2. Add decorators patching the real network surfaces (keep the existing
   `fetch_vcp_screener_tickers` and `yfinance.download` patches):
   ```python
   @patch('agents.scanner.scanner_agent.fetch_tv_screener_tickers', return_value=[])
   @patch('agents.scanner.scanner_agent.fetch_tv_screener_tickers_uk', return_value=[])
   @patch('agents.scanner.scanner_agent._fetch_fundamentals',
          return_value={"eps_growth": None, "annual_eps_growth": None, "roe": None,
                        "inst_ownership_pct": None, "pe_ratio": None,
                        "inst_count": None, "sector": None})
   ```
   Remember: each added `@patch` injects a positional mock arg into the test
   function signature, **bottom-decorator-first**. Update the signatures (add
   `_mock_*` params in the correct order) so the test still receives
   `mock_download` correctly. If unsure of ordering, run the single test and read
   the error — do not guess more than twice (see STOP conditions).
3. Do NOT change any assertion about the scan/analysis output JSON — they match
   current `orchestrator.pipeline()` output (verified).

**Verify**: `python -m pytest tests/test_smoke.py -o addopts="" -q` →
`3 passed` and completes in a few seconds (no hang).

### Step 4: Make the scanner tests hermetic

In `tests/test_scanner.py`, the two hanging tests are `test_run_method` (line 92)
and `test_run_method_default_watchlist` (line 113). Add the same three patches as
Step 3 (`fetch_tv_screener_tickers`, `fetch_tv_screener_tickers_uk`,
`_fetch_fundamentals`) plus
`@patch('agents.scanner.scanner_agent.fetch_vcp_screener_tickers', return_value=[])`
so `run()` makes zero live calls. Also patch the congress client to avoid its
network call:
`@patch('agents.scanner.scanner_agent._congress_client')` (a MagicMock whose
`.get_stats.return_value = None` is fine). Adjust each test signature for the
injected mock args.

Leave `test_fetch_stock_data_*`, `test_compute_technicals`, and
`test_scan_watchlist` unchanged (they already mock what they need).

**Verify**: `python -m pytest tests/test_scanner.py -o addopts="" -q` → all pass,
completes in < 10s (was hanging).

### Step 5: Confirm the whole root suite is green and fast

**Verify**:
`python -m pytest tests/ -o addopts="" -q -p no:cacheprovider`
→ `0 failed`, completes in **under 30 seconds** (proof that no test hits the
network).

## Test plan

No brand-new test files; this plan repairs existing tests and adds one method.
After completion the following must hold and are the regression coverage:
- `tests/test_trader_agent.py` — `get_latest_trade` exercised (2 passed).
- `tests/test_alert.py` — alert fires on `fresh_breakout`, not on raw score
  (11 passed).
- `tests/test_smoke.py`, `tests/test_scanner.py` — run fully mocked, no network
  (3 passed / all passed).
- Pattern to follow for the mock additions: the existing
  `@patch('agents.scanner.scanner_agent._fetch_fundamentals', ...)` on
  `tests/test_scanner.py::test_scan_watchlist` (line 70) is the exact idiom.

## Done criteria

ALL must hold:

- [ ] `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` exits 0
      (every root test passes) and finishes in < 30s.
- [ ] `grep -rn "fetch_finviz_tickers" tests/` returns no matches.
- [ ] `grep -n "def get_latest_trade" agents/trader/trader_agent.py` returns one match.
- [ ] No files outside the in-scope list are modified (`git status`).
- [ ] No production logic changed: `git diff f823039..HEAD -- agents/alert/alert_agent.py agents/scanner/scanner_agent.py orchestrator.py` shows **no** changes (only `trader_agent.py` gains `get_latest_trade`).
- [ ] `plans/README.md` status row for 001 updated to DONE.

## STOP conditions

Stop and report back (do not improvise) if:

- The "Current state" excerpts don't match the live code (drift since `f823039`).
- After fixing mock-argument ordering twice, a smoke/scanner test still errors on
  signature/argument count — report the exact error rather than reshuffling further.
- Making `test_full_pipeline_execution` pass appears to require changing a
  scan/analysis **output-shape** assertion (it shouldn't — that would mean
  `orchestrator.pipeline()` itself changed; that's out of scope).
- A test fails for a reason that implies a real bug in agent code (not a stale
  test). Report it — do not "fix" the agent to make the old test pass.

## Maintenance notes

- If the alert trigger model changes again (e.g. re-adding a score gate), the
  `fresh_breakout=True` fixtures in `test_alert.py` and the boundary test must be
  revisited — they now encode "alerts fire on breakout events".
- These tests are now hermetic by patching named imports in
  `scanner_agent`/`alert_agent`. If those modules change **how** they import the
  TV/fundamentals/congress helpers, the patch targets must move with them.
- Reviewer should scrutinize: that Step 3/4 didn't silently weaken assertions to
  pass, and that `git diff` on the three agent modules is empty (no behavior drift).
- Deferred: the `skills/*` collection errors (10 modules) are a separate
  pre-existing issue (broken relative imports) and intentionally out of scope here.
