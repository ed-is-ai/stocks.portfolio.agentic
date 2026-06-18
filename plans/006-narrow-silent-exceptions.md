# Plan 006: Replace silent `except Exception: pass` in the trader money/data path

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat ce96c93..HEAD -- agents/trader/trader_agent.py`
> If `agents/trader/trader_agent.py` changed since this plan was written,
> compare the "Current state" excerpts below against the live code before
> proceeding; on a mismatch, treat it as a STOP condition (line numbers will
> have moved — match on the surrounding code, not the numbers).

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: MED (touches money-path code; the whole point is to NOT change
  behavior, only to stop swallowing/log errors — verify against tests)
- **Depends on**: 001 (green test suite as the gate), and should land **after**
  003 if 003 is executed (003 owns the import-replay and oversell error paths;
  this plan deliberately leaves those alone)
- **Category**: tech-debt
- **Planned at**: commit `ce96c93`, 2026-06-18

## Why this matters

`agents/trader/trader_agent.py` swallows exceptions with bare
`except Exception: pass` in seven places on the money/data path. Silent
swallowing means a failed schema migration, an un-parseable cash figure, or a
broken price lookup produces **no signal at all** — the portfolio just shows
subtly wrong numbers and nobody knows why. The fix is not to change control flow
but to make failure *observable*: narrow each catch to the exception actually
expected, and log it (at `debug` for benign/expected cases like "column already
exists", at `warning` for cases that mask bad data). Behavior on the happy path
is unchanged; only the silent failures gain a log line. This is the deferred
"the rest" from plan 003, scoped tightly to `trader_agent.py` so it stays a
low-risk, reviewable change rather than a repo-wide sweep.

## Current state

- `agents/trader/trader_agent.py` — the trader; seven silent catches.

The seven occurrences (run `grep -n "except Exception" agents/trader/trader_agent.py`
to confirm; line numbers as of `ce96c93`):

```python
# ~line 92 and ~line 97 — schema migrations (ALTER TABLE add columns)
for col_def in (...):
    try:
        conn.execute(f"ALTER TABLE ... ADD COLUMN {col_def}")
    except Exception:
        pass            # benign: column already exists on re-run

# ~line 125 — (inside __init__ / schema setup block)
    except Exception:
        pass

# ~line 390 — inside value/price computation
    except Exception:
        pass

# ~line 411 — cash-balance parse fallback (this one already sets a value)
    except Exception:
        cash_balance = 0.0

# ~line 525 and ~line 541 — inside import_sipp row parsing
    except Exception:
        pass
```

> **IMPORTANT — the import_sipp catches (~525, ~541) belong to plan 003.**
> If plan 003 has already executed, those two may already be fixed; leave them
> as 003 left them. If 003 has NOT executed, still **do not** touch them in this
> plan — they are inside the trade-replay path 003 owns. This plan handles only
> the schema-migration catches (~92, ~97, ~125), the value/price catch (~390),
> and the cash-parse catch (~411).

### Logging in this module
Check whether the module already has a module-level logger:
`grep -n "logging\|getLogger\|logger" agents/trader/trader_agent.py`.
- If a `logger = logging.getLogger(__name__)` (or similar) already exists, use it.
- If not, add at the top of the module (after imports):
  `import logging` and `logger = logging.getLogger(__name__)`. Match how
  `web/app.py` / other agents import logging (`agents/scanner/scanner_agent.py`
  uses `logging`).

### Repo conventions to match
- Type hints required; line length ≤ 88; snake_case; f-strings; docstrings on
  public methods.
- Error handling elsewhere in the agents tends to log and continue rather than
  raise on best-effort paths — match that intent: narrow + log, do not start
  raising new exceptions that change control flow.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Trader unit tests | `python -m pytest tests/test_trader_agent.py -o addopts="" -q -p no:cacheprovider` | all pass |
| Full root suite | `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` | 0 failed, < 30s |
| Count remaining bare catches | `grep -n "except Exception:" agents/trader/trader_agent.py` | only the 2 import_sipp ones remain (or 0 if 003 fixed them) |

## Scope

**In scope** (the only file you may modify):
- `agents/trader/trader_agent.py` — the five catches listed above (schema ×3,
  value/price ×1, cash-parse ×1), plus adding a module logger if absent.

**Out of scope** (do NOT touch):
- The two `import_sipp` catches (~525, ~541) — owned by plan 003.
- `except Exception` in any other module (`web/app.py`, `analyst_agent.py`,
  `scanner_agent.py`) — deliberately deferred; this plan is trader-only to stay
  low-risk.
- Any change that alters control flow on the happy path (e.g. removing a
  fallback value, re-raising where code previously continued).

## Git workflow

- Branch: `advisor/006-narrow-silent-exceptions`
- Commit as one logical unit; conventional-commit style
  (e.g. `refactor(trader): log narrowed exceptions instead of silent pass`).
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Ensure a module logger exists

`grep -n "getLogger" agents/trader/trader_agent.py`. If none, add after the
import block:

```python
import logging

