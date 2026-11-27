"""Replay a stored backtest run in memory, optionally holding idle cash in SPY (#140).

The run's own pinned manifest, Strategy, price evidence and FX are resolved
through the worker's resolution path from read-only database connections, so
the replay matches what the worker executed. Nothing is written to the
databases; results go to ``docs/research/gh140/<arm>.json``.

Usage: ``uv run python scripts/research/gh140_sleeve_replay.py --arm off|sleeve``
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core import config  # noqa: E402
from app.repositories.backtest_repo import BacktestRepository  # noqa: E402
from app.repositories.historical_price_repo import (  # noqa: E402
    HistoricalPriceRepository,
)
from app.services.backtest import worker as worker_module  # noqa: E402
from app.services.backtest.backtest_engine import (  # noqa: E402
    EntryFillEventV1,
    ExitFillEventV1,
    InMemorySessionBatchSink,
    TerminalSettlementEventV1,
    SecurityMarketDataV1,
    run_simulation,
)
from app.services.backtest.currency import prepare_fx_closes  # noqa: E402
from app.services.backtest.market_view import MarketView  # noqa: E402
from app.services.backtest.run_input_manifest import (  # noqa: E402
    RunInputManifestV3,
)
from app.services.backtest.trading_calendar import TradingCalendar  # noqa: E402

#: The stored Yau-Weinstein run (Jan 2000 - Aug 2026, upgrades on, MA 150).
RUN_ID = "93f205c5-35ab-4007-afb8-b22ada16264b"


def read_only(path: Path):
    """A connection factory that can never write to ``path``."""
    return lambda: sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def resolve(run_id: str):
    """Resolve a stored run's inputs exactly as the worker does."""
    repo = BacktestRepository(read_only(config.BACKTEST_DB))
    prices = HistoricalPriceRepository(read_only(config.HISTORICAL_PRICE_CACHE))
    backtest = repo.strategy_run(run_id)
    if backtest is None:
        raise SystemExit(f"run {run_id} not found")
    engine = worker_module.BacktestExecutionEngine(repo, backtest, prices, ROOT)
    # The run was pinned under the engine source it ran with; a sleeve build
    # changes that source, so accept the run's own recorded contract here.
    with patch.object(
        worker_module,
        "current_execution_contract_digest",
        lambda _root: backtest.execution_contract_digest,
    ):
        manifest, strategy, security_data, fx_evidence = engine._resolve()
    windows = engine._price_windows(manifest, security_data)
    return repo, prices, engine, manifest, strategy, security_data, fx_evidence, windows


def week_end(session: date) -> bool:
    """Whether ``session`` is the last XNYS session of its ISO week."""
    following = TradingCalendar().first_session_in_range(
        "XNYS", session + timedelta(days=1), session + timedelta(days=10)
    )
    return following is None or following.isocalendar()[:2] != session.isocalendar()[:2]


def weekly_exits(strategy: Any, mode: str) -> None:
    """Research variant (test 4): hold some exits until the week's last session.

    ``sma`` defers exits whose only reason is the close below the 150-session
    SMA (a weekly close below a ~30-week average); ``sma+upgrade`` also
    defers upgrade exits. The maximum-loss stop and stage exits stay daily.
    """
    original = strategy.exit_signals

    def filtered(view: Any, portfolio: Any, parameters: Any) -> list:
        signals = original(view, portfolio, parameters)
        if week_end(view.as_of_session):
            return signals
        kept = []
        for signal in signals:
            codes = {
                reason.code
                for reason in (signal.explanation.reasons if signal.explanation else ())
            }
            if codes == {"close_below_sma150"}:
                continue
            if mode == "sma+upgrade" and signal.rule_id.endswith("upgrade_exit_v1"):
                continue
            kept.append(signal)
        return kept

    strategy.exit_signals = filtered


