# Authoring a Backtest Strategy against `StrategyProtocolV1`

This document defines the v1 convention for writing a Backtest Strategy: the
layout a Strategy lives in, the interface it implements, how it constructs
signals and reads bounded market/portfolio state, how its source identity is
versioned, and how to run its contract tests.

It covers **Story 2.1's** scope -- the typed protocol, the immutable
`Signal`/`PortfolioView` value objects, pure result validation, and the
mechanically enforced safety boundary -- and **Story 2.2's** scope: the
`SKILL.md` frontmatter Skill discovery recognizes, the closed parameter
schema, and the one shared parameter validator. It does not cover the
concrete pandas-backed `MarketView` (Story 2.3) or engine invocation/
mutation (Story 2.4) -- those extend this contract without changing it.

## Layout

A real Strategy lives under `skills/<strategy-name>/`, mirroring every other
Skill in this repository:

```
skills/<strategy-name>/
  SKILL.md              # frontmatter + description (Story 2.2 discovers this)
  scripts/
    strategy.py          # StrategyProtocolV1 implementation (this story)
    tests/
      conftest.py         # adds ../ (scripts/) to sys.path
      test_contract.py     # protocol conformance + determinism tests
```

`tests/fixtures/backtest-strategies/minimal-strategy/` is a worked example of
this exact layout, used only by the repository's own test suite -- it is
**not** discoverable as a live Skill and must never be treated as a real
trading rule. Never repurpose an existing live-trading Skill (one that
places orders or touches `TraderAgent`) as a Backtest Strategy; author a new
one against this protocol instead. It also predates this section's
`kind`/`api_version` frontmatter convention on purpose, so it stays
excluded from Skill discovery -- do not add those fields to it.

## `SKILL.md` frontmatter (Story 2.2 discovery)

`app.services.backtest.skill_discovery.discover_strategies` scans the
immediate child folders of a `skills/`-shaped root for a `SKILL.md` whose
frontmatter declares `kind: backtest-strategy`. Every other Skill in this
repository -- anything with no `kind` field, or a `SKILL.md` missing
entirely -- is silently ignored: discovery never assumes a folder without
this declaration is a Strategy.

A discoverable Strategy's frontmatter is plain YAML between a leading and
trailing `---` line, parsed with a hardened safe loader (`yaml.safe_load`
semantics -- no aliases, anchors, merge keys, duplicate keys, or a second
frontmatter document):

```yaml
---
kind: backtest-strategy
name: breakout-pivot
display_name: Breakout Pivot
description: >
  Buys a confirmed pivot breakout and exits on a trailing stop.
api_version: 1
parameters:
  - name: pivot_lookback_sessions
    type: integer
    default: 20
    description: Sessions of history used to detect the pivot.
    required: false
    minimum: 5
    maximum: 252
  - name: watch_security_id
    type: string
    default: sec-aapl
    description: Security this Strategy trades.
    required: true
  - name: risk_profile
    type: enum
    default: moderate
    description: Position-sizing aggressiveness.
    required: false
    enum_values: [conservative, moderate, aggressive]
---
```

- **`kind`** -- must be exactly `backtest-strategy`. This is the one
  signal that turns an ordinary Skill into a discoverable Strategy.
- **`name`** -- lowercase kebab-case, and must equal the folder name
  (`skills/breakout-pivot/` requires `name: breakout-pivot`). This is the
  Strategy's stable ID.
- **`api_version`** -- a plain integer, currently only `1`. A bool, float,
  or string value (even `"1"`) is rejected -- the same bool/int care
  `build_strategy_source_manifest` already takes.
- **`description`** -- non-empty prose, same as every other Skill.
- **`display_name`** (optional) -- non-empty prose shown to a user. When
  omitted, discovery derives a deterministic Title Case fallback from
  `name` (`breakout-pivot` -> `Breakout Pivot`).
