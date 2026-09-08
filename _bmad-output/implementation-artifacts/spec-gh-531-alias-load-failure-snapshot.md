---
title: 'GH-531: transient alias-load failure poisons portfolio snapshots'
type: 'bugfix'
created: '2026-09-08'
status: 'done'
baseline_revision: 'add484a2bee51b080687cd811cf3d526890e3cb1'
final_revision: 'd9a2c9d2e43543e19f3bf56e3ad297403b45d73e'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
---

<intent-contract>

## Intent

**Problem:** A transient failure to read `config/ticker_aliases.json` makes
`load_aliases()` return `{}`, silently re-identifying every aliased holding by
its raw import spelling for that run. The headless snapshot path then finds a
stale, raw-keyed `price_cache` row (written before the alias existed), sees
nothing "missing", and values the holding off that dead row — one bad
`portfolio_snapshots` row valued a fund at ~15x and spiked the value-history
chart.

**Approach:** (A) `load_aliases()` distinguishes "file absent" (legitimately
`{}`) from "file present but unreadable/invalid": it reuses a process-cached
last-good map, and if none exists yet it raises `AliasFileUnreadableError`
rather than degrading to raw spellings. (B) The headless snapshot/pricing path
(`get_prices_for_holdings`) filters the loaded `price_cache` to canonical-key
rows only, so a stale alias-source row is never consulted, and resolves the
alias map up front so an unreadable file aborts the run before any snapshot is
written.

## Boundaries & Constraints

**Always:**
- A missing `config/ticker_aliases.json` still returns `{}` (no aliases
  configured is a legitimate state).
- Once any `load_aliases()` call succeeds in a process, a later read/parse
  failure returns that last-good map (logged at WARNING), never `{}`.
- With no last-good map cached, a present-but-unreadable/invalid ticker-alias
  file raises `AliasFileUnreadableError` from `load_aliases()`.
- `get_prices_for_holdings` values every holding only from a `price_cache` row
  whose key is its own canonical identity under the current alias map.
- `load_provider_symbol_aliases()` keeps its existing lenient behaviour
  (returns `{}` on any failure); only the ticker-alias map changes.

**Block If:**
- The alias-map identity contract turns out to be relied on by a caller that
  must not raise and has no last-good map available at that point (e.g. a
  first-request web path) — HALT, do not broaden the raise.

**Never:**
- Do not add a new dependency, a config flag, or a persistent/on-disk alias
  cache — the cache is a plain module-level variable.
- Do not change the `/portfolio` display path (`fetch_all_prices` /
  `_resolve`) — it already canonicalises and was unaffected.
- Do not write a standalone DB migration script; a guard on read satisfies the
  stale-row requirement.
- Do not alter `canonical_ticker` / `canonicalize_or_fallback` semantics.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Alias file absent | `TICKER_ALIASES_JSON` does not exist | `load_aliases()` returns `{}` | No error |
| First load OK, then unreadable | one successful `load_aliases()`, then file `chmod 000` / mid-rewrite | second call returns the first map, logs WARNING | Caught, last-good returned |
| Unreadable on first-ever load | file present but `OSError` / bad JSON / wrong shape, no prior success | `load_aliases()` raises `AliasFileUnreadableError` | Raised to caller |
| Invalid JSON / wrong shape / empty key-value | as today's tests, no prior success | raises `AliasFileUnreadableError` | Raised |
| Stale alias-source row in price_cache | `price_cache` has key `HSFWA`, alias `HSFWA -> 0P00013P6I.L` configured, canonical row also present | `get_prices_for_holdings(['0P00013P6I.L'])` returns the canonical row's price; `HSFWA` row ignored | Stale key filtered out |
| Snapshot run, alias file unreadable, no last-good | orchestrator calls `get_prices_for_holdings` | `AliasFileUnreadableError` propagates; no `portfolio_snapshots` row written | Run aborts loudly |
| price_cache key with a cyclic alias chain | `canonicalize_or_fallback` can't resolve key | key kept as-is (fallback), row retained | Logged, not dropped |

</intent-contract>

## Code Map

- `app/core/ticker_identity.py` — `load_aliases()` / `_load_alias_map`; add
  last-good module cache, `AliasFileUnreadableError`, strict-vs-lenient split.
- `app/services/portfolio_service.py` — `get_prices_for_holdings` (~L760);
  resolve aliases up front, filter `price_cache` to canonical keys. Also
  `load_ticker_aliases` (~L177) delegates to `load_aliases()`.
- `app/orchestration/orchestrator.py` — ~L1181-1198, the headless snapshot
  caller; must let `AliasFileUnreadableError` propagate (confirm no broad
  `except` swallows it around the snapshot write).
