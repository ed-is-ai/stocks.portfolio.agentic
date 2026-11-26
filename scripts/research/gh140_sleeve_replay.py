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
from datetime import date
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
    InMemorySessionBatchSink,
    SecurityMarketDataV1,
    run_simulation,
)
from app.services.backtest.currency import prepare_fx_closes  # noqa: E402
from app.services.backtest.market_view import MarketView  # noqa: E402
from app.services.backtest.run_input_manifest import (  # noqa: E402
    RunInputManifestV3,
)

#: The stored Yau-Weinstein run (Jan 2000 - Aug 2026, upgrades on, MA 150).
RUN_ID = "93f205c5-35ab-4007-afb8-b22ada16264b"
OUTPUT = ROOT / "docs/research/gh140"


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


def replay(arm: str) -> dict[str, Any]:
    """Run one arm and return its equity curve and sleeve activity."""
    repo, prices, engine, manifest, strategy, security_data, fx, windows = resolve(
        RUN_ID
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
    return {
        "run_id": RUN_ID,
        "arm": arm,
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
    args = parser.parse_args()
    result = replay(args.arm)
    if args.arm == "off":
        result["parity_with_stored_run"] = parity(result)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / f"{args.arm}.json").write_text(json.dumps(result, indent=1))
    summary = {k: v for k, v in result.items() if k != "equity_curve"}
    summary["final_total"] = result["equity_curve"][-1]["total"]
    print(json.dumps(summary, indent=1), flush=True)


if __name__ == "__main__":
    main()