def replay(
    arm: str, overrides: dict[str, Any] | None = None, weekly: str = "none"
) -> dict[str, Any]:
    """Run one arm, with optional parameter overrides, and return its equity
    curve, sleeve activity and trade summary."""
    repo, prices, engine, manifest, strategy, security_data, fx, windows = resolve(
        RUN_ID
    )
    if weekly != "none":
        weekly_exits(strategy, weekly)
    if overrides:
        manifest = manifest.model_copy(
            update={"parameters": {**manifest.parameters, **overrides}}
        )
    prepared_planes: dict = {}
    scan_cache: dict = {}
    scan_cache_month: dict = {}
    prepared_fx = prepare_fx_closes(fx) if fx is not None else None
    benchmark = engine._regime_benchmark_access

    def market_view_factory(session: date) -> MarketView:
        return MarketView(
            as_of_session=session,
            profile_hash=manifest.profile_hash,
            security_price_revisions={
                item.security_id: item.price_revision
                for item in manifest.securities
                if worker_module._shows_prices(windows, item.security_id, session)
            },
            selected_universe=tuple(item.security_id for item in manifest.securities),
            backtest_repo=repo,
            historical_price_repo=prices,
            base_currency=manifest.base_currency,
            fx_evidence=fx,
            prepared_fx=prepared_fx,
            regime_benchmark=manifest.regime_benchmark
            if isinstance(manifest, RunInputManifestV3)
            else None,
            regime_benchmark_access=benchmark,
            prepared_planes=MappingProxyType(prepared_planes),
            prepared_plane_cache=prepared_planes,
            price_accesses=cast(
                Any,
                {
                    item.security_id: item.price_access
                    for item in security_data
                    if item.price_access is not None
                },
            ),
            scan_cache=scan_cache,
            scan_cache_month=scan_cache_month,
        )

    extra: dict[str, Any] = {}
    if arm == "sleeve":
        if not isinstance(manifest, RunInputManifestV3) or benchmark is None:
            raise SystemExit("the run has no pinned SPY benchmark to hold cash in")
        extra["cash_sleeve"] = SecurityMarketDataV1(
            security_id=manifest.regime_benchmark.security_id,
            price_access=cast(Any, benchmark),
        )
    started = time.monotonic()
    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=market_view_factory,
        security_market_data=security_data,
        fx_evidence=fx,
        prepared_fx=prepared_fx,
        sink=InMemorySessionBatchSink(),
        prepared_planes=prepared_planes,
        terminal_exits=manifest.terminal_exits,
        **extra,
    )
    kinds: dict[str, int] = {}
    for event in (*output.events, *output.cash_sleeve_events):
        kinds[event.kind] = kinds.get(event.kind, 0) + 1
    sleeve = {v.session: v.value_base for v in output.cash_sleeve_values}
    traded: dict[str, float] = {}
    closed: list[float] = []
    for event in output.events:
        if isinstance(event, EntryFillEventV1):
            value = float(event.cost_base)
        elif isinstance(event, ExitFillEventV1):
            value = float(event.proceeds_base)
        else:
            if isinstance(event, TerminalSettlementEventV1):
                closed.append(float(event.realized_pnl_base / event.cost_basis_base))
            continue
        day = event.fill_session.isoformat()
        traded[day] = traded.get(day, 0.0) + value
        if isinstance(event, ExitFillEventV1):
            closed.append(float(event.realized_pnl_base / event.cost_basis_base))
    return {
        "run_id": RUN_ID,
        "arm": arm,
        "overrides": overrides or {},
        "weekly_exits": weekly,
        "traded_base_by_day": traded,
        "closed_trade_returns": closed,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "event_counts": kinds,
        "final_cash_base": str(output.final_cash_base),
        "equity_curve": [
            {
                "date": p.session.isoformat(),
                "cash": str(p.cash_base),
                "positions": str(p.positions_value_base),
                "sleeve": str(sleeve.get(p.session, 0)),
                "total": str(p.total_equity_base),
            }
            for p in output.equity_curve
        ],
    }


def parity(result: dict[str, Any]) -> dict[str, Any]:
    """Compare a replayed equity curve with the stored run's."""
    with read_only(config.BACKTEST_DB)() as db:
        stored = dict(
            db.execute(
                "SELECT date, total_equity_base FROM equity_curve WHERE run_id=?",
                (RUN_ID,),
            ).fetchall()
        )
    replayed = {p["date"]: p["total"] for p in result["equity_curve"]}
    mismatched = [d for d in stored if stored[d] != replayed.get(d)]
    return {
        "stored_points": len(stored),
        "replayed_points": len(replayed),
        "mismatched_points": len(mismatched),
        "first_mismatch": mismatched[:1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("off", "sleeve"), required=True)
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="override one run parameter, e.g. maximum_loss_pct=8.0",
    )
    parser.add_argument(
        "--weekly", choices=("none", "sma", "sma+upgrade"), default="none"
    )
    parser.add_argument("--label", help="output file name (default: the arm)")
    parser.add_argument("--out", default="gh140", help="folder under docs/research")
    args = parser.parse_args()
    overrides = {
        name: json.loads(value)
        for name, value in (item.split("=", 1) for item in args.param)
    }
    result = replay(args.arm, overrides, args.weekly)
    if args.arm == "off" and not overrides and args.weekly == "none":
        result["parity_with_stored_run"] = parity(result)
    output = ROOT / "docs/research" / args.out
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{args.label or args.arm}.json").write_text(json.dumps(result, indent=1))
    summary = {
        k: v
        for k, v in result.items()
        if k not in {"equity_curve", "traded_base_by_day", "closed_trade_returns"}
    }
    summary["final_total"] = result["equity_curve"][-1]["total"]
    print(json.dumps(summary, indent=1), flush=True)


if __name__ == "__main__":
    main()