- `app/agents/trader/trader_agent.py` — `_replay_trades` (L868) and
  `held_tickers` path both call `load_aliases()`; post-fix they raise or use
  last-good instead of silently keying by raw spelling. No change expected,
  but confirm the raise is acceptable on these paths (replay is invoked from
  the same orchestrator snapshot run and from `/portfolio`; see Block If).
- `tests/test_ticker_identity.py` — existing `load_aliases` failure tests
  assert `== {}`; update to the new raise / last-good contract + add an
  autouse reset of the module cache.
- `tests/test_portfolio_service.py` — add the stale-row valuation regression
  and the "unreadable alias file during snapshot pricing -> raises, no
  snapshot" regression.

## Tasks & Acceptance

**Execution:**
- [x] `app/core/ticker_identity.py` — add module-level `_last_good_aliases:
  dict[str, str] | None`, `class AliasFileUnreadableError(RuntimeError)`, and
  split `_load_alias_map` so failures raise an internal error in strict mode.
  `load_aliases()`: `FileNotFoundError -> {}`; on strict failure, return
  `_last_good_aliases` if set (WARNING log) else raise
  `AliasFileUnreadableError`; on success store and return the map.
  `load_provider_symbol_aliases()` stays lenient (`{}` on failure).
- [x] `app/services/portfolio_service.py` — in `get_prices_for_holdings`, load
  `aliases = self.load_ticker_aliases()` before computing `missing`, and drop
  any `cached_prices` / `cached_display` entry whose key `k` is not
  `canonicalize_or_fallback(k, aliases, ...)` (keep `__…__` sentinels like
  `__GBPUSD__`). Compute `missing` against the filtered dict.
- [x] `app/orchestration/orchestrator.py` — verify (and adjust only if needed)
  that an exception from `get_prices_for_holdings` aborts the run before
  `update_portfolio_snapshot` / `_append_portfolio_snapshot`; add a comment
  referencing GH-531 if a catch is loosened.
- [x] `tests/test_ticker_identity.py` — autouse fixture resetting
  `ticker_identity._last_good_aliases = None`; rewrite the four failure tests
  (`invalid_json`, `wrong_shape`, `os_error`, `unicode_decode_error`) to expect
  `AliasFileUnreadableError`; add: last-good reuse after a prior success;
  `FileNotFoundError` still `{}`.
- [x] `tests/test_portfolio_service.py` — regression: `price_cache` has a stale
  `HSFWA` row (£109.90) and a canonical `0P00013P6I.L` row (£4.19), alias
  configured; `get_prices_for_holdings(['0P00013P6I.L'])` values from £4.19 and
  never returns £109.90 for the holding.
- [x] `tests/test_portfolio_service.py` — regression: alias file unreadable
  (no prior success) during `get_prices_for_holdings` raises
  `AliasFileUnreadableError`; assert the stub `save_price_cache` was not called.

**Acceptance Criteria:**
- Given a present-but-unreadable/invalid `ticker_aliases.json` and no prior
  successful load, when any snapshot-path code calls `load_aliases()`, then it
  raises `AliasFileUnreadableError` and no holding is valued by raw spelling.
- Given one prior successful `load_aliases()` in the process, when the file
  becomes unreadable, then `load_aliases()` returns the last-good map and logs
  a WARNING.
- Given a stale `price_cache` row under an alias-source key, when
  `get_prices_for_holdings` values holdings, then the holding is valued from
  the canonical row and the stale row is ignored.
- Given the orchestrator snapshot run and an unreadable alias file, when the
  run executes, then it aborts before writing a `portfolio_snapshots` row.
- Given a missing `ticker_aliases.json`, when `load_aliases()` is called, then
  it returns `{}` unchanged.

## Spec Change Log

_No `bad_spec` loopback — no entries._

## Review Triage Log

### 2026-09-08 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 0, medium 0, low 2)
- defer: 0
- reject: 11: (high 0, medium 2, low 9)
- addressed_findings:
  - `[low]` `[patch]` Empty / whitespace-only alias file raised `AliasFileUnreadableError` (old code returned `{}` via `JSONDecodeError`); `_load_alias_map_strict` now treats a blank file as "no aliases" (`{}`), with a parametrized regression test.
  - `[low]` `[patch]` Last-good fallback returned the shared module dict; now returns `dict(_last_good_aliases)` so a caller mutating the result cannot poison later fallbacks.
- rejected (summary): broad `load_aliases()` blast-radius / SIPP-import now fails loud — matches intent-contract acceptance criterion 1 ("fails loudly OR falls back to last good") and Design Notes; uptime-dependent raise-vs-fallback — inherent to the last-good design and accepted; no TTL on last-good — self-heals on the next readable load; module-global without a lock — GIL-safe, a lock is over-engineering; per-row canonicalize cost on the fast path — called once per pipeline run, negligible; cyclic-alias log volume — degenerate operator misconfig; `k.startswith("__")` sentinel — no realistic ticker collides; `missing` recompute for raw spellings — `held_tickers()` already canonicalizes its input; `.bak` file — untracked, not staged.

