"""Run the proposed Moving Average Skill on one pinned ETF at a time.

This is a supplemental diagnostic. It derives a one-security run manifest from
the frozen Moving Average run and uses a separate, temporary evidence database.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.research.gh66_experiment as gh  # noqa: E402


STRATEGY_ID = "rtly-backtest-moving-average"
SOURCE_BACKTEST_DB = Path("/Users/edyau/Git/stocks.portfolio.agentic/data/backtest.db")
PARENT_MANIFEST = ROOT / "docs/research/gh66/experiment_manifest.json"
RESULT_DIR = ROOT / "docs/research/gh66/single-instrument"
EVIDENCE_SETUP = RESULT_DIR / "evidence-setup.json"


def _evidence_cache_path(setup: dict[str, Any]) -> Path:
    cache = Path(setup["historical_cache"])
    if not cache.is_absolute():
        cache = EVIDENCE_SETUP.parent / cache
    expected = setup.get("historical_cache_sha256")
    if (
        not cache.is_file()
        or hashlib.sha256(cache.read_bytes()).hexdigest() != expected
    ):
        raise RuntimeError(
            "pinned single-instrument evidence cache is unavailable or changed"
        )
    return cache


def _resolver_for(instrument: dict[str, str], position_cap: int):
    def resolve(
        backtest_db: Path,
        historical_db: Path,
        strategy_id: str,
        start_month: str,
        end_month: str,
        *,
        spy_benchmark: bool = False,
        skills_root: Path | None = None,
    ) -> tuple[Any, ...]:
        if strategy_id != STRATEGY_ID or spy_benchmark or skills_root is not None:
            raise ValueError("single-instrument runner supports only Moving Average")

        backtest_repo = gh.BacktestRepository(gh._read_only_connection(backtest_db))
        prices = gh.HistoricalPriceRepository(gh._read_only_connection(historical_db))
        original = gh._original_rows(backtest_db)[strategy_id]
        original_json = backtest_repo.run_input_manifest_json(
            original["run_input_manifest_digest"]
        )
        if original_json is None:
            raise RuntimeError("saved Moving Average manifest is unavailable")
        saved_manifest = gh.read_run_input_manifest(original_json)
        descriptor = next(
            item
            for item in gh.discover_strategies(gh.config.SKILLS_DIR).strategies
            if item.strategy_id == strategy_id
        )

        raw = saved_manifest.model_dump(mode="json")
        parameters = dict(raw["parameters"])
        parameters["selected_securities"] = [instrument["security_id"]]
        parameters["max_concurrent_positions"] = position_cap
        if instrument["security_id"] == parameters.get(
            "regime_filter_benchmark_security_id"
        ):
            # V3 requires its separately pinned benchmark to sit outside the
            # trade universe. For SPY, use its selected price history directly.
            raw.pop("regime_benchmark", None)
            raw["schema_version"] = "run_input_manifest.v2"
            parameters["regime_filter_enabled"] = False
            parameters["block_buy_on_downtrend_enabled"] = True
        raw["parameters"] = parameters
        raw["securities"] = [
            {
                "security_id": instrument["security_id"],
                "price_revision": instrument["price_revision"],
                "action_revision": instrument["action_revision"],
                "fx_revision": instrument["fx_revision"],
            }
        ]
        selection = raw["universe_selection"]
        selection["canonical_security_ids"] = [instrument["security_id"]]
        selection["run_universe_digest"] = gh.run_universe_digest(
            (instrument["security_id"],),
            universe_schema=selection["universe_schema"],
            mode=selection["universe_mode"],
            parameter=selection["universe_parameter"],
            profile_hash=selection["profile_hash"],
        )
        runtime = gh._host_execution_identity()
        raw.update(
            {
                "engine_version": gh.ENGINE_VERSION,
                "protocol_schema_version": gh.PROTOCOL_SCHEMA_VERSION,
                "market_view_source_digest": runtime["market_view_source_digest"],
                "ledger_action_metrics_digest": runtime["ledger_action_metrics_digest"],
                "runtime_lock_digest": runtime["runtime_lock_digest"],
                "calendar_session_table_digest": runtime[
                    "calendar_session_table_digest"
                ],
                "python_runtime": runtime["python_runtime"],
                "timezone_dataset_version": runtime["timezone_dataset_version"],
                "detector_source_digests": runtime["detector_source_digests"],
                "strategy_api_version": descriptor.api_version,
                "strategy_source_digest": descriptor.source_digest,
                "start_month": start_month,
                "end_month": end_month,
            }
        )
        current_manifest = gh.read_run_input_manifest(
            json.dumps(raw, sort_keys=True, separators=(",", ":"))
        )
        if (
            current_manifest.execution_contract_digest()
            != gh._read_frozen_manifest(PARENT_MANIFEST)["host_execution_contract"][
                "digest"
            ]
        ):
            raise RuntimeError("single-instrument run changed the execution contract")

        digest = current_manifest.digest()
        raw_json = current_manifest.canonical_json()
        overlay = gh._ManifestOverlay(backtest_repo, digest, raw_json)
        saved_run = backtest_repo.strategy_run(original["run_id"])
        run = saved_run.model_copy(
            update={
                "parameters": current_manifest.parameters,
                "strategy_api_version": descriptor.api_version,
                "strategy_source_digest": descriptor.source_digest,
                "start_month": start_month,
                "end_month": end_month,
                "ordered_month_digest": current_manifest.ordered_month_digest,
                "run_input_manifest_digest": digest,
                "execution_contract_digest": current_manifest.execution_contract_digest(),
                "manifest_version": current_manifest.schema_version,
                "universe_selection": current_manifest.universe_selection,
                "regime_benchmark": getattr(current_manifest, "regime_benchmark", None),
            }
        )
        resolver = gh.BacktestExecutionEngine(
            repository=overlay,
            backtest=run,
            prices=prices,
            project_root=gh.config.ROOT_DIR,
        )
        manifest, strategy, market_data, fx_evidence = resolver._resolve()
        return (
            backtest_repo,
            overlay,
            prices,
            run,
            resolver,
            manifest,
            strategy,
            market_data,
            fx_evidence,
        )

    return resolve


def run_instrument(
    name: str, instrument: dict[str, str], position_cap: int
) -> dict[str, Any]:
    parent = gh._read_frozen_manifest(PARENT_MANIFEST)
    setup = json.loads(EVIDENCE_SETUP.read_text())
    history_db = _evidence_cache_path(setup)
    derived = {key: value for key, value in parent.items() if key != "experiment_id"}
    derived["source_database_readonly_signatures"]["historical_prices"] = (
        gh._database_signature(history_db)
    )
    derived["single_instrument_scope"] = {
        "instrument": name,
        **instrument,
        "source": "pinned SPY evidence plus QQQ evidence from the yfinance adapter",
        "selection": (
            "one security; original Moving Average rules and SPY downtrend gate; "
            "SPY uses its own selected price history for the gate, while QQQ uses "
            "the separately pinned SPY reference"
        ),
        "position_cap": position_cap,
        "parent_experiment_id": parent["experiment_id"],
    }
    derived["experiment_id"] = gh._sha256(derived)
    label = f"{name.lower()}-cap{position_cap}"
    manifest_path = RESULT_DIR / f"{label}-manifest.json"
    gh._atomic_json(manifest_path, derived)

    original_resolver = gh._current_manifest_and_resolver
    gh._current_manifest_and_resolver = _resolver_for(instrument, position_cap)
    target = RESULT_DIR / f"{label}-result.json"
    try:
        result = gh.run_one(
            manifest_path=manifest_path,
            backtest_db=SOURCE_BACKTEST_DB,
            historical_db=history_db,
            strategy_id=STRATEGY_ID,
            variant="proposed",
            seed=None,
            output=target,
        )
    finally:
        gh._current_manifest_and_resolver = original_resolver

    result["instrument"] = name
    result["position_cap"] = position_cap
    result["instrument_security_id"] = instrument["security_id"]
    result["price_revision"] = instrument["price_revision"]
    result["single_instrument_experiment_id"] = derived["experiment_id"]
    gh._atomic_json(target, result)
    return result


def main() -> None:
    setup = json.loads(EVIDENCE_SETUP.read_text())
    fx_revision = setup["fx"]["price_revision"]
    instruments = {
        "SPY": {
            "security_id": setup["spy"]["security_id"],
            "price_revision": setup["spy"]["price_revision"],
            "action_revision": setup["spy"]["action_revision"],
            "fx_revision": fx_revision,
        },
        "QQQ": {
            "security_id": setup["qqq"]["security_id"],
            "price_revision": setup["qqq"]["data_revision"],
            "action_revision": setup["qqq"]["data_revision"],
            "fx_revision": fx_revision,
        },
    }
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    for name, instrument in instruments.items():
        for position_cap in (10, 1):
            result = run_instrument(name, instrument, position_cap)
            print(
                f"{name} cap={position_cap}: {result['status']} "
                f"return={result['metrics']['total_return']:.4%} "
                f"CAGR={result['metrics']['cagr']:.4%} "
                f"max_drawdown={result['metrics']['max_drawdown']:.4%}",
                flush=True,
            )


if __name__ == "__main__":
    main()
