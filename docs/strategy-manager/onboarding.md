# Strategy Manager onboarding

This guide explains how to get from a local checkout to a completed Strategy
Manager backtest, how to identify the gate that is stopping you, and which
parts of the current setup are not yet self-service.

> [!IMPORTANT]
> Strategy Manager is not currently bootstrappable from a clean checkout.
> The web UI can initialize historical months and run backtests only after an
> operator has provisioned a qualified historical-data contract, an immutable
> reconstruction roster, and an active snapshot profile. There is no supported
> CLI or UI workflow that creates those three prerequisites yet.

Do not edit the Strategy Manager SQLite tables by hand. Much of the evidence is
content-addressed or append-only, and a plausible-looking manual row can make
the database internally inconsistent.

## What must exist first

Strategy Manager has four gates. A later gate cannot compensate for a missing
earlier one.

```text
Historical provider qualification
              |
              v
Reconstruction roster + active snapshot profile
              |
              v
Ready monthly snapshot coverage
              |
              v
Backtest launch -> worker -> result
```

1. **Historical provider qualification** proves that the installed yfinance,
   pandas, calendar, fixture, and live-probe contract still matches the
   version the application expects.
2. **Roster and active profile** pin the securities, aliases, detector source
   versions, calendar version, and reconstruction policy used by a run.
3. **Monthly coverage** stores complete, immutable scanner snapshots for every
   month in a Ready interval.
4. **Backtest execution** discovers a Strategy Skill, validates its parameters,
   pins price/action/FX revisions, and runs it in a child worker process.

The first two gates are provisioning concerns. The web UI owns gates 3 and 4.

## Install and start the application

Requirements are Python 3.12 or newer, Git, `uv`, and internet access to the
configured public market-data sources.

```bash
git clone https://github.com/ed-is-ai/Agents.stocks.git
cd Agents.stocks
uv sync
cp .env.example .env
uv run python -m app.main serve
```

Open <http://127.0.0.1:8000>, then select **Strategy Manager**.

The local Strategy Manager worker is enabled by default. If the application is
started by a process manager, make the setting explicit for the one process
that should execute jobs:

```dotenv
STRATEGY_MANAGER_WORKER_ENABLED=true
```

Set it to `false` only for additional web processes that must not own the local
dispatcher. If the worker is disabled everywhere, jobs remain Queued.

## Run the preflight

### 1. Confirm that Strategy Skills are discoverable

Run this from the repository root:

```bash
uv run python - <<'PY'
from app.core.config import SKILLS_DIR
from app.services.backtest.skill_discovery import discover_strategies

result = discover_strategies(SKILLS_DIR)
print("Strategies:")
for strategy in result.strategies:
    print(f"  {strategy.strategy_id}: {strategy.display_name}")
print("Warnings:")
for warning in result.warnings:
    print(f"  {warning.folder}: {warning.message}")
PY
```

The current repository should discover these six Strategy IDs:

- `rtly-backtest-buy-and-hold`
- `rtly-backtest-darvas-box`
- `rtly-backtest-minervini`
- `rtly-backtest-moving-average`
- `rtly-backtest-turtle-trend`
- `rtly-backtest-weinstein`

A warning applies only to the folder it names. Fix its `SKILL.md` metadata or
`scripts/strategy.py` entry point before trying to select that Strategy.

### 2. Inspect the durable setup state

Start the application once before running this check so it can create the
database schema. The query is read-only.

```bash
uv run python - <<'PY'
import sqlite3
from app.core.config import BACKTEST_DB
from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository

with sqlite3.connect(f"file:{BACKTEST_DB}?mode=ro", uri=True) as connection:
    checks = {
        "qualification attempts": "SELECT COUNT(*) FROM historical_source_qualifications",
        "passed qualifications": "SELECT COUNT(*) FROM historical_source_qualifications WHERE passed = 1",
        "reconstruction rosters": "SELECT COUNT(*) FROM reconstruction_rosters",
        "snapshot profiles": "SELECT COUNT(*) FROM snapshot_profiles",
        "active profiles": "SELECT COUNT(*) FROM active_snapshot_profile",
        "ready snapshot months": "SELECT COUNT(*) FROM snapshot_months",
        "jobs": "SELECT COUNT(*) FROM strategy_jobs",
    }
    for label, statement in checks.items():
        print(f"{label}: {connection.execute(statement).fetchone()[0]}")

repository = BacktestRepository(db.make_connect(lambda: str(BACKTEST_DB)))
current = repository.current_qualification_contract_digest()
print(f"current compatible qualification: {'yes' if current else 'no'}")
PY
```

