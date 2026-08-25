# Strategy Manager onboarding

This guide explains how to get from a local checkout to a completed Strategy
Manager backtest using only supported routes and UI actions -- no manual
SQLite edits, no guessed or copied security IDs, and no bypassing readiness.

Strategy Manager is bootstrappable from a clean checkout. `StrategyBootstrapService`
(`app/services/backtest/strategy_bootstrap_service.py`) qualifies the historical
provider contract, captures an immutable reconstruction roster and identity
evidence, validates a compatible scanner-data profile, and activates it --- all
through one guarded **Set up Strategy Manager** action. Nothing in this guide
requires opening a SQLite client, hand-provisioning a database, or inventing a
placeholder `security_id`.

## The supported journey

```text
Readiness (read-only)
        |
        v
Bootstrap (Setup)  --  qualification -> roster/identity capture -> profile activation
        |
        v
Historical initialization  --  ready monthly scanner-data coverage
        |
        v
Universe selection  --  choose which active-profile securities a Run trades
        |
        v
Strategy configuration + launch  --  evidence preparation -> Backtest
        |
        v
Result
```

Every stage above is a real, guarded route. Reads, page loads, readiness
checks, and diagnostics never create, repair, activate, or queue anything --
only the guarded POST routes below do.

| Stage | Read route | Mutating route |
| --- | --- | --- |
| Readiness | `GET /strategy-manager/readiness` | -- |
| Diagnostics | `GET /strategy-manager/diagnostics` | -- |
| Setup (Bootstrap) | `GET /strategy-manager/setup` | `POST /strategy-manager/setup` |
| Historical initialization | `GET /strategy-manager/initialization` | `POST /strategy-manager/initialization` |
| Universe selection | `GET /strategy-manager/configuration/universe` | (submitted as part of launch) |
| Strategy configuration + launch | `GET /strategy-manager/configuration` | `POST /strategy-manager/configuration` |
| Result | `GET /strategy-manager/results/{run_id}` | -- |

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

Set it to `false` only for additional web processes that must not own the
local dispatcher. If the worker is disabled everywhere, jobs remain Queued.

## Confirm that Strategy Skills are discoverable

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

## Check readiness

Open **Strategy Manager** -> **Readiness**, or fetch `GET
/strategy-manager/readiness` directly. This is a pure read: it never creates,
repairs, activates, or queues anything.

Readiness reports six independent prerequisites plus worker health:

| Prerequisite | Meaning when not Ready |
| --- | --- |
| Qualification | The installed provider/calendar/fixture/probe contract has not passed certification. |
| Roster | No immutable reconstruction roster has been captured yet. |
| Active profile | No scanner-data version is activated. |
| Coverage | No Ready monthly snapshot coverage exists yet for the active profile. |
| Worker | The local dispatcher is disabled, unavailable, or busy. |
| Strategy discovery | No Strategy Skill was discovered, or discovery reported a warning. |

Each row shows a state, an explanation, and (where applicable) one supported
recovery action -- generally a link to Setup or Historical initialization.
`GET /strategy-manager/diagnostics` adds bounded recent-failure detail for the
same prerequisites without exposing secrets, raw provider payloads, local
paths, or stack traces.

On a genuinely clean checkout, Qualification, Roster, and Active profile all
start out not-Ready. That is expected -- Setup (below) is what makes them
Ready, not a manual step.

## Set up Strategy Manager (Bootstrap)

Open **Strategy Manager** -> **Setup**, or fetch `GET /strategy-manager/setup`.
The page shows either "already set up" (with the activation time) or a
one-button confirmation form.

Selecting **Set up Strategy Manager** submits `POST /strategy-manager/setup`
(local access, or a shared auth token, required -- see Troubleshooting). This
enqueues one Bootstrap activity that a worker then advances through three
closed stages, shown on the Activity page:

1. **Qualification** -- verifies the installed yfinance/pandas/calendar
   provider contract still matches a qualified fixture-and-probe result.
2. **Roster and identity capture** -- captures an immutable reconstruction
   roster from the required DataHub S&P 500, TradingView US, and TradingView
   UK sources, and resolves each member's market identity.
3. **Profile validation and activation** -- validates a scanner-data profile
   bound to that roster and, only after every prior stage has succeeded,
   activates it.

Submitting Setup again after Bootstrap has already produced a compatible
active profile is a verified no-op: it returns the existing activity rather
than starting another one or reporting a conflict.

If a stage fails, the Activity page shows a stable failure code and a safe,
actionable reason (never a raw provider payload, secret, or stack trace).
Fix the named cause and use **Retry setup**.

## Initialize historical coverage

Once Setup is complete (Readiness shows Qualification, Roster, and Active
profile all Ready):

1. Open **Strategy Manager** -> **Historical initialization**, or fetch `GET
   /strategy-manager/initialization`.
2. Choose fully completed months in `YYYY-MM` format. The current month is
   not eligible.
3. For an initial smoke test, request one month. A large roster and a long
   range can require substantial network work.
4. Select **Initialize** (`POST /strategy-manager/initialization`) and leave
   the server running.
5. Watch the Activity page until it reaches Complete. The page polls every
   three seconds.
6. Return to Strategy Manager and confirm the month appears in a Ready
   interval.