logger = logging.getLogger(__name__)
```

**Verify**: `grep -n "logger = logging.getLogger" agents/trader/trader_agent.py` → 1 match.

### Step 2: Narrow + log the schema-migration catches (~92, ~97, ~125)

These guard `ALTER TABLE ... ADD COLUMN`, which raises
`sqlite3.OperationalError` ("duplicate column name") on re-run — that is benign
and expected. Replace each:

```python
    except Exception:
        pass
```

with:

```python
    except sqlite3.OperationalError as exc:
        logger.debug("schema migration step skipped: %s", exc)
```

Ensure `import sqlite3` is present (it almost certainly already is — the module
uses sqlite; confirm with `grep -n "import sqlite3" agents/trader/trader_agent.py`).

**Verify**: `python -m pytest tests/test_trader_agent.py -o addopts="" -q` → all pass
(constructing a `TraderAgent` runs the migrations; tests must still pass).

### Step 3: Narrow + log the value/price catch (~390)

Open the code around line 390 and read what the `try` body does (it is in the
portfolio value / price path). Replace the silent catch with a logged one. Use
`warning` here because a failure means a position value may be wrong:

```python
    except Exception as exc:
        logger.warning("price/value computation failed: %s", exc)
```

Keep whatever the code did *after* the `except` unchanged (if it continued to
the next item, it still does).

**Verify**: `python -m pytest tests/test_trader_agent.py -o addopts="" -q` → all pass.

### Step 4: Log the cash-parse fallback (~411)

This one already sets `cash_balance = 0.0` on failure — keep that fallback, just
make it visible:

```python
    except (ValueError, TypeError) as exc:
        logger.warning("could not parse cash balance, defaulting to 0.0: %s", exc)
        cash_balance = 0.0
```

If reading the code shows the parsed value can fail in a way that is NOT a
`ValueError`/`TypeError` (e.g. a `KeyError` on a dict lookup), widen the tuple to
include exactly those types — do **not** fall back to bare `except Exception`.

**Verify**: `python -m pytest tests/test_trader_agent.py -o addopts="" -q` → all pass.

### Step 5: Confirm the suite is green and only the deferred catches remain

**Verify**:
- `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` → 0 failed, < 30s.
- `grep -n "except Exception:" agents/trader/trader_agent.py` → at most the two
  `import_sipp` catches remain (zero if plan 003 already fixed them).

## Test plan

- No new behavior, so no new behavioral tests are strictly required — the
  existing `tests/test_trader_agent.py` is the regression gate (constructing the
  agent and recording trades exercises the migration and value paths).
- **Optional, encouraged**: add one test asserting the cash-parse fallback logs
  a warning, modeled on existing trader tests + `pytest`'s `caplog` fixture:
  ```python
  def test_bad_cash_logs_warning(caplog) -> None:
      ...  # trigger the parse-failure branch, then:
      assert any("cash balance" in r.message for r in caplog.records)
  ```
  Only add this if the failure branch is reachable in a unit test without heavy
  setup; if not, skip it rather than contort the code.
- Verification: `python -m pytest tests/test_trader_agent.py -o addopts="" -q`.

## Done criteria

ALL must hold:

- [ ] `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` exits 0, < 30s.
- [ ] `grep -n "except Exception:" agents/trader/trader_agent.py` shows only the
      two `import_sipp` catches (or none if 003 ran) — the schema/value/cash
      catches are gone.
- [ ] `grep -c "logger\." agents/trader/trader_agent.py` ≥ 4 (the new log calls).
- [ ] No control-flow change on the happy path (manual diff review: every
      `except` still continues/falls back exactly as before).
- [ ] No files outside `agents/trader/trader_agent.py` modified (`git status`).
- [ ] `plans/README.md` status row for 006 updated to DONE.

## STOP conditions

Stop and report back (do not improvise) if:

- The five target catches don't match the "Current state" shape (drift, or 003
  refactored the area differently than expected).
- Narrowing a catch to a specific exception type causes a test to fail because
  the real code throws a *different* exception than expected — report the actual
  exception type rather than reverting to bare `except Exception`.
- You find that removing a silent catch reveals a genuine, previously-hidden bug
  (a real error was being swallowed). Report it — do not paper over it.

## Maintenance notes

- These catches are now narrowed to specific exception types. If the underlying
  call starts raising a different exception (e.g. a sqlite driver change, a new
  cash-field format), the narrow `except` may stop catching it and the error
  will surface — that is the intended behavior, but a reviewer should be aware.
- The remaining `except Exception` instances in `web/app.py`,
  `analyst_agent.py`, and `scanner_agent.py` are deliberately deferred. They are
  mostly network-resilience best-effort paths; address opportunistically, not as
  one sweep.
- A reviewer should scrutinize that no `except` started *raising* where it
  previously continued — that would change runtime behavior.