Interpret the output in order:

| Result | Meaning | Next action |
| --- | --- | --- |
| `current compatible qualification: no` | The latest attempt did not pass, or its installed package/calendar/fixture/probe contract differs from the current one. An older passed row is not sufficient. | Provision or rerun the exact qualification workflow. The UI cannot do this. |
| `reconstruction rosters: 0` | No immutable market universe has been captured. | Provision a roster from the required DataHub S&P 500, TradingView US, and TradingView UK sources. The UI cannot do this. |
| `active profiles: 0` | Strategy Manager has no scanner-data version to use. | Build, persist, and activate a profile bound to the roster. The UI cannot do this. |
| `ready snapshot months: 0` | Provisioning exists, but no historical month has completed. | Use **Historical initialization** in the UI. |
| `jobs: 0` | Nothing has been submitted yet. | Initialize coverage first, then configure a backtest. |

On a clean checkout, the first three counts are expected to be zero. That is a
known onboarding gap, not an error in your installation command.

## Provisioning boundary

There is currently no supported end-user command that safely composes the
following application APIs:

- `QualificationRunner` in
  `app/services/backtest/historical_data_qualification.py`;
- `ReconstructionRosterCaptureService` in
  `app/services/backtest/reconstruction_roster.py`;
- `SnapshotProfileV1`,
  `BacktestRepository.compare_and_insert_snapshot_profile()`, and
  `BacktestRepository.activate_snapshot_profile()`.

Until a bootstrap command is added, use one of these options:

1. Run against a database set provisioned by the application maintainer for
   the **same Git commit and locked dependency versions**.
2. Add a reviewed bootstrap command that composes the APIs above and records
   failures without bypassing qualification or integrity validation.

A provisioned environment may need all of the following together:

- `data/backtest.db` — qualification, roster, profile, coverage, jobs, and
  results;
- `data/historical_price_cache.db` — immutable provider-native price/action
  evidence referenced by the backtest database;
- `app/agents/trader/trades.db` — content-addressed `GBPUSD=X` quotes when the
  roster currency differs from the selected base currency.

Copying only `backtest.db` can leave dangling evidence digests. Never use an
untrusted database bundle.

## Initialize historical coverage

Once the preflight reports a passed qualification and one active profile:

1. Start the application with its Strategy Manager worker enabled.
2. Open **Strategy Manager** -> **Historical initialization**.
3. Choose fully completed months in `YYYY-MM` format. The current month is not
   eligible.
4. For an initial smoke test, request one month. A large roster and a long
   range can require substantial network work.
5. Select **Initialize** and leave the server running.
6. Watch the Activity page until it reaches Complete. The page polls every
   three seconds.
7. Return to Strategy Manager and confirm the month appears in a Ready
   interval.

Initialization reconstructs every member of the active profile. One failed or
partial member prevents the month from being presented as Ready. Restarting a
failed attempt reuses committed shared evidence but replays the attempt from
the beginning.

## Find a usable security ID

Every bundled Strategy currently trades one configured `security_id`. The UI
does not yet display the active roster's internal IDs, and the sample default
`sec-aapl` is a test-friendly placeholder that may not exist in a live roster.

Use this read-only query to select an ID from the active profile:

```bash
uv run python - <<'PY'
import sqlite3
from app.core.config import BACKTEST_DB

statement = """
SELECT member.security_id, member.provider_symbol, member.mic, member.currency
FROM active_snapshot_profile AS active
JOIN snapshot_profiles AS profile
  ON profile.profile_hash = active.profile_hash
JOIN reconstruction_roster_members AS member
  ON member.roster_digest = profile.roster_digest
ORDER BY member.mic, member.provider_symbol
"""
with sqlite3.connect(f"file:{BACKTEST_DB}?mode=ro", uri=True) as connection:
    for row in connection.execute(statement):
        print("\t".join(row))
PY
```

Copy the first-column value for the symbol you intend to test and paste it
into the Strategy's `security_id` field.

## Run a smoke-test backtest

Start with **Buy and Hold Backtest** because it has the fewest signal gates.

1. Open **Strategy Manager** -> **Configure a Backtest**.
2. Select **Buy and Hold Backtest**.
3. Replace `sec-aapl` with a real active-profile security ID from the query
   above.