Initialization reconstructs every member of the active profile. A month
already covered is a no-op (the route reports "Coverage is already Ready for
the requested period" and does not enqueue another attempt). One failed or
partial member prevents the month from being presented as Ready. Restarting a
failed attempt reuses committed shared evidence but replays the attempt from
the beginning.

## Select a universe and launch a Backtest

Open **Strategy Manager** -> **Configure a Backtest** (`GET
/strategy-manager/configuration`).

1. Choose a Strategy. Start with **Buy and Hold Backtest** -- it has the
   fewest signal gates.
2. Choose the securities the Run trades. The universe selector (`GET
   /strategy-manager/configuration/universe`) lists every active-profile
   roster member with its provider symbol, MIC, and currency -- no manual
   lookup or hand-typed ID is needed. Search narrows the list; "select the
   whole active roster" is on by default and can be turned off to hand-pick a
   subset. The selector rejects a stale profile, an unknown ID, or an empty
   selection rather than silently accepting it; a duplicate ID is collapsed
   into one entry rather than rejected.
3. Keep `fixed_shares` small, for example `10`.
4. Set `entry_on_or_after` to a date on or before the selected Ready period.
5. Select a start and end month inside one Ready interval.
6. Enter a positive starting capital, for example `10000.00`.
7. Select the base currency and choose **Run Backtest**
   (`POST /strategy-manager/configuration`).
8. Keep the server running while the Activity page is Queued or Running.
9. When it reaches Complete, choose **Review this Backtest**
   (`GET /strategy-manager/results/{run_id}`).

Launch first runs an evidence-preparation activity that seals exactly the
selected universe's price/action (and, where needed, FX) evidence into an
immutable Run-input manifest; the Backtest itself then replays only that
pinned evidence. Signals are evaluated after a session close and accepted
orders fill at the next exchange session's open. The v1 engine is long-only,
does not pyramid, and permits only full-position exits.

### FX requirement

If any selected security's currency differs from the chosen base currency,
launch requires a content-addressed FX quote (for example `GBPUSD=X`) for the
first calendar day of the start month. If that quote is unavailable, launch
fails with:

```text
Pinned historical FX evidence is unavailable for '<security-id>' as of YYYY-MM-01.
```

Historical FX backfill is not currently part of the Strategy Manager UI.
Selecting a single-currency universe (or a universe that already matches the
chosen base currency) avoids this requirement entirely -- note that "select
the whole active roster" is on by default, so a mixed-currency roster will
hit this requirement out of the box unless you narrow the selection or
provision the matching FX quote.

## Troubleshooting

| Symptom | Cause | What to do |
| --- | --- | --- |
| **Setup** shows "already set up" but you expected fresh provisioning. | A compatible active profile already exists; Setup is a verified no-op by design. | Check `GET /strategy-manager/readiness` to confirm what is active. |
| **Initialize** is disabled: "No active scanner-data version is configured." | No active snapshot profile exists yet. | Complete **Setup** first. |
| **Initialize** is disabled: "Historical data providers have not passed certification." | No compatible passed qualification exists. | Retry **Setup**; check internet access and provider throttling. |
| **Run Backtest** is disabled. | No Ready monthly coverage exists, or coverage integrity validation failed. | Complete **Historical initialization** and resolve any red coverage alert. |
| No Strategies appear. | Discovery rejected the Skill metadata or runtime. | Run the discovery preflight above and fix the named folder warning. |
| A run completes with no trades. | The entry date is after the selected period, there is insufficient warm-up, or the Strategy's signal gates never passed. | Smoke-test Buy and Hold on a Ready period before a signal-heavy Strategy. |
| Launch reports missing pinned historical evidence. | `backtest.db` references a price/action revision absent from `historical_price_cache.db`. | Rerun **Historical initialization** for the affected period; do not patch the digest by hand. |
| Launch reports missing pinned historical FX evidence. | The selected universe's currencies do not all match the base currency, and no matching FX quote is pinned for the start month. | Choose a single-currency universe or base currency, or provision the matching FX quote. |
| Activity remains Queued. | No running process owns the dispatcher. | Start one app process with `STRATEGY_MANAGER_WORKER_ENABLED=true`. |
| Activity becomes Failed with "Worker process could not be started." | The child interpreter or repository working directory is unavailable. | Start from the repository root with the installed `uv` environment and inspect the server log. |
| Activity fails at a particular stage or month. | Qualification/profile drift, missing provider evidence, or reconstruction/integrity validation stopped the stage or month. | Read the Activity failure detail; correct the named gate, then use Restart/Retry. |
| The page does not update while the server log shows progress. | htmx/JavaScript assets did not load or a proxy cached partial responses. | Reload without cache and confirm `/static/js/strategy-manager.js` returns HTTP 200. |
| Remote POST requests return 403. | Mutating routes require localhost or the shared auth token. | Use localhost, or configure `APP_AUTH_TOKEN` and send it through the application's supported auth mechanism. |

## Safe validation commands

These checks do not create market evidence or modify Strategy Manager state:

```bash
uv run pytest tests/backtest/test_skill_discovery.py \
  tests/backtest/test_strategy_manager_lifespan.py \
  tests/test_strategy_manager_routes.py -q

uv run pytest tests/backtest/test_strategy_manager_clean_checkout_journey.py -q

uv run pytest skills/rtly-backtest-buy-and-hold/scripts/tests -q
```

Passing tests prove the discovery, route, worker-lifecycle, Bootstrap,
initialization, preparation, and Backtest contracts against controlled
fixtures. They do not provision your local databases with live data, and they
do not prove that today's live providers will pass qualification -- the
Fixture path used in tests is visibly distinguished from Production and is
never a substitute for running **Setup** for real.