## Design Notes

Why last-good + raise (not just `{}` with a warning): the incident was the
*first* pipeline run after a restart, so a plain last-good cache would still
have been empty and still degraded to raw spellings. The raise is the
backstop for the cold-start case; the cache handles the steady-state
mid-rewrite case without aborting a run unnecessarily.

Why filter `price_cache` on read rather than a migration or a write guard:
the orphan `HSFWA` row was *canonical* when written (no alias existed yet) —
a write-time guard would not have caught it. It only became non-canonical
retroactively when the alias was added, so a read-time canonical-key filter
is the matching fix and self-heals without a migration.

Sketch:

```python
# ticker_identity.py
_last_good_aliases: dict[str, str] | None = None

def load_aliases() -> dict[str, str]:
    global _last_good_aliases
    try:
        data = _load_alias_map(TICKER_ALIASES_JSON, strict=True)
    except FileNotFoundError:
        return {}
    except _AliasLoadError as exc:
        if _last_good_aliases is not None:
            logger.warning("Reusing last-good ticker aliases: %s", exc)
            return _last_good_aliases
        raise AliasFileUnreadableError(str(exc)) from exc
    _last_good_aliases = data
    return data
```

```python
# portfolio_service.get_prices_for_holdings
cached_prices, _, cached_display = self._trader.load_price_cache()
aliases = self.load_ticker_aliases()
cached_prices = {
    k: v for k, v in cached_prices.items()
    if k.startswith("__")
    or canonicalize_or_fallback(k, aliases, logger=logger,
                                context="price_cache") == k
}
cached_display = {k: v for k, v in cached_display.items() if k in cached_prices}
missing = [t for t in tickers if t not in cached_prices]
```

## Verification

**Commands:**
- `uv run pytest tests/test_ticker_identity.py tests/test_portfolio_service.py
  tests/test_snapshot_valuation.py` — expected: all pass.
- `uv run ruff format . && uv run ruff check .` — expected: clean.
- `uv run pyrefly check` — expected: no new errors.
- `uv run pytest` — expected: full suite green (no collateral breakage from the
  `load_aliases` contract change).

## Auto Run Result

Status: done

**Change:** A transient `config/ticker_aliases.json` read failure no longer
silently degrades every aliased holding to its raw import spelling. Part A:
`load_aliases()` now distinguishes an absent file (`{}`) from a
present-but-unreadable/invalid one — it reuses a process-cached last-good map,
or raises `AliasFileUnreadableError` when none is cached yet. An
empty/whitespace file still means "no aliases". `load_provider_symbol_aliases()`
stays lenient. Part B: the headless pricing core `get_prices_for_holdings`
resolves the alias map up front (so an unreadable file aborts the run before
any `portfolio_snapshots` write) and filters `price_cache` to canonical-key
rows only, so a stale row keyed by a now-aliased raw spelling is never
consulted.

**Files changed:**
- `app/core/ticker_identity.py` — last-good alias cache, `AliasFileUnreadableError`,
  strict `_load_alias_map_strict` split, empty-file tolerance.
- `app/services/portfolio_service.py` — `get_prices_for_holdings` resolves aliases
  first and drops non-canonical `price_cache` rows before pricing.
- `tests/test_ticker_identity.py` — failure tests updated to the raise/last-good
  contract; autouse cache reset; empty-file and last-good-reuse coverage.
- `tests/test_portfolio_service.py` — regressions for the stale alias-source row
  and the unreadable-file abort.

**Review:** Blind Hunter + Edge Case Hunter, 1 pass. 2 low-severity patches
applied (empty-file tolerance, defensive-copy on fallback); 11 findings
rejected (see Review Triage Log). No `intent_gap`, no `bad_spec` loopback.

**Verification:**
- `uv run pytest` — 2983 passed.
- `uv run ruff format` / `ruff check` (touched files) — clean.
- `uv run pyrefly check app/core/ticker_identity.py` — 0 errors.

**Residual risks:** The `load_aliases()` contract change means other callers
(`trades_repo.held_tickers`, `_replay_trades`, `import_sipp`, recommendation
and snapshot-evidence services) now raise `AliasFileUnreadableError` on a
cold-process unreadable alias file instead of degrading to `{}`. This is the
intended loud-failure behaviour (acceptance criterion 1) but broadens where a
genuinely broken config file surfaces as an error; the last-good cache masks
it for any process that has loaded the file once.