- **`parameters`** (optional, defaults to none) -- an ordered list of
  parameter declarations, each mapping onto `StrategyParameterV1`:
  - `name` -- non-empty, unique within the list.
  - `type` -- one of `integer`, `number`, `boolean`, `string`, `enum`.
  - `default` -- a value matching `type` (or, for `enum`, an exact
    type-and-value member of `enum_values`). A default outside a declared
    `minimum`/`maximum` is still accepted here -- discovery itself
    isolates that Strategy with an `invalid_defaults` warning, rather
    than failing SKILL.md parsing outright.
  - `description` -- non-empty prose.
  - `required` -- whether a caller must submit this parameter when
    `validate_strategy_parameters` is called with `apply_defaults=False`.
  - `minimum`/`maximum` (optional, `integer`/`number` only) -- inclusive
    bounds; `minimum` must not exceed `maximum`.
  - `enum_values` (optional, `enum` only) -- a non-empty, duplicate-free,
    homogeneous tuple of scalars (a `bool` is never interchangeable with
    an `int` here, so `[0, 1]` and `[false, true]` cannot mix).

Discovery also requires `skills/<name>/scripts/strategy.py` to exist (the
Strategy's runtime entrypoint used to build its source-identity manifest)
but never imports or executes it -- discovery is metadata-only. A missing
entrypoint, a malformed field, an unsupported `api_version`, an invalid
parameter schema, an out-of-range declared default, unsafe frontmatter
YAML, or two Strategies whose `name` or canonicalized `display_name`
collide each isolate that one Strategy with a structured warning; one bad
Strategy never aborts the scan.

## The protocol

Import everything from `app.services.backtest.strategy_protocol`:

```python
from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
    StrategyProtocolV1,
)
```

A Strategy is any object implementing three methods:

```python
class MyStrategy:
    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        ...

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        ...

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        ...
```

`StrategyProtocolV1` is `@runtime_checkable`, so `isinstance(strategy,
StrategyProtocolV1)` works for a cheap conformance smoke check -- but
`typing.Protocol` only checks that these three methods exist, not their full
signatures or return types at runtime. Always pair it with the explicit
`validate_*` functions below and with static Pyrefly checking; do not rely on
`isinstance` alone.

No method may accept or return a database connection, repository,
ORM/session, job, run, or persistence model. Only the bounded views and
JSON-compatible `parameters` mapping defined in `strategy_protocol.py` may
cross this boundary.

### `MarketViewV1`

A bounded, no-look-ahead market-data seam: `as_of_session: date`, plus two
typed accessors every Strategy method receives through `view`:

```python
from datetime import date
from app.services.backtest.strategy_protocol import MarketViewV1

def entry_signals(self, view: MarketViewV1, parameters):
    history = view.price_history("sec-aapl")  # pandas DataFrame, oldest first
    latest = view.scan_result("sec-aapl")      # HistoricalScanRecordV1 | None
    ...
```

`app.services.backtest.market_view.MarketView` (Story 2.3) is the concrete
pandas-backed implementation a future Backtest Engine (Story 2.4)
constructs once per simulated session `D` and binds to `view`:

- `price_history(security_id) -> pd.DataFrame` returns
  `security_id`'s split-continuous OHLCV history through `D`, oldest
  first, indexed by session date -- every split effective by `D` is
  already applied and no future corporate action is ever exposed
  (AD-6's `split_continuous_as_of_D` plane). Values are `Decimal` (object
  dtype), matching this codebase's deterministic-rounding policy
  everywhere else AD-6 evidence is consumed; convert a column explicitly
  (e.g. `.astype(float)`) if a vectorized numeric library needs it.
  A `security_id` this view has no pinned evidence for at all returns an
  **empty** DataFrame -- not an error, since "nothing tracked" is not a
  bound violation. A `security_id` the view *does* track, but whose
  pinned evidence does not itself extend through `D`, raises
  `MarketViewBoundError` (`.code == "bound_violation"`) immediately,
  naming the security and `D` -- never a silent truncation to an earlier,
  misleadingly-labeled "current" state.
- `scan_result(security_id) -> HistoricalScanRecordV1 | None` returns the
  latest *committed* monthly scan record visible as of `D`. A monthly
  scan candidate enters visibility only from its own recorded month-end
  `as_of_session_date` onward and remains the answer until superseded by
  that security's next committed month -- so if `D` falls inside a later
  month than the one a security's most recent committed record belongs
  to, but before that later month's own `as_of_session_date`, this still
  returns the *earlier* committed record, never backdating the later
  one. Returns `None` when no committed record is visible yet -- not an
  error.

Write and test your Strategy against `MarketViewV1` alone (never import
`market_view.py` itself, or any repository, from Strategy code -- see the
import boundary above); a future engine hands you an already-constructed
`MarketView` instance, never a way to construct one yourself.

### `PortfolioView`

An immutable, read-only snapshot of simulated portfolio state bounded to one
`as_of_session`:

```python
from decimal import Decimal
from app.services.backtest.strategy_protocol import (
    PortfolioView,
    PositionSummaryV1,
    VolatilityObservationV1,
)

view = PortfolioView(
    as_of_session=date(2026, 6, 1),
    base_currency="GBP",
    cash=Decimal("10000"),
    positions=(
        PositionSummaryV1(
            security_id="sec-aapl",
            quantity=Decimal("10"),
            average_cost=Decimal("150.00"),
        ),
    ),
    volatility_observations=(
        VolatilityObservationV1(
            security_id="sec-aapl", session=date(2026, 6, 1), value=Decimal("0.22")
        ),
    ),
)
```

- It exposes only simulated `cash`, immutable `positions`, and
  `volatility_observations` already authorized as of `as_of_session` -- never
  a live SIPP/ISA account identifier.
- Construction rejects any `volatility_observations` entry dated **after**
  `as_of_session` (`StrategyProtocolError` with code
  `future_dated_observation`) and any non-finite `cash`/`quantity`/
  `average_cost`/`value` (`NaN`/`Infinity`, rejected by pydantic's own
  `allow_inf_nan=False` config).
- Passing a `list` for `positions`/`volatility_observations` is fine -- it is
  copied into a tuple before validation, and every item is itself a frozen
  model, so mutating your original list (or dict) after construction can
  never change the view. There is no mutation method.

### `Signal`

```python
from app.services.backtest.strategy_protocol import Signal, SignalSide

signal = Signal(
    security_id="sec-aapl",
    side=SignalSide.BUY,
    session=view.as_of_session,
    rule_id="breakout_pivot_v1",
)
```

Every `Signal` is frozen and carries a `sort_key` property -- a pure
function of `(session, security_id, side, rule_id)` alone, with SELL ranked
before BUY for the same session/security. Return signals in whatever order
is convenient; the validators below always produce one canonical order.

### `parameters`

`parameters: StrategyParameters` is a read-only, JSON-compatible mapping
(`str | int | float | bool | None`, plus nested tuples/mappings of the
same). A Strategy simply reads whatever keys its `SKILL.md`'s
`parameters:` block documents (see "`SKILL.md` frontmatter" above); the
values it receives were already normalized by
`app.services.backtest.strategy_protocol.validate_strategy_parameters` --
the one shared required/type/min/max/enum authority Skill discovery, a
future engine launch, and a future UI all reuse verbatim, so a Strategy
never re-validates its own parameters.

## Validating results

Before any future engine state mutation, validate every method result with
the matching pure validator:

```python
from app.services.backtest.strategy_protocol import (
    StrategyProtocolError,
    validate_entry_signals,
    validate_exit_signals,
    validate_position_size,
)

try:
    entries = validate_entry_signals(strategy.entry_signals(view, parameters))
    size = validate_position_size(
        strategy.position_size(entries[0], view, portfolio, parameters)
    )
except StrategyProtocolError as exc:
    # exc.code is a stable, machine-readable StrategyProtocolErrorCode
    ...
```

- `validate_entry_signals`/`validate_exit_signals` reject a non-`list`
  return (`invalid_signal_container`) or any non-`Signal` element
  (`invalid_signal_element`), then return the signals sorted by `sort_key`.
- `validate_position_size` rejects `bool` and any non-`int` value
  (`invalid_position_size_type` -- note a `bool` is an `int` subclass in
  Python, so it is checked explicitly) and negative sizes
  (`negative_position_size`).

All validators are pure and side-effect-free: they never touch cash,
positions, the ledger, staging, or run state themselves -- that is Story
2.4's job, once these checks pass.

## Source identity and versioning

A Strategy's version is a canonical source-identity manifest, built the same
way as every detector and the yfinance ingestion path -- through
`build_source_manifest`, never a second canonicalizer:

```python
from pathlib import Path
from app.services.backtest.source_manifest import build_strategy_source_manifest

manifest = build_strategy_source_manifest(
    project_root=Path(__file__).resolve().parents[3],
    strategy_id="breakout_pivot_v1",
    api_version=1,
    allowlist=("skills/breakout-pivot/scripts/strategy.py",),
    defaults={"pivot_lookback_sessions": 20},
    python_runtime="3.14",
    dependency_versions={"pydantic": "2.13.4"},
)
```

- `allowlist` is one explicit, closed list of POSIX-relative runtime source
  paths (sorted, UTF-8, newline-normalized). Only list files your Strategy
  actually imports at runtime -- never `scripts/tests/`, `__pycache__`,
  bytecode, logs, reports, or generated output; those are rejected outright.
- `api_version` is a plain positive integer, represented canonically as its
  decimal string in the manifest.
- The resulting digest changes when runtime source or `defaults`/config
  changes materially, and is unchanged by newline-style-only edits or by
  anything outside the allowlist.

## Running contract tests

Every Strategy should have a `scripts/tests/` suite proving protocol
conformance, deterministic output, parameter pass-through, bounded inputs,
and immutable portfolio data -- the same shape as
`tests/fixtures/backtest-strategies/minimal-strategy/scripts/tests/`. Run a
Strategy's own contract tests directly:

```bash
uv run pytest skills/<strategy-name>/scripts/tests -q
```

For the repository's fixture Strategy:

```bash
uv run pytest tests/fixtures/backtest-strategies/minimal-strategy/scripts/tests -q
```

Strategy functions always run in-process, called directly as Python
methods. Never invoke a Strategy's script through a per-signal, per-ticker,
or per-session subprocess call.

## The safety boundary (AD-10)

A Strategy runtime module may only import:

- `app.services.backtest.strategy_protocol` (the versioned host API), and
- deterministic standard-library calculation modules on the mechanically
  enforced allowlist.

Methodology code stays inside the Skill's hashed `scripts/strategy.py`. A
Strategy does not import `app.core` helpers: keeping application behavior out
of the runtime makes the Skill independently releasable and prevents an
unhashed shared module change from altering replay behavior.

It must never import -- directly, aliased, via `from ... import ...`, or
transitively through any other first-party module -- `TraderAgent`,
anything under `app.agents`, live portfolio/trade/cash/position
repositories or the `Connect`/DB-session factory under `app.repositories`,
or any order-submission path. This is enforced mechanically, not just by
convention: `tests/backtest/test_strategy_runtime_import_boundary.py`
statically parses a Strategy module's import graph (AST only -- nothing is
ever executed) and follows every local `app.*` import it finds, failing with
a diagnostic naming both the offending module and the forbidden dependency
it reached, however many hops away.

**This is a bounded-interface guard, not a Python sandbox.** First-party
in-process Strategy code is trusted and code-reviewed like any other module
in this repository; nothing here provides process isolation, resource
limits, or protection against arbitrary Python execution. Untrusted
third-party Skills remain explicitly out of scope for this protocol.
