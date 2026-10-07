"""Run the frozen GH #66 SPY benchmark with strict tuple normalization."""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import MethodType
from typing import Any

import scripts.research.gh66_experiment as experiment
from app.services.backtest.strategy_protocol import (
    EntrySelectionDecisionV1,
    EntrySelectionState,
    InitialEntrySelectionV1,
    Signal,
    SignalSide,
)


def _normalize_security_tuple() -> None:
    model = experiment.RunInputManifestV2
    original_validate = model.model_validate.__func__

    def validate(cls: type[Any], value: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("securities"), list):
            value = {**value, "securities": tuple(value["securities"])}
        return original_validate(cls, value, *args, **kwargs)

    model.model_validate = classmethod(validate)


def _install_passive_spy_entry() -> None:
    original_resolver = experiment._current_manifest_and_resolver

    def resolve(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        resolved = original_resolver(*args, **kwargs)
        if kwargs.get("spy_benchmark"):
            manifest, strategy = resolved[5], resolved[6]
            security_ids = tuple(item.security_id for item in manifest.securities)
            if len(security_ids) != 1:
                raise RuntimeError("SPY benchmark must pin exactly one security")
            security_id = security_ids[0]
            rule_id = "spy_buy_and_hold_initial_entry_v1"

            def initial_entry_selection(
                _strategy: Any, view: Any, _parameters: Any
            ) -> InitialEntrySelectionV1:
                signal = Signal(
                    security_id=security_id,
                    side=SignalSide.BUY,
                    session=view.as_of_session,
                    rule_id=rule_id,
                    priority=Decimal(1),
                )
                return InitialEntrySelectionV1(
                    session=view.as_of_session,
                    metric_id="spy_passive_buy_and_hold",
                    metric_version="v1",
                    rule_id=rule_id,
                    decisions=(
                        EntrySelectionDecisionV1(
                            security_id=security_id,
                            rank=1,
                            state=EntrySelectionState.SELECTED,
                        ),
                    ),
                    signals=(signal,),
                )

            strategy.initial_entry_selection = MethodType(
                initial_entry_selection, strategy
            )
        return resolved

    experiment._current_manifest_and_resolver = resolve


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--backtest-db", type=Path, required=True)
    parser.add_argument("--historical-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    adapter_manifest_path = args.manifest.parent / "spy_benchmark_adapter_manifest.json"
    adapter_manifest = json.loads(adapter_manifest_path.read_text())
    current_adapter_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    frozen = experiment._read_frozen_manifest(args.manifest)
    if (
        adapter_manifest.get("adapter_sha256") != current_adapter_sha
        or adapter_manifest.get("parent_experiment_id") != frozen["experiment_id"]
        or adapter_manifest.get("result_path") != args.output.as_posix()
    ):
        raise RuntimeError("SPY adapter differs from its frozen supplemental manifest")

    _normalize_security_tuple()
    _install_passive_spy_entry()
    result = experiment.run_one(
        manifest_path=args.manifest,
        backtest_db=args.backtest_db,
        historical_db=args.historical_db,
        strategy_id="SPY",
        variant="legacy",
        seed=None,
        output=args.output,
    )
    result["benchmark_adapter_id"] = adapter_manifest["adapter_id"]
    result["benchmark_adapter_sha256"] = current_adapter_sha
    experiment._atomic_json(args.output, result)
    print(
        "DONE SPY benchmark "
        f"sessions={len(result['equity_curve'])} "
        f"seconds={result['performance']['elapsed_seconds']:.2f} "
        f"digest={result['simulation_result_sha256']}"
    )


if __name__ == "__main__":
    main()