4. Keep `fixed_shares` small, for example `10`.
5. Set `entry_on_or_after` to a date on or before the selected Ready period.
6. Select a start and end month inside one Ready interval.
7. Enter a positive starting capital, for example `10000.00`.
8. Select the base currency and choose **Run Backtest**.
9. Keep the server running while the Activity page is Queued or Running.
10. When it reaches Complete, choose **Review this Backtest**.

Signals are evaluated after a session close and accepted orders fill at the
next exchange session's open. The v1 engine is long-only, does not pyramid,
and permits only full-position exits.

### FX requirement

Launch pins evidence for the **entire active roster**, even though a bundled
Strategy trades one security. If any roster member's currency differs from the
selected base currency, Strategy Manager requires a content-addressed
`GBPUSD=X` quote in `trades.db` for the first calendar day of the start month.

If that quote is absent, launch fails with:

```text
Pinned historical FX evidence is unavailable for '<security-id>' as of YYYY-MM-01.
```

Changing GBP to USD usually does not solve this for a mixed US/UK roster; it
only changes which members need conversion. Historical FX backfill is not
currently part of the Strategy Manager UI.

## Troubleshooting

| Symptom | Cause | What to do |
| --- | --- | --- |
| **Initialize** is disabled: “No active scanner-data version is configured.” | No active snapshot profile exists. | Complete provisioning; the UI cannot create the profile. |
| **Initialize** is disabled: “Historical data providers have not passed certification.” | No compatible passed qualification exists. | Rerun the exact live qualification workflow; check internet access and provider throttling. |
| **Run Backtest** is disabled. | No Ready monthly coverage exists, or coverage integrity validation failed. | Complete Historical initialization and resolve any red coverage alert. |
| No Strategies appear. | Discovery rejected the Skill metadata or runtime. | Run the discovery preflight and fix the named folder warning. |
| A run completes with no trades. | The `security_id` is not in the active roster, the entry date is after the period, there is insufficient warm-up, or the Strategy's signal gates never passed. | Use a roster ID, then smoke-test Buy and Hold before a signal-heavy Strategy. |
| Launch reports missing pinned historical evidence. | `backtest.db` references a price/action revision absent from `historical_price_cache.db`. | Restore the matching provisioned cache or rerun supported initialization; do not patch the digest. |
| Launch reports missing pinned historical FX evidence. | The mixed-currency roster lacks a quote for the start month's first day. | Provision the matching historical `GBPUSD=X` quote. |
| Activity remains Queued. | No running process owns the dispatcher. | Start one app process with `STRATEGY_MANAGER_WORKER_ENABLED=true`. |
| Activity becomes Failed with “Worker process could not be started.” | The child interpreter or repository working directory is unavailable. | Start from the repository root with the installed `uv` environment and inspect the server log. |
| Activity fails at a particular month. | Qualification/profile drift, missing provider evidence, or reconstruction/integrity validation stopped the month. | Read the Activity failure detail; correct the named gate, then use Restart. |
| The page does not update while the server log shows progress. | htmx/JavaScript assets did not load or a proxy cached partial responses. | Reload without cache and confirm `/static/js/strategy-manager.js` returns HTTP 200. |
| Remote POST requests return 403. | Mutating routes require localhost or the shared auth token. | Use localhost, or configure `APP_AUTH_TOKEN` and send it through the application's supported auth mechanism. |

## Safe validation commands

These checks do not create market evidence or modify Strategy Manager state:

```bash
uv run pytest tests/backtest/test_skill_discovery.py \
  tests/backtest/test_strategy_manager_lifespan.py \
  tests/test_strategy_manager_routes.py -q

uv run pytest skills/rtly-backtest-buy-and-hold/scripts/tests -q
```

Passing tests prove the discovery, route, worker-lifecycle, and Strategy
contracts against controlled fixtures. They do not provision your local
databases or prove that today's live providers will pass qualification.

## Current onboarding gaps

The following should be treated as product gaps, not operator mistakes:

1. No supported command or UI provisions qualification, roster, and active
   profile state on a clean install.
2. The configuration UI requests a `security_id` but does not expose a safe
   active-roster picker.
3. A one-security Strategy still pins the full roster, making mixed-currency
   historical FX evidence a launch-wide requirement.
4. There is no Strategy Manager historical FX backfill workflow.

Until those gaps are closed, the earliest reliable success criterion is not
“the server starts”; it is “the preflight shows a compatible qualification,
one roster, one active profile, at least one Ready month, a real roster
security ID, and any required FX evidence.”
