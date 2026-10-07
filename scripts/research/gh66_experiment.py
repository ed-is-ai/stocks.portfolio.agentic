"""Frozen GH #66 experiment: read-only inputs, in-memory replays, standalone outputs."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import nullcontext
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import html
import io
import json
import os
from pathlib import Path
import resource
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from types import MappingProxyType
from typing import Any, Iterable, cast

from app.core import config
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import (
    HistoricalEvidenceReadHandle,
    HistoricalPriceRepository,
)
from app.services.backtest.backtest_engine import (
    CandidateAuditV1,
    CandidateAuditDisposition,
    EntryFillEventV1,
    ExitFillEventV1,
    EquityCurvePointV1,
    InMemorySessionBatchSink,
    Signal,
    SignalSide,
    TradeLogEvent,
    run_simulation,
    _engine_signal_sort_key,
    _slot_rank,
)
from app.services.backtest.currency import prepare_fx_closes
from app.services.backtest.market_planes import HistoricalMarketPlanes
from app.services.backtest.market_view import MarketView
from app.services.backtest.metrics import calculate_metrics
from app.services.backtest.run_input_manifest import (
    ENGINE_VERSION,
    PinnedSecurityEvidenceV1,
    PROTOCOL_SCHEMA_VERSION,
    RunInputManifestV2,
    _detector_source_digests,
    _ledger_action_metrics_digest,
    _market_view_source_manifest,
    _python_runtime,
    _runtime_lock_digest,
    _timezone_dataset_version,
    current_execution_contract_digest,
    read_run_input_manifest,
)
from app.services.backtest.skill_discovery import discover_strategies
from app.services.backtest.run_universe import run_universe_digest
from app.services.backtest.strategy_protocol import InitialEntrySelectionV1
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.worker import BacktestExecutionEngine
from scripts.research.gh66_variants import (
    BUY_AND_HOLD,
    RANDOM_ORDER_VERSION,
    RankingVariant,
    research_variant_adapter,
    transform_signal_batch,
)

RUN_IDS = {
    BUY_AND_HOLD: "59926a2b-4ee6-4b1e-9ac0-519399f2ae3f",
    "rtly-backtest-darvas-box": "696c765c-b55a-469a-bf75-bb4f70e77941",
    "rtly-backtest-minervini": "04285cf1-934d-4941-83db-e526f16c218e",
    "rtly-backtest-moving-average": "71fc5af6-3624-4220-bb57-879f66b07e5a",
    "rtly-backtest-turtle-trend": "1a57a5d8-39c6-43a3-8657-e639f9d9d4f3",
    "rtly-backtest-weinstein": "da274a0c-affd-4b18-8acc-1f6551f4ca5d",
}
STRATEGIES = tuple(RUN_IDS)
SEEDS = (11, 23, 37, 53, 71)
RESULT_SCHEMA = "gh66_research_result.v1"
EXPERIMENT_SCHEMA = "gh66_experiment_manifest.v1"
LEGACY_SOURCE_REVISION = "cd36e4fbf729c0ad13238ea2ad8fc35771f87692"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(json.dumps(value, indent=2, sort_keys=True, default=str).encode())
    os.replace(temp, path)


def _read_only_connection(database: Path):
    uri = f"{database.resolve().as_uri()}?mode=ro"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.execute("PRAGMA query_only=ON")
        return connection

    return connect


def _database_signature(database: Path) -> dict[str, Any]:
    path = database.resolve()
    stat = path.stat()
    sidecars = {}
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar_stat = sidecar.stat()
            sidecars[suffix[1:]] = {
                "size_bytes": sidecar_stat.st_size,
                "mtime_ns": sidecar_stat.st_mtime_ns,
                "inode": sidecar_stat.st_ino,
            }
        else:
            sidecars[suffix[1:]] = None
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "sidecars": sidecars,
    }


def _database_main_identity(signature: dict[str, Any]) -> dict[str, Any]:
    return {
        key: signature[key]
        for key in ("path", "size_bytes", "mtime_ns", "device", "inode")
    }


def _original_rows(database: Path) -> dict[str, dict[str, Any]]:
    connection = _read_only_connection(database)()
    try:
        runs: dict[str, dict[str, Any]] = {}
        for strategy_id, run_id in RUN_IDS.items():
            row = connection.execute(
                "SELECT id, strategy_id, strategy_api_version, strategy_source_digest, "
                "parameters_json, profile_hash, start_month, end_month, base_currency, "
                "starting_capital, run_input_manifest_digest, execution_contract_digest, "
                "manifest_version FROM strategy_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"saved run is missing: {strategy_id}")
            manifest_row = connection.execute(
                "SELECT canonical_manifest_json FROM run_input_manifests WHERE digest=?",
                (row[10],),
            ).fetchone()
            if manifest_row is None:
                raise RuntimeError(f"saved manifest is missing: {strategy_id}")
            if (
                connection.execute(
                    "SELECT 1 FROM backtest_results WHERE run_id=?", (run_id,)
                ).fetchone()
                is None
            ):
                raise RuntimeError(f"saved result row is missing: {strategy_id}")
            run_manifest = json.loads(manifest_row[0])
            parameters = json.loads(row[4])
            securities = run_manifest["securities"]
            universe = run_manifest["universe_selection"]["canonical_security_ids"]
            runs[strategy_id] = {
                "run_id": row[0],
                "strategy_id": row[1],
                "strategy_api_version": row[2],
                "original_strategy_source_digest": row[3],
                "parameters": parameters,
                "profile_hash": row[5],
                "start_month": row[6],
                "end_month": row[7],
                "base_currency": row[8],
                "starting_capital": str(row[9]),
                "run_input_manifest_digest": row[10],
                "execution_contract_digest": row[11],
                "manifest_version": row[12],
                "manifest": run_manifest,
                "selected_security_ids": universe,
                "pinned_securities": securities,
            }
        return runs
    finally:
        connection.close()


_RESULT_TABLES = (
    "backtest_results",
    "backtest_result_audit_manifests",
    "backtest_result_candidate_audits",
    "backtest_result_entry_selection",
    "backtest_result_entry_selection_decisions",
    "trade_log",
    "equity_curve",
)


def _sqlite_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    if isinstance(value, float):
        return {"float_hex": value.hex()}
    return value


def _saved_result_snapshot(database: Path) -> dict[str, Any]:
    """Hash every original result row across one read-only SQLite snapshot."""
    connection = _read_only_connection(database)()
    per_run: dict[str, dict[str, Any]] = {}
    try:
        connection.execute("BEGIN")
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for strategy_id, run_id in RUN_IDS.items():
            per_run[strategy_id] = {}
            for table in _RESULT_TABLES:
                if table not in existing:
                    per_run[strategy_id][table] = {
                        "status": "table_missing",
                        "rows": 0,
                        "sha256": None,
                    }
                    continue
                columns = list(connection.execute(f'PRAGMA table_info("{table}")'))
                names = [str(column[1]) for column in columns]
                if "run_id" not in names:
                    per_run[strategy_id][table] = {
                        "status": "run_id_column_missing",
                        "rows": 0,
                        "sha256": None,
                    }
                    continue
                primary_key = sorted(
                    (int(column[5]), str(column[1]))
                    for column in columns
                    if int(column[5]) > 0
                )
                order_by = ", ".join(f'"{name}"' for _, name in primary_key) or "rowid"
                digest = hashlib.sha256()
                digest.update(_canonical_bytes(names))
                count = 0
                query = f'SELECT * FROM "{table}" WHERE run_id=? ORDER BY {order_by}'
                for row in connection.execute(query, (run_id,)):
                    payload = _canonical_bytes([_sqlite_value(value) for value in row])
                    digest.update(len(payload).to_bytes(8, "big"))
                    digest.update(payload)
                    count += 1
                per_run[strategy_id][table] = {
                    "status": "present",
                    "rows": count,
                    "sha256": digest.hexdigest(),
                }
        return {
            "snapshot_started_at": datetime.now(timezone.utc).isoformat(),
            "source_database": str(database.resolve()),
            "results": per_run,
            "snapshot_sha256": _sha256(per_run),
        }
    finally:
        connection.close()


def _assert_frozen_original_inputs(database: Path, registered: dict[str, Any]) -> None:
    current = _original_rows(database)
    expected_runs = registered["original_saved_runs"]
    expected_pins = registered["fixed_inputs"]["pinned_securities"]
    expected_roster_digest = registered["fixed_inputs"]["selected_security_ids_sha256"]
    for strategy_id, expected in expected_runs.items():
        row = current[strategy_id]
        checks = {
            "run_id": row["run_id"] == expected["original_run_id"],
            "manifest_digest": row["run_input_manifest_digest"]
            == expected["original_manifest_digest"],
            "strategy_source": row["original_strategy_source_digest"]
            == expected["original_strategy_source_digest"],
            "parameters": _sha256(row["parameters"]) == _sha256(expected["parameters"]),
            "roster": _sha256(row["selected_security_ids"]) == expected_roster_digest,
            "pins": _sha256(row["pinned_securities"]) == _sha256(expected_pins),
        }
        failed = [name for name, matches in checks.items() if not matches]
        if failed:
            raise RuntimeError(
                f"saved inputs changed since freeze for {strategy_id}: {', '.join(failed)}"
            )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _research_source_digests() -> dict[str, str]:
    paths = (
        Path(__file__),
        Path(__file__).with_name("gh66_variants.py"),
    )
    return {
        str(path.relative_to(config.ROOT_DIR)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


def _host_execution_identity() -> dict[str, Any]:
    return {
        "digest": current_execution_contract_digest(config.ROOT_DIR),
        "engine_version": ENGINE_VERSION,
        "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
        "python_runtime": _python_runtime(),
        "market_view_source_digest": _market_view_source_manifest(
            config.ROOT_DIR
        ).digest,
        "ledger_action_metrics_digest": _ledger_action_metrics_digest(config.ROOT_DIR),
        "runtime_lock_digest": _runtime_lock_digest(config.ROOT_DIR),
        "timezone_dataset_version": _timezone_dataset_version(),
        "calendar_session_table_digest": TradingCalendar().session_table_digest(),
        "detector_source_digests": [
            item.model_dump(mode="json")
            for item in _detector_source_digests(config.ROOT_DIR)
        ],
    }


def _current_skill_source_digests() -> dict[str, str]:
    discovery = discover_strategies(config.SKILLS_DIR)
    descriptors = {item.strategy_id: item for item in discovery.strategies}
    missing = set(RUN_IDS) - set(descriptors)
    if missing:
        raise RuntimeError(
            "one or more original Skills are no longer discoverable: "
            + ", ".join(sorted(missing))
        )
    return {
        strategy_id: descriptors[strategy_id].source_digest
        for strategy_id in STRATEGIES
    }


def _materialize_legacy_skill_source(destination: Path) -> dict[str, str]:
    """Extract the six original Skills from the revision that matches saved runs."""
    archive = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            LEGACY_SOURCE_REVISION,
            *(f"skills/{strategy_id}" for strategy_id in STRATEGIES),
        ],
        cwd=config.ROOT_DIR,
        check=True,
        capture_output=True,
    ).stdout
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        members = source.getmembers()
        for member in members:
            parts = member.name.split("/")
            if (
                member.name.startswith("/")
                or ".." in parts
                or not (member.isdir() or member.isfile())
            ):
                raise RuntimeError(
                    "pinned legacy Skill archive contains an unsafe path"
                )
        for member in members:
            target = destination.joinpath(*member.name.split("/"))
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = source.extractfile(member)
            if content is None:
                raise RuntimeError(
                    "pinned legacy Skill archive contains an unreadable file"
                )
            target.write_bytes(content.read())
            target.chmod(member.mode & 0o777)

    discovery = discover_strategies(destination / "skills")
    descriptors = {item.strategy_id: item for item in discovery.strategies}
    missing = set(STRATEGIES) - set(descriptors)
    if missing:
        raise RuntimeError(
            "pinned legacy revision is missing Skills: " + ", ".join(sorted(missing))
        )
    return {
        strategy_id: descriptors[strategy_id].source_digest
        for strategy_id in STRATEGIES
    }


def _active_skill_source_digest(
    registered: dict[str, Any], strategy_id: str, variant: RankingVariant
) -> str:
    strategy_key = BUY_AND_HOLD if strategy_id == "SPY" else strategy_id
    source_key = (
        "legacy" if variant == "legacy" and strategy_id != "SPY" else "proposed"
    )
    return registered["variants"][source_key]["skill_source_digests"][strategy_key]


def _assert_frozen_runtime(registered: dict[str, Any]) -> None:
    """Reject replays if source or runtime identities drifted after freeze."""
    expected_research = registered["research_code_sha256"]
    actual_research = _research_source_digests()
    if actual_research != expected_research:
        raise RuntimeError("GH #66 research source changed after experiment freeze")

    expected_host = registered["host_execution_contract"]
    actual_host = _host_execution_identity()
    if actual_host != expected_host:
        changed = sorted(
            key
            for key in set(expected_host) | set(actual_host)
            if expected_host.get(key) != actual_host.get(key)
        )
        raise RuntimeError(
            "GH #66 host runtime identity changed after experiment freeze: "
            + ", ".join(changed)
        )

    expected_skills = registered["variants"]["proposed"]["skill_source_digests"]
    actual_skills = _current_skill_source_digests()
    if actual_skills != expected_skills:
        changed = sorted(
            strategy_id
            for strategy_id in set(expected_skills) | set(actual_skills)
            if expected_skills.get(strategy_id) != actual_skills.get(strategy_id)
        )
        raise RuntimeError(
            "GH #66 Skill source identity changed after experiment freeze: "
            + ", ".join(changed)
        )
    legacy = registered["variants"]["legacy"]
    saved_sources = {
        strategy_id: item["original_strategy_source_digest"]
        for strategy_id, item in registered["original_saved_runs"].items()
    }
    if (
        legacy.get("source_revision") != LEGACY_SOURCE_REVISION
        or legacy.get("skill_source_digests") != saved_sources
    ):
        raise RuntimeError("GH #66 pinned legacy Skill identity changed after freeze")


def _read_frozen_manifest(path: Path) -> dict[str, Any]:
    registered = json.loads(path.read_text())
    if registered.get("schema") != EXPERIMENT_SCHEMA:
        raise ValueError("unsupported frozen experiment manifest")
    expected_id = _sha256(
        {key: value for key, value in registered.items() if key != "experiment_id"}
    )
    if registered.get("experiment_id") != expected_id:
        raise ValueError("frozen experiment manifest digest does not match")
    return registered


def _expected_run_identity(
    registered: dict[str, Any],
    strategy_id: str,
    variant: RankingVariant,
    seed: int | None,
    start_month: str | None,
    end_month: str | None,
) -> dict[str, Any]:
    is_spy = strategy_id == "SPY"
    strategy_key = BUY_AND_HOLD if is_spy else strategy_id
    fixed = registered["fixed_inputs"]
    return {
        "experiment_id": registered["experiment_id"],
        "strategy_id": strategy_id,
        "variant": "benchmark" if is_spy else variant,
        "seed": seed,
        "start_month": start_month or fixed["start_month"],
        "end_month": end_month or fixed["end_month"],
        "original_run_id": registered["original_saved_runs"][strategy_key][
            "original_run_id"
        ],
        "original_manifest_digest": registered["original_saved_runs"][strategy_key][
            "original_manifest_digest"
        ],
    }


def _validate_requested_arm(
    registered: dict[str, Any],
    strategy_id: str,
    variant: RankingVariant,
    seed: int | None,
) -> None:
    if strategy_id == "SPY":
        if variant != "legacy" or seed is not None:
            raise ValueError("SPY accepts only its single deterministic benchmark run")
        return
    if strategy_id not in registered["original_saved_runs"]:
        raise ValueError("strategy is outside the frozen experiment")
    if variant == "random":
        if (
            isinstance(seed, bool)
            or seed not in registered["variants"]["random"]["seeds"]
        ):
            raise ValueError("random seed is outside the frozen experiment")
    elif seed is not None:
        raise ValueError("only preregistered random arms accept a seed")


def _requested_horizon(
    registered: dict[str, Any], start_month: str | None, end_month: str | None
) -> tuple[str, str]:
    fixed = registered["fixed_inputs"]
    return start_month or fixed["start_month"], end_month or fixed["end_month"]


def _validate_run_horizon(
    registered: dict[str, Any],
    *,
    strategy_id: str,
    variant: RankingVariant,
    seed: int | None,
    start_month: str | None,
    end_month: str | None,
) -> None:
    requested = _requested_horizon(registered, start_month, end_month)
    fixed = registered["fixed_inputs"]
    if requested == (fixed["start_month"], fixed["end_month"]):
        return
    short = registered["short_replay"]
    variant_label = f"random seed {seed}" if variant == "random" else variant
    if (
        strategy_id == short["strategy_id"]
        and requested == (short["start_month"], short["end_month"])
        and variant_label in short["variants"]
    ):
        return
    raise ValueError(
        "requested horizon is outside the frozen experiment and preregistered short replay"
    )


def _validate_group_horizon(
    registered: dict[str, Any],
    *,
    strategy_id: str,
    arms: list[str],
    repeat_count: int,
    start_month: str | None,
    end_month: str | None,
    share_strategy_signals: bool,
    interleave_repeats: bool,
) -> None:
    requested = _requested_horizon(registered, start_month, end_month)
    fixed = registered["fixed_inputs"]
    if requested == (fixed["start_month"], fixed["end_month"]):
        return
    short = registered["short_replay"]
    short_arms = [
        variant.replace("random seed ", "random:")
        if variant.startswith("random seed ")
        else variant
        for variant in short["variants"]
    ]
    if (
        strategy_id == short["strategy_id"]
        and requested == (short["start_month"], short["end_month"])
        and arms == short_arms
        and repeat_count == short["repeats_per_variant"]
        and share_strategy_signals is short["share_strategy_signals"]
        and interleave_repeats is ("interleaved" in short["schedule"])
    ):
        return
    raise ValueError(
        "run-group horizon or schedule is outside the frozen experiment and preregistered short replay"
    )


def _validate_existing_result(
    result: dict[str, Any],
    registered: dict[str, Any],
    *,
    strategy_id: str,
    variant: RankingVariant,
    seed: int | None,
    start_month: str | None = None,
    end_month: str | None = None,
) -> None:
    """Ensure a resumable output belongs to this frozen experiment/run key."""
    expected = _expected_run_identity(
        registered, strategy_id, variant, seed, start_month, end_month
    )
    mismatches = [key for key, value in expected.items() if result.get(key) != value]
    if mismatches:
        raise RuntimeError(
            "existing GH #66 result does not match frozen run identity: "
            + ", ".join(mismatches)
        )
    if result.get("status") != "completed":
        return

    run_key = {
        key: result.get(key)
        for key in (
            "experiment_id",
            "strategy_id",
            "variant",
            "seed",
            "start_month",
            "end_month",
            "manifest_digest",
        )
    }
    if not run_key["manifest_digest"] or result.get("run_id") != _sha256(run_key):
        raise RuntimeError("existing GH #66 result has an invalid run key digest")
    if result.get("research_code_sha256") != registered["research_code_sha256"]:
        raise RuntimeError(
            "existing GH #66 result has a different research source identity"
        )
    if (
        result.get("execution_contract_digest")
        != registered["host_execution_contract"]["digest"]
    ):
        raise RuntimeError(
            "existing GH #66 result has a different host execution contract"
        )
    strategy_key = BUY_AND_HOLD if strategy_id == "SPY" else strategy_id
    expected_skill = registered["variants"]["proposed"]["skill_source_digests"][
        strategy_key
    ]
    if result.get("proposed_skill_source_digest") != expected_skill:
        raise RuntimeError(
            "existing GH #66 result has a different Skill source identity"
        )
    if result.get("active_skill_source_digest") != _active_skill_source_digest(
        registered, strategy_id, variant
    ):
        raise RuntimeError(
            "existing GH #66 result has a different active Skill source identity"
        )


def _fixed_candidate_fixture() -> dict[str, Any]:
    """Show which candidates the engine admits from one identical state."""
    strategy_id = "rtly-backtest-moving-average"
    session = date(2020, 1, 2)
    raw = [
        Signal(
            security_id=security_id,
            side=SignalSide.BUY,
            session=session,
            rule_id="gh66-fixed-cohort",
            priority=Decimal(priority),
        )
        for security_id, priority in (("AAA", "3"), ("BBB", "5"), ("CCC", "4"))
    ]
    variants: list[tuple[RankingVariant, int | None]] = [
        ("legacy", None),
        ("proposed", None),
        *(("random", seed) for seed in SEEDS),
    ]
    output: dict[str, Any] = {
        "session": session.isoformat(),
        "portfolio_state": {
            "open_positions": [],
            "pending_buy_orders": [],
            "max_concurrent_positions": 1,
            "available_slots": 1,
        },
        "same_eligible_candidates": [
            {"security_id": item.security_id, "proposed_priority": str(item.priority)}
            for item in raw
        ],
        "selection_rule": "engine host order, then highest priority first; missing priority sorts last",
        "variants": {},
    }
    for variant, seed in variants:
        signals = transform_signal_batch(strategy_id, raw, variant=variant, seed=seed)
        host_order = sorted(signals, key=_engine_signal_sort_key)
        allocator_order = sorted(host_order, key=_slot_rank)
        output["variants"][f"{variant}:{seed}" if seed is not None else variant] = {
            "ordering_source": (
                "current Skill priority"
                if variant == "proposed"
                else "stable SHA-256 seed/session/security digest"
                if variant == "random"
                else "legacy unranked host fallback"
            ),
            "ordered_candidates": [
                {
                    "security_id": item.security_id,
                    "priority": None if item.priority is None else str(item.priority),
                    "admitted": index == 0,
                }
                for index, item in enumerate(allocator_order)
            ],
            "admitted_security_ids": [allocator_order[0].security_id],
        }
    return output


def freeze_manifest(
    database: Path, historical_database: Path, output: Path
) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"refusing to replace frozen manifest {output}")
    originals = _original_rows(database)
    expected_roster = originals[BUY_AND_HOLD]["selected_security_ids"]
    expected_pins = originals[BUY_AND_HOLD]["pinned_securities"]
    if len(expected_roster) != 755:
        raise ValueError(
            f"expected 755 selected securities, found {len(expected_roster)}"
        )

    strategy_inputs: dict[str, dict[str, Any]] = {}
    for strategy_id, row in originals.items():
        params = row["parameters"]
        manifest = row["manifest"]
        if (
            row["start_month"],
            row["end_month"],
            row["base_currency"],
            row["starting_capital"],
            row["profile_hash"],
        ) != (
            "2016-09",
            "2026-08",
            "GBP",
            "10000",
            originals[BUY_AND_HOLD]["profile_hash"],
        ):
            raise ValueError(f"saved run has a different fixed contract: {strategy_id}")
        if params.get("max_concurrent_positions") != 10:
            raise ValueError(f"saved run cap differs from 10: {strategy_id}")
        if params.get("block_buy_on_downtrend_enabled") is not True:
            raise ValueError(f"saved downtrend block is not enabled: {strategy_id}")
        if row["selected_security_ids"] != expected_roster:
            raise ValueError(f"saved run roster differs: {strategy_id}")
        if _sha256(row["pinned_securities"]) != _sha256(expected_pins):
            raise ValueError(f"saved run evidence pins differ: {strategy_id}")
        if manifest.get("regime_benchmark") != originals[BUY_AND_HOLD]["manifest"].get(
            "regime_benchmark"
        ):
            raise ValueError(f"saved SPY regime reference differs: {strategy_id}")
        strategy_inputs[strategy_id] = {
            "original_run_id": row["run_id"],
            "original_manifest_digest": row["run_input_manifest_digest"],
            "original_strategy_source_digest": row["original_strategy_source_digest"],
            "parameters": params,
            "manifest_version": row["manifest_version"],
        }

    current_sources = _current_skill_source_digests()
    saved_sources = {
        strategy_id: row["original_strategy_source_digest"]
        for strategy_id, row in originals.items()
    }
    with tempfile.TemporaryDirectory(prefix="gh66-legacy-freeze-") as directory:
        legacy_sources = _materialize_legacy_skill_source(Path(directory))
    if legacy_sources != saved_sources:
        raise ValueError(
            "pinned legacy Skills do not match the source digests on the saved runs"
        )
    code_digests = _research_source_digests()
    regime = originals[BUY_AND_HOLD]["manifest"]["regime_benchmark"]
    fx_revisions = sorted(
        {
            item["fx_revision"]
            for item in expected_pins
            if item.get("fx_revision") is not None
        }
    )
    if len(fx_revisions) != 1:
        raise ValueError(
            "saved securities must share exactly one pinned FX revision for the SPY benchmark"
        )
    pin_payload = [
        {
            "security_id": item["security_id"],
            "price_revision": item["price_revision"],
            "action_revision": item["action_revision"],
            "fx_revision": item.get("fx_revision"),
        }
        for item in expected_pins
    ]
    base = {
        "schema": EXPERIMENT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fixed_inputs": {
            "start_month": "2016-09",
            "end_month": "2026-08",
            "starting_capital": "10000",
            "base_currency": "GBP",
            "max_concurrent_positions": 10,
            "block_buy_on_downtrend_enabled": True,
            "selected_security_count": 755,
            "selected_security_ids": expected_roster,
            "selected_security_ids_sha256": _sha256(expected_roster),
            "pinned_security_count": len(pin_payload),
            "pinned_securities": pin_payload,
            "pinned_securities_sha256": _sha256(pin_payload),
            "profile_hash": originals[BUY_AND_HOLD]["profile_hash"],
        },
        "original_saved_runs": strategy_inputs,
        "source_database_readonly_signatures": {
            "backtest": _database_signature(database),
            "historical_prices": _database_signature(historical_database),
        },
        "variants": {
            "legacy": {
                "policy": {
                    "rtly-backtest-minervini": "raw VCP score priority",
                    "rtly-backtest-weinstein": "unranked host fallback",
                    "rtly-backtest-darvas-box": "unranked host fallback",
                    "rtly-backtest-turtle-trend": "unranked host fallback",
                    "rtly-backtest-moving-average": "unranked host fallback",
                    BUY_AND_HOLD: "existing initial top-X membership and ordering",
                },
                "source_revision": LEGACY_SOURCE_REVISION,
                "skill_source_digests": legacy_sources,
                "source": "archived original Skill sources matching all six saved runs",
            },
            "proposed": {
                "skill_source_digests": current_sources,
                "source": "current hashed Skill runtime, unchanged output",
            },
            "random": {
                "source": "research output-boundary adapter",
                "ordering_version": RANDOM_ORDER_VERSION,
                "seeds": list(SEEDS),
                "buy_and_hold": "reorders only the existing selected top-X basket",
            },
        },
        "host_execution_contract": _host_execution_identity(),
        "spy_benchmark": {
            "security_id": regime["security_id"],
            "price_revision": regime["price_revision"],
            "action_revision": regime["action_revision"],
            "fx_revision": fx_revisions[0],
            "currency": "USD",
            "base_currency": "GBP",
            "capital": "10000",
            "method": "single-SPY buy-and-hold through the same engine and dates",
            "dividends": "engine credits pinned cash dividends; no reinvestment",
            "valuation": "split-adjusted holdings marked at as-traded close, exact pinned GBP/USD FX",
            "costs": "same gross engine convention as strategy runs; no commission, spread, or slippage",
            "regime_filter": "disabled; SPY is held continuously as the passive benchmark",
            "regime_pin": regime,
        },
        "short_replay": {
            "start_month": "2016-09",
            "end_month": "2016-11",
            "strategy_id": "rtly-backtest-moving-average",
            "variants": ["legacy", "proposed", "random seed 11"],
            "repeats_per_variant": 3,
            "share_strategy_signals": False,
            "schedule": "interleaved by repeat across variants",
            "timed_comparison": "each arm executes its own Skill entry logic on the same warmed market-data plane",
            "proposed_repeat_equality": "all three proposed replay result/equity digests must match",
        },
        "full_repeat_control": {
            "strategy_id": "rtly-backtest-moving-average",
            "variant": "proposed",
            "repetitions": 2,
            "equality": "simulation result and equity curve digests must match",
        },
        "arm_matrix": {
            "strategy_ids": list(STRATEGIES),
            "per_strategy": [
                "legacy",
                "proposed",
                *[f"random seed {s}" for s in SEEDS],
            ],
            "planned_full_runs": len(STRATEGIES) * (2 + len(SEEDS)),
            "repeat_control": "two identical proposed short replays",
        },
        "research_code_sha256": code_digests,
        "fixed_candidate_admission_fixture": _fixed_candidate_fixture(),
        "limitations": [
            "The 2016-09 through 2026-08 decade was already inspected and is diagnostic, not untouched validation.",
            "The selected 755-security universe is survivor-universe data; this experiment does not remove survivorship bias.",
            "All variants are gross of commission, spread, and slippage.",
        ],
    }
    body = {**base, "experiment_id": _sha256(base)}
    _atomic_json(output, body)
    return body


class _ManifestOverlay:
    """Supply an in-memory current manifest while every repository read stays read-only."""

    def __init__(self, base: BacktestRepository, digest: str, raw: str) -> None:
        self._base = base
        self._digest = digest
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def run_input_manifest_json(self, digest: str) -> str | None:
        if digest == self._digest:
            return self._raw
        return self._base.run_input_manifest_json(digest)


def _current_manifest_and_resolver(
    backtest_db: Path,
    historical_db: Path,
    strategy_id: str,
    start_month: str,
    end_month: str,
    *,
    spy_benchmark: bool = False,
    skills_root: Path | None = None,
):
    backtest_repo = BacktestRepository(_read_only_connection(backtest_db))
    prices = HistoricalPriceRepository(_read_only_connection(historical_db))
    original = _original_rows(backtest_db)[strategy_id]
    original_manifest_json = backtest_repo.run_input_manifest_json(
        original["run_input_manifest_digest"]
    )
    if original_manifest_json is None:
        raise RuntimeError("saved run input manifest is missing")
    original_manifest = read_run_input_manifest(original_manifest_json)
    source_root = skills_root or config.SKILLS_DIR
    descriptor = next(
        item
        for item in discover_strategies(source_root).strategies
        if item.strategy_id == strategy_id
    )
    updates: dict[str, object] = {
        "engine_version": ENGINE_VERSION,
        "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
        "market_view_source_digest": _market_view_source_manifest(
            config.ROOT_DIR
        ).digest,
        "ledger_action_metrics_digest": _ledger_action_metrics_digest(config.ROOT_DIR),
        "runtime_lock_digest": _runtime_lock_digest(config.ROOT_DIR),
        "calendar_session_table_digest": TradingCalendar().session_table_digest(),
        "python_runtime": _python_runtime(),
        "timezone_dataset_version": _timezone_dataset_version(),
        "detector_source_digests": _detector_source_digests(config.ROOT_DIR),
        "strategy_api_version": descriptor.api_version,
        "strategy_source_digest": descriptor.source_digest,
        "start_month": start_month,
        "end_month": end_month,
    }
    if (start_month, end_month) != (
        original_manifest.start_month,
        original_manifest.end_month,
    ):
        readiness = backtest_repo.interval_readiness(
            original_manifest.profile_hash, start_month, end_month
        )
        if not readiness.ready or readiness.ordered_month_digest is None:
            raise RuntimeError(
                "short replay interval is not covered by the saved profile"
            )
        updates["ordered_month_digest"] = readiness.ordered_month_digest
    if spy_benchmark:
        if strategy_id != BUY_AND_HOLD or not hasattr(
            original_manifest, "regime_benchmark"
        ):
            raise ValueError("SPY benchmark requires the saved Buy & Hold V3 manifest")
        regime = original_manifest.regime_benchmark
        fx_revisions = {
            item.fx_revision
            for item in original_manifest.securities
            if item.fx_revision is not None
        }
        if len(fx_revisions) != 1:
            raise ValueError(
                "SPY benchmark requires one unambiguous pinned FX revision"
            )
        (fx_revision,) = fx_revisions
        original_selection = original_manifest.universe_selection
        assert original_selection is not None
        security_ids = (regime.security_id,)
        universe_digest = run_universe_digest(
            security_ids,
            universe_schema=original_selection.universe_schema,
            mode=original_selection.universe_mode,
            parameter=original_selection.universe_parameter,
            profile_hash=original_selection.profile_hash,
        )
        selection = original_selection.model_copy(
            update={
                "canonical_security_ids": security_ids,
                "run_universe_digest": universe_digest,
            }
        )
        parameters = dict(original_manifest.parameters)
        parameters[original_selection.universe_parameter] = list(security_ids)
        parameters["top_x"] = 1
        parameters["max_concurrent_positions"] = 1
        parameters["regime_filter_enabled"] = False
        parameters["block_buy_on_downtrend_enabled"] = False
        raw_manifest = original_manifest.model_dump(mode="python")
        raw_manifest.pop("regime_benchmark", None)
        raw_manifest.update(updates)
        raw_manifest.update(
            {
                "schema_version": "run_input_manifest.v2",
                "parameters": parameters,
                "securities": [
                    PinnedSecurityEvidenceV1(
                        security_id=regime.security_id,
                        price_revision=regime.price_revision,
                        action_revision=regime.action_revision,
                        fx_revision=fx_revision,
                    ).model_dump(mode="python")
                ],
                "universe_selection": selection.model_dump(mode="python"),
            }
        )
        current_manifest = RunInputManifestV2.model_validate(raw_manifest)
    else:
        current_manifest = read_run_input_manifest(
            original_manifest.model_copy(update=updates).canonical_json()
        )
    digest = current_manifest.digest()
    raw = current_manifest.canonical_json()
    overlay = _ManifestOverlay(backtest_repo, digest, raw)
    original_run = backtest_repo.strategy_run(original["run_id"])
    run = original_run.model_copy(
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
            "universe_selection": getattr(current_manifest, "universe_selection", None),
            "regime_benchmark": getattr(current_manifest, "regime_benchmark", None),
        }
    )
    resolver = BacktestExecutionEngine(
        repository=cast(BacktestRepository, overlay),
        backtest=run,
        prices=prices,
        project_root=config.ROOT_DIR,
    )
    original_skills_root = config.SKILLS_DIR
    try:
        if skills_root is not None:
            config.SKILLS_DIR = skills_root
        manifest, strategy, market_data, fx_evidence = resolver._resolve()
    finally:
        config.SKILLS_DIR = original_skills_root
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


def _to_json(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _to_json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_to_json(item) for item in value]
    return value


def _audit_storage(audits: Iterable[CandidateAuditV1]) -> dict[str, int]:
    encoded: list[tuple[int, str, str, bytes]] = []
    for audit in audits:
        payload = _canonical_bytes(audit.model_dump(mode="json"))
        encoded.append(
            (
                int(audit.candidate_sequence),
                str(audit.signal_session),
                str(audit.security_id),
                payload,
            )
        )
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE audit_rows(run_id TEXT NOT NULL, candidate_sequence INTEGER NOT NULL, "
            "session TEXT NOT NULL, security_id TEXT NOT NULL, digest TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY(run_id, candidate_sequence))"
        )
        connection.execute(
            "CREATE INDEX audit_rows_security ON audit_rows(security_id, session)"
        )
        connection.executemany(
            "INSERT INTO audit_rows VALUES(?,?,?,?,?,?)",
            [
                (
                    "gh66",
                    sequence,
                    session,
                    security_id,
                    hashlib.sha256(payload).hexdigest(),
                    payload,
                )
                for sequence, session, security_id, payload in encoded
            ],
        )
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    finally:
        connection.close()
    return {
        "rows": len(encoded),
        "payload_bytes": sum(len(row[3]) for row in encoded),
        "isolated_sqlite_page_bytes": page_count * page_size,
    }


def _metrics(
    starting_capital: Decimal,
    curve: tuple,
    events: tuple,
    audits: list[CandidateAuditV1],
) -> dict[str, Any]:
    exits = tuple(event for event in events if isinstance(event, ExitFillEventV1))
    metrics = calculate_metrics(
        starting_capital=starting_capital,
        equity_curve=curve,
        closed_trades=exits,
    )
    first, last = curve[0], curve[-1]
    elapsed_days = max((last.session - first.session).days, 1)
    ending = float(last.total_equity_base)
    cagr = (
        (ending / float(starting_capital)) ** (365.2425 / elapsed_days) - 1
        if ending > 0
        else None
    )
    exposures = [
        float(point.positions_value_base / point.total_equity_base)
        for point in curve
        if point.total_equity_base > 0
    ]
    buys = sum(
        (event.cost_base for event in events if isinstance(event, EntryFillEventV1)),
        Decimal(0),
    )
    sells = sum(
        (event.proceeds_base for event in events if isinstance(event, ExitFillEventV1)),
        Decimal(0),
    )
    priority_coverage = (
        sum(audit.priority is not None for audit in audits) / len(audits)
        if audits
        else None
    )
    explanation_coverage = (
        sum(audit.explanation is not None for audit in audits) / len(audits)
        if audits
        else None
    )
    momentum_values = [
        fact.observed
        for audit in audits
        if audit.explanation is not None
        for reason in audit.explanation.reasons
        if reason.code == "entry_ranking"
        for fact in reason.facts
        if fact.label == "Momentum"
    ]
    ranked_momentum = [value for value in momentum_values if value is not None]
    dispositions = Counter(str(audit.disposition) for audit in audits)
    return {
        **metrics.model_dump(mode="json"),
        "cagr": cagr,
        "start_value": str(first.total_equity_base),
        "end_value": str(last.total_equity_base),
        "total_return": (
            None if metrics.total_return is None else float(metrics.total_return)
        ),
        "mean_invested_exposure_pct": 100 * statistics.fmean(exposures)
        if exposures
        else None,
        "time_invested_pct": 100
        * sum(value > 0 for value in exposures)
        / len(exposures)
        if exposures
        else None,
        "turnover_on_initial_capital": float((buys + sells) / starting_capital),
        "exit_count": len(exits),
        "candidate_audit_rows": len(audits),
        "priority_coverage_pct": None
        if priority_coverage is None
        else 100 * priority_coverage,
        "explanation_coverage_pct": None
        if explanation_coverage is None
        else 100 * explanation_coverage,
        "momentum_evidence_coverage_pct": (
            None
            if not momentum_values
            else 100 * len(ranked_momentum) / len(momentum_values)
        ),
        "contested_selections": dispositions.get(
            str(CandidateAuditDisposition.COMPETITION_REJECTED), 0
        ),
        "full_book_rejections": dispositions.get(
            str(CandidateAuditDisposition.FULL_BOOK_REJECTED), 0
        ),
        "candidate_dispositions": dict(dispositions),
    }


class _ProgressSink(InMemorySessionBatchSink):
    def __init__(self, label: str, interval: int) -> None:
        super().__init__()
        self._label = label
        self._interval = interval

    def publish_session(
        self,
        *,
        session: date,
        events: tuple[TradeLogEvent, ...],
        equity_point: EquityCurvePointV1,
        candidate_audits: tuple[CandidateAuditV1, ...] = (),
        initial_entry_selection: InitialEntrySelectionV1 | None = None,
    ) -> None:
        super().publish_session(
            session=session,
            events=events,
            equity_point=equity_point,
            candidate_audits=candidate_audits,
            initial_entry_selection=initial_entry_selection,
        )
        count = len(self.equity_curve)
        if count == 1 or count % self._interval == 0:
            print(
                f"PROGRESS {self._label}: session={session.isoformat()} count={count}",
                flush=True,
            )


def run_one(
    *,
    manifest_path: Path,
    backtest_db: Path,
    historical_db: Path,
    strategy_id: str,
    variant: RankingVariant,
    seed: int | None,
    output: Path,
    start_month: str | None = None,
    end_month: str | None = None,
    shared_prepared_planes: dict[str, HistoricalMarketPlanes] | None = None,
    shared_entry_signal_cache: dict[date, tuple[Signal, ...]] | None = None,
    shared_initial_selection_cache: dict[date, Any] | None = None,
    legacy_skill_root: Path | None = None,
) -> dict[str, Any]:
    if variant == "legacy" and strategy_id != "SPY" and legacy_skill_root is None:
        with tempfile.TemporaryDirectory(prefix="gh66-legacy-run-") as directory:
            archive_root = Path(directory)
            _materialize_legacy_skill_source(archive_root)
            return run_one(
                manifest_path=manifest_path,
                backtest_db=backtest_db,
                historical_db=historical_db,
                strategy_id=strategy_id,
                variant=variant,
                seed=seed,
                output=output,
                start_month=start_month,
                end_month=end_month,
                shared_prepared_planes=shared_prepared_planes,
                shared_entry_signal_cache=shared_entry_signal_cache,
                shared_initial_selection_cache=shared_initial_selection_cache,
                legacy_skill_root=archive_root / "skills",
            )
    setup_started = time.perf_counter()
    registered = _read_frozen_manifest(manifest_path)
    expected_signatures = registered["source_database_readonly_signatures"]
    for role, database in (
        ("backtest", backtest_db),
        ("historical_prices", historical_db),
    ):
        expected = expected_signatures[role]
        actual = _database_signature(database)
        if expected["path"] != actual["path"]:
            raise RuntimeError(
                f"{role} database path differs from the frozen experiment"
            )
    is_spy_benchmark = strategy_id == "SPY"
    strategy_key = BUY_AND_HOLD if is_spy_benchmark else strategy_id
    _validate_requested_arm(registered, strategy_id, variant, seed)
    _validate_run_horizon(
        registered,
        strategy_id=strategy_id,
        variant=variant,
        seed=seed,
        start_month=start_month,
        end_month=end_month,
    )
    _assert_frozen_runtime(registered)
    _assert_frozen_original_inputs(backtest_db, registered)
    original = registered["original_saved_runs"][strategy_key]
    saved_period = registered["fixed_inputs"]
    start_month = start_month or saved_period["start_month"]
    end_month = end_month or saved_period["end_month"]
    (
        repository,
        overlay,
        prices,
        run,
        resolver,
        run_manifest,
        strategy,
        market_data,
        fx_evidence,
    ) = _current_manifest_and_resolver(
        backtest_db,
        historical_db,
        strategy_key,
        start_month,
        end_month,
        spy_benchmark=is_spy_benchmark,
        skills_root=legacy_skill_root,
    )
    if (
        run_manifest.execution_contract_digest()
        != registered["host_execution_contract"]["digest"]
    ):
        raise RuntimeError(
            "resolved host execution contract differs from frozen identity"
        )
    active_skill_digest = _active_skill_source_digest(registered, strategy_id, variant)
    if run_manifest.strategy_source_digest != active_skill_digest:
        raise RuntimeError("resolved Skill source differs from frozen identity")
    adapter = (
        None
        if variant == "legacy" and not is_spy_benchmark
        else research_variant_adapter(
            strategy,
            strategy_key,
            variant,
            seed,
            entry_signal_cache=shared_entry_signal_cache,
            initial_selection_cache=shared_initial_selection_cache,
        )
    )
    prepared_planes = (
        shared_prepared_planes if shared_prepared_planes is not None else {}
    )
    scan_cache: dict[tuple[str, str], object] = {}
    scan_cache_month: dict[str, str] = {}
    prepared_fx = None
    if fx_evidence is not None:
        prepared_fx = prepare_fx_closes(fx_evidence)
    benchmark_access = resolver._regime_benchmark_access
    price_accesses = {
        item.security_id: cast(HistoricalEvidenceReadHandle, item.price_access)
        for item in market_data
        if item.price_access is not None
    }

    def factory(session: date) -> MarketView:
        return MarketView(
            as_of_session=session,
            profile_hash=run_manifest.profile_hash,
            security_price_revisions={
                item.security_id: item.price_revision
                for item in run_manifest.securities
            },
            selected_universe=tuple(
                item.security_id for item in run_manifest.securities
            ),
            backtest_repo=cast(BacktestRepository, overlay),
            historical_price_repo=prices,
            base_currency=run_manifest.base_currency,
            fx_evidence=fx_evidence,
            prepared_fx=prepared_fx,
            regime_benchmark=getattr(run_manifest, "regime_benchmark", None),
            regime_benchmark_access=benchmark_access,
            prepared_planes=MappingProxyType(prepared_planes),
            prepared_plane_cache=prepared_planes,
            price_accesses=price_accesses,
            scan_cache=scan_cache,
            scan_cache_month=scan_cache_month,
        )

    period_years = (
        date.fromisoformat(f"{end_month}-01") - date.fromisoformat(f"{start_month}-01")
    ).days / 365.2425
    progress_interval = max(10, round(period_years * 25))
    sink = _ProgressSink(f"{strategy_id}/{variant}/{seed}", progress_interval)
    started = time.perf_counter()
    setup_elapsed = started - setup_started
    rss_before = _rss_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    try:
        output_result = run_simulation(
            manifest=run_manifest,
            strategy=strategy if adapter is None else adapter,
            market_view_factory=factory,
            security_market_data=market_data,
            fx_evidence=fx_evidence,
            prepared_fx=prepared_fx,
            sink=sink,
            prepared_planes=prepared_planes,
        )
    finally:
        for item in market_data:
            if item.price_access is not None:
                item.price_access.close()
        resolver._close_regime_benchmark_access()
    elapsed = time.perf_counter() - started
    rss_after = _rss_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    metrics = _metrics(
        run_manifest.starting_capital,
        output_result.equity_curve,
        output_result.events,
        sink.candidate_audits,
    )
    storage = _audit_storage(sink.candidate_audits)
    events = [event.model_dump(mode="json") for event in output_result.events]
    audits = [audit.model_dump(mode="json") for audit in sink.candidate_audits]
    curve = [_to_json(point) for point in output_result.equity_curve]
    event_digest = _sha256(events)
    audit_digest = _sha256(audits)
    result_digest = _sha256({"events": event_digest, "equity_curve": curve})
    result_strategy_id = "SPY" if is_spy_benchmark else strategy_id
    result_variant = "benchmark" if is_spy_benchmark else variant
    run_key = {
        "experiment_id": registered["experiment_id"],
        "strategy_id": result_strategy_id,
        "variant": result_variant,
        "seed": seed,
        "start_month": start_month,
        "end_month": end_month,
        "manifest_digest": run_manifest.digest(),
    }
    result = {
        "schema": RESULT_SCHEMA,
        "run_id": _sha256(run_key),
        **run_key,
        "original_run_id": original["original_run_id"],
        "original_manifest_digest": original["original_manifest_digest"],
        "proposed_skill_source_digest": registered["variants"]["proposed"][
            "skill_source_digests"
        ][strategy_key],
        "active_skill_source_digest": active_skill_digest,
        "research_code_sha256": registered["research_code_sha256"],
        "execution_contract_digest": run_manifest.execution_contract_digest(),
        "simulation_result_sha256": result_digest,
        "event_count": len(events),
        "event_sha256": event_digest,
        "candidate_audit_sha256": audit_digest,
        "research_signal_cache": {
            "entry_batches_reused": 0
            if adapter is None
            else adapter.entry_signal_cache_hits,
            "entry_batches_computed": 0
            if adapter is None
            else adapter.entry_signal_cache_misses,
            "initial_selections_reused": 0
            if adapter is None
            else adapter.initial_selection_cache_hits,
            "initial_selections_computed": 0
            if adapter is None
            else adapter.initial_selection_cache_misses,
        },
        "status": "completed",
        "metrics": metrics,
        "audit_storage": storage,
        "performance": {
            "setup_seconds": setup_elapsed,
            "elapsed_seconds": elapsed,
            "total_seconds": setup_elapsed + elapsed,
            "peak_rss_bytes": rss_after,
            "peak_rss_delta_bytes": max(rss_after - rss_before, 0),
            "prepared_market_plane_count": len(prepared_planes),
        },
        "equity_curve": curve,
    }
    _atomic_json(output, result)
    return result


def _rss_bytes(value: int) -> int:
    """Normalize getrusage's platform-specific RSS units to bytes."""
    return int(value if sys.platform == "darwin" else value * 1024)


def _arm_name(strategy_id: str, variant: str, seed: int | None = None) -> str:
    suffix = f"random-{seed}" if variant == "random" else variant
    return f"{strategy_id}--{suffix}"


def _parse_group_arm(value: str) -> tuple[RankingVariant, int | None]:
    parts = value.split(":", maxsplit=1)
    if parts[0] in {"legacy", "proposed"} and len(parts) == 1:
        return cast(RankingVariant, parts[0]), None
    if parts[0] == "random" and len(parts) == 2:
        return "random", int(parts[1])
    raise ValueError(f"invalid run-group arm {value!r}")


def _group_schedule(
    arms: list[str], repeat_count: int, interleave_repeats: bool
) -> list[tuple[RankingVariant, int | None, int]]:
    parsed = [_parse_group_arm(arm) for arm in arms]
    if interleave_repeats:
        return [
            (*arm, repeat) for repeat in range(1, repeat_count + 1) for arm in parsed
        ]
    return [(*arm, repeat) for arm in parsed for repeat in range(1, repeat_count + 1)]


def run_group(
    *,
    manifest_path: Path,
    backtest_db: Path,
    historical_db: Path,
    strategy_id: str,
    arms: list[str],
    output_dir: Path,
    repeat_count: int = 1,
    start_month: str | None = None,
    end_month: str | None = None,
    share_strategy_signals: bool = True,
    interleave_repeats: bool = False,
) -> None:
    if repeat_count < 1:
        raise ValueError("repeat_count must be positive")
    registered = _read_frozen_manifest(manifest_path)
    _validate_group_horizon(
        registered,
        strategy_id=strategy_id,
        arms=arms,
        repeat_count=repeat_count,
        start_month=start_month,
        end_month=end_month,
        share_strategy_signals=share_strategy_signals,
        interleave_repeats=interleave_repeats,
    )
    _assert_frozen_runtime(registered)
    shared_planes: dict[str, HistoricalMarketPlanes] = {}
    shared_entry_batches: dict[date, tuple[Signal, ...]] = {}
    shared_initial_selections: dict[date, Any] = {}
    schedule = _group_schedule(arms, repeat_count, interleave_repeats)
    for variant, seed, _repeat in schedule:
        _validate_requested_arm(registered, strategy_id, variant, seed)
    needs_legacy_skills = strategy_id != "SPY" and any(
        variant == "legacy" for variant, _, _ in schedule
    )
    legacy_context = (
        tempfile.TemporaryDirectory(prefix="gh66-legacy-group-")
        if needs_legacy_skills
        else nullcontext(None)
    )
    with legacy_context as legacy_directory:
        archive_root = Path(legacy_directory) if legacy_directory is not None else None
        legacy_skill_root = (
            archive_root / "skills" if archive_root is not None else None
        )
        if archive_root is not None:
            legacy_sources = _materialize_legacy_skill_source(archive_root)
            if (
                legacy_sources
                != registered["variants"]["legacy"]["skill_source_digests"]
            ):
                raise RuntimeError(
                    "materialized legacy Skills differ from frozen identity"
                )
        for variant, seed, repeat in schedule:
            label = f"{variant}-{seed}" if seed is not None else variant
            filename = (
                f"{_arm_name(strategy_id, variant, seed)}.json"
                if repeat_count == 1
                else f"{label}-repeat-{repeat}.json"
            )
            target = output_dir / filename
            if target.exists():
                existing = json.loads(target.read_text())
                _validate_existing_result(
                    existing,
                    registered,
                    strategy_id=strategy_id,
                    variant=variant,
                    seed=seed,
                    start_month=start_month,
                    end_month=end_month,
                )
                print(
                    f"SKIP matching {existing.get('status')} result {target.name}",
                    flush=True,
                )
                continue
            try:
                result = run_one(
                    manifest_path=manifest_path,
                    backtest_db=backtest_db,
                    historical_db=historical_db,
                    strategy_id=strategy_id,
                    variant=variant,
                    seed=seed,
                    output=target,
                    start_month=start_month,
                    end_month=end_month,
                    shared_prepared_planes=shared_planes,
                    shared_entry_signal_cache=(
                        shared_entry_batches if share_strategy_signals else None
                    ),
                    shared_initial_selection_cache=(
                        shared_initial_selections if share_strategy_signals else None
                    ),
                    legacy_skill_root=(
                        legacy_skill_root if variant == "legacy" else None
                    ),
                )
                print(
                    f"ARM {target.name}: {result['status']} "
                    f"seconds={result['performance']['elapsed_seconds']:.2f} "
                    f"planes={result['performance']['prepared_market_plane_count']}",
                    flush=True,
                )
            except Exception as exc:
                identity = _expected_run_identity(
                    registered,
                    strategy_id,
                    variant,
                    seed,
                    start_month,
                    end_month,
                )
                _atomic_json(
                    target,
                    {
                        "schema": RESULT_SCHEMA,
                        "status": "failed",
                        **identity,
                        "failure_type": type(exc).__name__,
                        "failure_detail": str(exc),
                    },
                )
                print(
                    f"ARM {target.name}: failed {type(exc).__name__}: {exc}",
                    flush=True,
                )


def _run_child(command: list[str], label: str, log_path: Path) -> tuple[int, str]:
    print(f"START {label}", flush=True)
    lines: list[str] = []
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    ) as child:
        assert child.stdout is not None
        for line in child.stdout:
            rendered = line.rstrip()
            print(rendered, flush=True)
            lines.append(rendered)
        code = child.wait()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return code, "\n".join(lines)[-8000:]


def _run_command(
    args: argparse.Namespace,
    *,
    strategy: str,
    variant: str,
    target: Path,
    seed: int | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
) -> dict[str, Any]:
    registered = _read_frozen_manifest(args.manifest)
    _assert_frozen_runtime(registered)
    command = [
        sys.executable,
        "-m",
        "scripts.research.gh66_experiment",
        "run-one",
        "--manifest",
        str(args.manifest.resolve()),
        "--backtest-db",
        str(args.backtest_db.resolve()),
        "--historical-db",
        str(args.historical_db.resolve()),
        "--strategy",
        strategy,
        "--variant",
        variant,
        "--output",
        str(target.resolve()),
    ]
    if seed is not None:
        command += ["--seed", str(seed)]
    if start_month is not None:
        command += ["--start-month", start_month]
    if end_month is not None:
        command += ["--end-month", end_month]
    log_path = target.with_suffix(".log")
    if target.exists():
        result = json.loads(target.read_text())
        _validate_existing_result(
            result,
            registered,
            strategy_id=strategy,
            variant=cast(RankingVariant, variant),
            seed=seed,
            start_month=start_month,
            end_month=end_month,
        )
        return result
    code, excerpt = _run_child(command, target.name, log_path)
    if code == 0 and target.exists():
        return json.loads(target.read_text())
    failed = {
        "schema": RESULT_SCHEMA,
        "status": "failed",
        **_expected_run_identity(
            registered,
            strategy,
            cast(RankingVariant, variant),
            seed,
            start_month,
            end_month,
        ),
        "failure_exit_code": code,
        "failure_log": str(log_path.name),
        "failure_excerpt": excerpt,
    }
    _atomic_json(target, failed)
    return failed


def _group_result_path(
    output_dir: Path,
    strategy: str,
    variant: str,
    seed: int | None,
    repeat_count: int,
    repeat: int,
) -> Path:
    label = f"{variant}-{seed}" if seed is not None else variant
    filename = (
        f"{_arm_name(strategy, variant, seed)}.json"
        if repeat_count == 1
        else f"{label}-repeat-{repeat}.json"
    )
    return output_dir / filename


def _run_group_command(
    args: argparse.Namespace,
    *,
    strategy: str,
    arms: list[str],
    output_dir: Path,
    repeat_count: int = 1,
    start_month: str | None = None,
    end_month: str | None = None,
    share_strategy_signals: bool = True,
    interleave_repeats: bool = False,
) -> None:
    registered = _read_frozen_manifest(args.manifest)
    command = [
        sys.executable,
        "-m",
        "scripts.research.gh66_experiment",
        "run-group",
        "--manifest",
        str(args.manifest.resolve()),
        "--backtest-db",
        str(args.backtest_db.resolve()),
        "--historical-db",
        str(args.historical_db.resolve()),
        "--strategy",
        strategy,
        "--output-dir",
        str(output_dir.resolve()),
        "--repeat-count",
        str(repeat_count),
    ]
    for arm in arms:
        command.extend(("--arm", arm))
    if start_month is not None:
        command.extend(("--start-month", start_month))
    if end_month is not None:
        command.extend(("--end-month", end_month))
    if not share_strategy_signals:
        command.append("--no-share-strategy-signals")
    if interleave_repeats:
        command.append("--interleave-repeats")
    log_path = output_dir / f"{strategy}--group.log"
    output_dir.mkdir(parents=True, exist_ok=True)
    code, excerpt = _run_child(command, f"group {strategy}", log_path)
    if code == 0:
        return
    for raw_arm in arms:
        variant, seed = _parse_group_arm(raw_arm)
        for repeat in range(1, repeat_count + 1):
            target = _group_result_path(
                output_dir, strategy, variant, seed, repeat_count, repeat
            )
            if not target.exists():
                identity = _expected_run_identity(
                    registered,
                    strategy,
                    variant,
                    seed,
                    start_month,
                    end_month,
                )
                _atomic_json(
                    target,
                    {
                        "schema": RESULT_SCHEMA,
                        "status": "failed",
                        **identity,
                        "failure_exit_code": code,
                        "failure_excerpt": excerpt,
                    },
                )


def run_short_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    before_path = args.output / "original-results-before.json"
    if not before_path.exists():
        _atomic_json(before_path, _saved_result_snapshot(args.backtest_db))
    short_dir = args.output / "short-replays"
    short_dir.mkdir(parents=True, exist_ok=True)
    arms = ["legacy", "proposed", "random:11"]
    _run_group_command(
        args,
        strategy="rtly-backtest-moving-average",
        arms=arms,
        output_dir=short_dir,
        repeat_count=3,
        start_month="2016-09",
        end_month="2016-11",
        share_strategy_signals=False,
        interleave_repeats=True,
    )
    results: dict[str, list[dict[str, Any]]] = {}
    for name in ("legacy", "proposed", "random-11"):
        results[name] = []
        for repeat in range(1, 4):
            target = short_dir / f"{name}-repeat-{repeat}.json"
            results[name].append(json.loads(target.read_text()))
    proposed = results["proposed"]
    proposed_digests = [
        item.get("simulation_result_sha256")
        for item in proposed
        if item.get("status") == "completed"
    ]
    legacy_times = [
        item["performance"]["total_seconds"]
        for item in results["legacy"]
        if item.get("status") == "completed"
    ]
    proposed_times = [
        item["performance"]["total_seconds"]
        for item in proposed
        if item.get("status") == "completed"
    ]
    legacy_median = statistics.median(legacy_times) if legacy_times else None
    proposed_median = statistics.median(proposed_times) if proposed_times else None
    overhead = (
        proposed_median / legacy_median - 1
        if legacy_median and proposed_median is not None
        else None
    )
    summary = {
        "experiment_id": _read_frozen_manifest(args.manifest)["experiment_id"],
        "strategy_id": "rtly-backtest-moving-average",
        "period": ["2016-09", "2016-11"],
        "repeats_per_variant": 3,
        "signal_cache_shared": False,
        "repeat_schedule": [
            "legacy",
            "proposed",
            "random-11",
            "legacy",
            "proposed",
            "random-11",
            "legacy",
            "proposed",
            "random-11",
        ],
        "runtime_comparison_method": "median total seconds; each arm computes its own Skill output on shared warmed market data",
        "completed_replays": sum(
            item.get("status") == "completed"
            for group in results.values()
            for item in group
        ),
        "planned_replays": 9,
        "variants": {
            name: {
                "statuses": [item.get("status") for item in group],
                "median_seconds": (
                    statistics.median(
                        item["performance"]["total_seconds"]
                        for item in group
                        if item.get("status") == "completed"
                    )
                    if any(item.get("status") == "completed" for item in group)
                    else None
                ),
                "peak_rss_bytes_max": max(
                    (
                        item["performance"]["peak_rss_bytes"]
                        for item in group
                        if item.get("status") == "completed"
                    ),
                    default=None,
                ),
                "audit_rows": [
                    item.get("audit_storage", {}).get("rows") for item in group
                ],
                "audit_payload_bytes": [
                    item.get("audit_storage", {}).get("payload_bytes") for item in group
                ],
            }
            for name, group in results.items()
        },
        "proposed_repeat_digests": proposed_digests,
        "proposed_repeats_deterministic": (
            len(proposed_digests) == 3 and len(set(proposed_digests)) == 1
        ),
        "proposed_vs_legacy_median_runtime_overhead_pct": (
            None if overhead is None else 100 * overhead
        ),
        "overhead_investigation": (
            "triggered if proposed median exceeds legacy median by more than 20%"
            if overhead is not None and overhead > 0.2
            else "not triggered"
            if overhead is not None
            else "inconclusive: one or both variants have no completed replays"
        ),
    }
    _atomic_json(args.output / "short-benchmark.json", summary)
    return summary


def _source_preservation_check(
    args: argparse.Namespace, manifest: dict[str, Any]
) -> dict[str, Any]:
    expected_runs = manifest["original_saved_runs"]
    try:
        current_runs = _original_rows(args.backtest_db)
        current_identity = {
            strategy: {
                "run_id": row["run_id"],
                "run_input_manifest_digest": row["run_input_manifest_digest"],
                "parameters": row["parameters"],
            }
            for strategy, row in current_runs.items()
        }
        expected_identity = {
            strategy: {
                "run_id": row["original_run_id"],
                "run_input_manifest_digest": row["original_manifest_digest"],
                "parameters": row["parameters"],
            }
            for strategy, row in expected_runs.items()
        }
        identity_matches = _sha256(current_identity) == _sha256(expected_identity)
        result_rows_present = True
        detail = None
    except Exception as exc:
        identity_matches = False
        result_rows_present = False
        detail = f"{type(exc).__name__}: {exc}"
    source_before = manifest["source_database_readonly_signatures"]
    source_after = {
        "backtest": _database_signature(args.backtest_db),
        "historical_prices": _database_signature(args.historical_db),
    }
    main_file_matches = {
        role: _database_main_identity(source_before[role])
        == _database_main_identity(source_after[role])
        for role in source_before
    }
    before_path = args.output / "original-results-before.json"
    before_results = (
        json.loads(before_path.read_text()) if before_path.exists() else None
    )
    after_results = _saved_result_snapshot(args.backtest_db)
    result_payloads_unchanged = (
        before_results is not None
        and before_results["snapshot_sha256"] == after_results["snapshot_sha256"]
    )
    all_result_rows_present = all(
        after_results["results"][strategy]["backtest_results"]["rows"] == 1
        for strategy in RUN_IDS
    )
    report = {
        "source_database_signature_before": source_before,
        "source_database_signature_after": source_after,
        "main_file_signatures_unchanged": main_file_matches,
        "sqlite_sidecar_signatures_unchanged": {
            role: source_before[role]["sidecars"] == source_after[role]["sidecars"]
            for role in source_before
        },
        "saved_run_input_identities_unchanged": identity_matches,
        "all_original_result_rows_still_present": (
            result_rows_present and all_result_rows_present
        ),
        "original_result_payloads_unchanged": result_payloads_unchanged,
        "original_result_snapshot_sha256_before": (
            None if before_results is None else before_results["snapshot_sha256"]
        ),
        "original_result_snapshot_sha256_after": after_results["snapshot_sha256"],
        "detail": detail,
        "preserved": (
            identity_matches
            and result_rows_present
            and all_result_rows_present
            and result_payloads_unchanged
        ),
        "write_path": "all experiment connections use SQLite mode=ro plus PRAGMA query_only; replay output is in memory",
    }
    _atomic_json(args.output / "original-results-preservation.json", report)
    return report


def run_matrix(args: argparse.Namespace) -> None:
    manifest = _read_frozen_manifest(args.manifest)
    _assert_frozen_runtime(manifest)
    runs_dir = args.output / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    short_summary_path = args.output / "short-benchmark.json"
    if short_summary_path.exists():
        short_summary = json.loads(short_summary_path.read_text())
        if short_summary.get("experiment_id") != manifest["experiment_id"]:
            raise RuntimeError(
                "existing short benchmark belongs to a different frozen experiment"
            )
    else:
        run_short_benchmark(args)
    completed = 0
    failed = 0
    for strategy_id in STRATEGIES:
        arms = ["legacy", "proposed", *[f"random:{seed}" for seed in SEEDS]]
        _run_group_command(
            args,
            strategy=strategy_id,
            arms=arms,
            output_dir=runs_dir,
        )
        expected_arms = [("legacy", None), ("proposed", None)] + [
            ("random", seed) for seed in SEEDS
        ]
        strategy_results = [
            json.loads(
                (runs_dir / f"{_arm_name(strategy_id, variant, seed)}.json").read_text()
            )
            for variant, seed in expected_arms
        ]
        for result, (variant, seed) in zip(strategy_results, expected_arms):
            _validate_existing_result(
                result,
                manifest,
                strategy_id=strategy_id,
                variant=cast(RankingVariant, variant),
                seed=seed,
            )
        all_results = [json.loads(path.read_text()) for path in runs_dir.glob("*.json")]
        completed = sum(item.get("status") == "completed" for item in all_results)
        failed = sum(item.get("status") != "completed" for item in all_results)
        _atomic_json(
            args.output / "matrix-progress.json",
            {
                "experiment_id": manifest["experiment_id"],
                "planned": 42,
                "completed": completed,
                "failed": failed,
                "last_strategy": strategy_id,
                "strategy_completed": sum(
                    item.get("status") == "completed" for item in strategy_results
                ),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    spy_path = args.output / "spy_benchmark.json"
    spy = _run_command(args, strategy="SPY", variant="legacy", target=spy_path)
    repeat_path = args.output / "full-repeat" / "moving-average-proposed-repeat.json"
    repeat = _run_command(
        args,
        strategy="rtly-backtest-moving-average",
        variant="proposed",
        target=repeat_path,
    )
    representative = json.loads(
        (runs_dir / "rtly-backtest-moving-average--proposed.json").read_text()
    )
    repeat_check = {
        "strategy_id": "rtly-backtest-moving-average",
        "variant": "proposed",
        "status": "completed"
        if representative.get("status") == repeat.get("status") == "completed"
        else "failed",
        "first_result_sha256": representative.get("simulation_result_sha256"),
        "repeat_result_sha256": repeat.get("simulation_result_sha256"),
        "first_equity_sha256": _sha256(representative.get("equity_curve")),
        "repeat_equity_sha256": _sha256(repeat.get("equity_curve")),
    }
    repeat_check["deterministic"] = (
        repeat_check["status"] == "completed"
        and repeat_check["first_result_sha256"] == repeat_check["repeat_result_sha256"]
        and repeat_check["first_equity_sha256"] == repeat_check["repeat_equity_sha256"]
    )
    _atomic_json(args.output / "full-repeat-control.json", repeat_check)
    preservation = _source_preservation_check(args, manifest)
    print(
        f"MATRIX COMPLETE: completed={completed}, failed={failed}, planned=42; "
        f"SPY={spy.get('status')}; full-repeat={repeat_check['status']}; "
        f"source-preserved={preservation['preserved']}",
        flush=True,
    )


def _summary(
    results: list[dict[str, Any]],
    benchmark: dict[str, Any] | None,
    short_benchmark: dict[str, Any] | None,
    repeat_control: dict[str, Any] | None,
    preservation: dict[str, Any] | None,
) -> dict[str, Any]:
    by_strategy: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        arms = [item for item in results if item.get("strategy_id") == strategy]
        randoms = [
            item["metrics"]["total_return"]
            for item in arms
            if item.get("variant") == "random" and item.get("status") == "completed"
        ]
        proposed = next(
            (
                item["metrics"]["total_return"]
                for item in arms
                if item.get("variant") == "proposed"
                and item.get("status") == "completed"
            ),
            None,
        )
        legacy = next(
            (
                item["metrics"]["total_return"]
                for item in arms
                if item.get("variant") == "legacy" and item.get("status") == "completed"
            ),
            None,
        )
        by_strategy[strategy] = {
            "arms": len(arms),
            "completed": sum(item.get("status") == "completed" for item in arms),
            "failed": sum(item.get("status") != "completed" for item in arms),
            "legacy_total_return": legacy,
            "proposed_total_return": proposed,
            "random_total_return_count": len(randoms),
            "random_total_return_min": min(randoms) if randoms else None,
            "random_total_return_median": statistics.median(randoms)
            if randoms
            else None,
            "random_total_return_mean": statistics.fmean(randoms) if randoms else None,
            "random_total_return_max": max(randoms) if randoms else None,
        }
    return {
        "strategies": by_strategy,
        "spy_benchmark": benchmark,
        "short_benchmark": short_benchmark,
        "full_repeat_control": repeat_control,
        "original_results_preservation": preservation,
    }


def _read_results(output: Path) -> list[dict[str, Any]]:
    results = []
    for path in sorted((output / "runs").glob("*.json")):
        results.append(json.loads(path.read_text()))
    return results


def write_report(manifest_path: Path, output: Path) -> None:
    frozen = _read_frozen_manifest(manifest_path)
    results = _read_results(output)
    for result in results:
        variant = result.get("variant")
        strategy_id = result.get("strategy_id")
        if variant not in {"legacy", "proposed", "random"}:
            raise RuntimeError("report contains an unknown GH #66 run variant")
        if not isinstance(strategy_id, str):
            raise RuntimeError("report contains a GH #66 run without a strategy ID")
        _validate_existing_result(
            result,
            frozen,
            strategy_id=strategy_id,
            variant=cast(RankingVariant, variant),
            seed=result.get("seed"),
        )
    benchmark_path = output / "spy_benchmark.json"
    benchmark = (
        json.loads(benchmark_path.read_text()) if benchmark_path.exists() else None
    )
    if benchmark is not None:
        _validate_existing_result(
            benchmark,
            frozen,
            strategy_id="SPY",
            variant="legacy",
            seed=None,
        )
    short_path = output / "short-benchmark.json"
    short_benchmark = (
        json.loads(short_path.read_text()) if short_path.exists() else None
    )
    repeat_path = output / "full-repeat-control.json"
    repeat_control = (
        json.loads(repeat_path.read_text()) if repeat_path.exists() else None
    )
    preservation_path = output / "original-results-preservation.json"
    preservation = (
        json.loads(preservation_path.read_text())
        if preservation_path.exists()
        else None
    )
    summary = _summary(
        results, benchmark, short_benchmark, repeat_control, preservation
    )
    _atomic_json(output / "summary.json", summary)
    _write_curve_csv(
        output / "equity-curves.csv", [*results, *([benchmark] if benchmark else [])]
    )
    _write_html_report(
        output / "report.html",
        frozen,
        results,
        summary,
        benchmark,
        short_benchmark,
        repeat_control,
        preservation,
    )


def _write_curve_csv(path: Path, results: list[dict[str, Any]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            (
                "strategy_id",
                "variant",
                "seed",
                "session",
                "cash_gbp",
                "positions_gbp",
                "equity_gbp",
            )
        )
        for run in results:
            for point in run.get("equity_curve", ()):
                writer.writerow(
                    (
                        run.get("strategy_id", "SPY"),
                        run.get("variant", "benchmark"),
                        run.get("seed", ""),
                        point["session"],
                        point["cash_base"],
                        point["positions_value_base"],
                        point["total_equity_base"],
                    )
                )
    os.replace(temp, path)


def _svg_panel(
    title: str, runs: list[dict[str, Any]], benchmark: dict[str, Any] | None
) -> str:
    candidates = [run for run in runs if run.get("status") == "completed"]
    if benchmark and benchmark.get("status") == "completed":
        candidates.append(benchmark)
    if not candidates:
        return f"<section><h3>{html.escape(title)}</h3><p>No completed equity curves.</p></section>"
    values = [
        float(point["total_equity_base"])
        for run in candidates
        for point in run.get("equity_curve", ())
    ]
    minimum, maximum = min(values), max(values)
    span = max(maximum - minimum, 1.0)
    colors = (
        "#eb7753",
        "#78a6ce",
        "#77b995",
        "#ba93ce",
        "#d6ac57",
        "#68b4bd",
        "#d5839b",
        "#e6e3d7",
    )
    paths = []
    for index, run in enumerate(candidates):
        points = run.get("equity_curve", ())
        if not points:
            continue
        n = max(len(points) - 1, 1)
        coords = []
        for item, point in enumerate(points):
            x = 35 + 830 * item / n
            y = 15 + 220 * (1 - (float(point["total_equity_base"]) - minimum) / span)
            coords.append(f"{x:.1f},{y:.1f}")
        name = (
            f"SPY {run.get('variant', '')}"
            if run.get("strategy_id") == "SPY"
            else f"{run.get('variant')} {run.get('seed') or ''}".strip()
        )
        legend_x = 40 + (index % 4) * 205
        legend_y = 265 + (index // 4) * 17
        paths.append(
            f'<polyline fill="none" stroke="{colors[index % len(colors)]}" stroke-width="2" points="{" ".join(coords)}"/>'
            f'<text x="{legend_x}" y="{legend_y}" fill="{colors[index % len(colors)]}" font-size="11">{html.escape(name)}</text>'
        )
    return (
        f'<section><h3>{html.escape(title)}</h3><svg viewBox="0 0 900 305" role="img" aria-label="Equity curves for {html.escape(title)}">'
        f'<path d="M35 15V245H865" stroke="#687078" fill="none"/><text x="38" y="12">£{maximum:,.0f}</text><text x="38" y="242">£{minimum:,.0f}</text>{"".join(paths)}</svg></section>'
    )


def _percent(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float, Decimal)):
        return f"{float(value):.1%}"
    return str(value)


def _number(value: object, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float, Decimal)):
        return f"{float(value):,.{digits}f}"
    return str(value)


def _write_html_report(
    path: Path,
    manifest: dict[str, Any],
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    benchmark: dict[str, Any] | None,
    short_benchmark: dict[str, Any] | None,
    repeat_control: dict[str, Any] | None,
    preservation: dict[str, Any] | None,
) -> None:
    rows = []
    for run in results:
        metrics = run.get("metrics", {})
        rows.append(
            "<tr>"
            + "".join(
                f"<td>{html.escape(str(value if value is not None else '—'))}</td>"
                for value in (
                    run.get("strategy_id"),
                    run.get("variant"),
                    run.get("seed", ""),
                    run.get("status"),
                    _percent(metrics.get("total_return")),
                    _percent(metrics.get("cagr")),
                    _percent(metrics.get("max_drawdown")),
                    _number(metrics.get("sharpe_ratio")),
                    _number(metrics.get("mean_invested_exposure_pct")),
                    _number(metrics.get("turnover_on_initial_capital")),
                    metrics.get("exit_count"),
                    metrics.get("contested_selections"),
                    _number(metrics.get("priority_coverage_pct")),
                    _number(metrics.get("explanation_coverage_pct")),
                    _number(metrics.get("momentum_evidence_coverage_pct")),
                    run.get("audit_storage", {}).get("rows"),
                    run.get("audit_storage", {}).get("payload_bytes"),
                )
            )
            + "</tr>"
        )
    charts = "".join(
        _svg_panel(
            strategy,
            [run for run in results if run.get("strategy_id") == strategy],
            benchmark,
        )
        for strategy in STRATEGIES
    )
    spy_row = "No benchmark result recorded."
    if benchmark:
        spy_row = (
            f"status={html.escape(str(benchmark.get('status')))}, "
            f"return={html.escape(_percent(benchmark.get('metrics', {}).get('total_return')))}, "
            f"CAGR={html.escape(_percent(benchmark.get('metrics', {}).get('cagr')))}, "
            f"max drawdown={html.escape(_percent(benchmark.get('metrics', {}).get('max_drawdown')))}, "
            f"Sharpe={html.escape(_number(benchmark.get('metrics', {}).get('sharpe_ratio')))}"
        )
    random_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(value if value is not None else '—'))}</td>"
            for value in (
                strategy,
                _percent(values["legacy_total_return"]),
                _percent(values["proposed_total_return"]),
                _percent(values["random_total_return_min"]),
                _percent(values["random_total_return_median"]),
                _percent(values["random_total_return_max"]),
                values["random_total_return_count"],
            )
        )
        + "</tr>"
        for strategy, values in summary["strategies"].items()
    )
    fixture = manifest["fixed_candidate_admission_fixture"]
    fixture_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(value))}</td>"
            for value in (
                variant,
                detail["ordering_source"],
                detail["admitted_security_ids"][0],
                ", ".join(
                    f"{candidate['security_id']} (priority {candidate['priority'] if candidate['priority'] is not None else '—'})"
                    for candidate in detail["ordered_candidates"]
                ),
            )
        )
        + "</tr>"
        for variant, detail in fixture["variants"].items()
    )
    short_text = "No short-replay timing recorded."
    if short_benchmark:
        short_text = (
            f"{short_benchmark['completed_replays']}/{short_benchmark['planned_replays']} "
            f"short replays completed; proposed-versus-legacy median runtime overhead "
            f"{_percent(None if short_benchmark['proposed_vs_legacy_median_runtime_overhead_pct'] is None else short_benchmark['proposed_vs_legacy_median_runtime_overhead_pct'] / 100)}; "
            f"repeat deterministic={short_benchmark['proposed_repeats_deterministic']}; "
            f"overhead investigation={html.escape(short_benchmark['overhead_investigation'])}."
        )
    repeat_text = (
        "No full repeat recorded."
        if repeat_control is None
        else f"Full Moving Average proposed repeat deterministic={repeat_control.get('deterministic')}."
    )
    preservation_text = (
        "Original database preservation check is unavailable."
        if preservation is None
        else f"Original saved Results preserved={preservation.get('preserved')} "
        f"(main database signatures unchanged={preservation.get('main_file_signatures_unchanged')}; "
        f"run inputs unchanged={preservation.get('saved_run_input_identities_unchanged')})."
    )
    fixed = manifest["fixed_inputs"]
    document = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GH #66 controlled ranking evaluation</title>
<style>
body{{margin:0;background:#101519;color:#e6e3d7;font:15px/1.5 system-ui,sans-serif}}main{{max-width:1280px;margin:auto;padding:32px}}h1,h2,h3{{font-family:Georgia,serif;font-weight:500}}p,li{{color:#bbc3c5}}.meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}.meta div{{background:#1b2428;padding:12px;border:1px solid #354044}}table{{border-collapse:collapse;width:100%;font-size:12px;overflow:auto}}th,td{{border-bottom:1px solid #354044;padding:7px;text-align:left;white-space:nowrap}}th{{color:#c59c74;position:sticky;top:0;background:#101519}}.scroll{{overflow:auto}}section{{margin:28px 0;background:#1b2428;padding:14px}}svg{{width:100%;height:auto}}.warning{{border-left:3px solid #e0a65e;padding-left:14px}}a{{color:#83b6d0}}
</style><main><h1>Entry ranking: legacy, proposed and random controls</h1>
<p>Experiment <code>{html.escape(manifest["experiment_id"])}</code> · frozen {html.escape(manifest["created_at"])}</p>
<div class="meta"><div>Horizon<br><b>{fixed["start_month"]} — {fixed["end_month"]}</b></div><div>Initial capital<br><b>£{fixed["starting_capital"]}</b></div><div>Universe<br><b>{fixed["selected_security_count"]} securities</b></div><div>Position cap<br><b>{fixed["max_concurrent_positions"]}</b></div><div>Random seeds<br><b>{", ".join(map(str, SEEDS))}</b></div></div>
<p class="warning">Diagnostic only: this decade was already inspected and the original 755-security roster is survivor-universe data. The results do not establish out-of-sample performance. All arms are gross of commission, spread, and slippage. Full-replay audit candidate counts can differ after portfolios diverge and are descriptive, not directly comparable.</p>
<h2>Fixed candidate-state admission control</h2><p>One free slot, no existing positions or pending buys, and the same eligible candidates are held constant. The engine first applies its stable host cohort order, then allocates the slot by priority; absent legacy priorities sort last. This isolates how the selection rule changes an admission.</p><div class="scroll"><table><thead><tr><th>Variant</th><th>Ordering source</th><th>Admitted candidate</th><th>Allocation order and priorities</th></tr></thead><tbody>{fixture_rows}</tbody></table></div>
<h2>Legacy, proposed and random outcome ranges</h2><div class="scroll"><table><thead><tr><th>Strategy</th><th>Legacy return</th><th>Proposed return</th><th>Random min</th><th>Random median</th><th>Random max</th><th>Seeds completed</th></tr></thead><tbody>{random_rows}</tbody></table></div>
<h2>Portfolio paths</h2>{charts}
<h2>SPY benchmark</h2><p>{spy_row}</p><p>Same GBP starting capital, date range, pinned SPY prices and FX; the engine credits cash dividends without reinvestment and applies the same gross cost assumptions.</p>
<h2>Run ledger and metrics</h2><p>{len(results)} planned arms have records; missing and failed arms remain visible.</p>
<div class="scroll"><table><thead><tr><th>Strategy</th><th>Variant</th><th>Seed</th><th>Status</th><th>Return</th><th>CAGR</th><th>Max drawdown</th><th>Sharpe</th><th>Exposure %</th><th>Turnover / capital</th><th>Exits</th><th>Contested</th><th>Priority evidence %</th><th>Explanation %</th><th>Momentum evidence %</th><th>Audit rows</th><th>Audit payload bytes</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
<h2>Audit and performance</h2><p>Short replay: {short_text} {repeat_text} {preservation_text}</p><p>Per-run JSON records include candidate-audit row counts and digests, priority/explanation coverage, contested/full-book outcomes, serialized audit payload bytes, isolated SQLite page allocation, runtime, and process peak RSS in bytes.</p>
<p>See the adjacent <code>summary.json</code> and <code>equity-curves.csv</code> for machine-readable summaries and full curves.</p>
<h2>Reproducibility</h2><p>Variant adapter: SHA-256 seed/session/security ordering ({html.escape(RANDOM_ORDER_VERSION)}). Host execution-contract digest: <code>{html.escape(manifest["host_execution_contract"]["digest"])}</code>. Experiment sources and pins are recorded in the frozen manifest.</p>
</main></html>"""
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(document)
    os.replace(temp, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--backtest-db", type=Path, required=True)
    freeze.add_argument("--historical-db", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run-one")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--backtest-db", type=Path, required=True)
    run.add_argument("--historical-db", type=Path, required=True)
    run.add_argument("--strategy", required=True)
    run.add_argument(
        "--variant", choices=("legacy", "proposed", "random"), required=True
    )
    run.add_argument("--seed", type=int)
    run.add_argument("--start-month")
    run.add_argument("--end-month")
    run.add_argument("--output", type=Path, required=True)
    group = sub.add_parser("run-group")
    group.add_argument("--manifest", type=Path, required=True)
    group.add_argument("--backtest-db", type=Path, required=True)
    group.add_argument("--historical-db", type=Path, required=True)
    group.add_argument("--strategy", required=True)
    group.add_argument("--arm", action="append", required=True)
    group.add_argument("--repeat-count", type=int, default=1)
    group.add_argument("--no-share-strategy-signals", action="store_true")
    group.add_argument("--interleave-repeats", action="store_true")
    group.add_argument("--start-month")
    group.add_argument("--end-month")
    group.add_argument("--output-dir", type=Path, required=True)
    matrix = sub.add_parser("matrix")
    matrix.add_argument("--manifest", type=Path, required=True)
    matrix.add_argument("--backtest-db", type=Path, required=True)
    matrix.add_argument("--historical-db", type=Path, required=True)
    matrix.add_argument("--output", type=Path, required=True)
    short = sub.add_parser("short-bench")
    short.add_argument("--manifest", type=Path, required=True)
    short.add_argument("--backtest-db", type=Path, required=True)
    short.add_argument("--historical-db", type=Path, required=True)
    short.add_argument("--output", type=Path, required=True)
    report = sub.add_parser("report")
    report.add_argument("--manifest", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze_manifest(args.backtest_db, args.historical_db, args.output)
        print(f"FROZEN {result['experiment_id']} at {args.output}")
    elif args.command == "run-one":
        result = run_one(
            manifest_path=args.manifest,
            backtest_db=args.backtest_db,
            historical_db=args.historical_db,
            strategy_id=args.strategy,
            variant=args.variant,
            seed=args.seed,
            output=args.output,
            start_month=args.start_month,
            end_month=args.end_month,
        )
        print(
            f"DONE {args.strategy} {args.variant} seed={args.seed} "
            f"sessions={len(result['equity_curve'])} seconds={result['performance']['elapsed_seconds']:.2f} "
            f"digest={result['simulation_result_sha256']}"
        )
    elif args.command == "run-group":
        run_group(
            manifest_path=args.manifest,
            backtest_db=args.backtest_db,
            historical_db=args.historical_db,
            strategy_id=args.strategy,
            arms=args.arm,
            output_dir=args.output_dir,
            repeat_count=args.repeat_count,
            start_month=args.start_month,
            end_month=args.end_month,
            share_strategy_signals=not args.no_share_strategy_signals,
            interleave_repeats=args.interleave_repeats,
        )
    elif args.command == "matrix":
        run_matrix(args)
        write_report(args.manifest, args.output)
    elif args.command == "short-bench":
        summary = run_short_benchmark(args)
        print(
            f"SHORT BENCHMARK: {summary['completed_replays']}/"
            f"{summary['planned_replays']} completed; "
            f"proposed deterministic={summary['proposed_repeats_deterministic']}",
            flush=True,
        )
    else:
        write_report(args.manifest, args.output)


if __name__ == "__main__":
    main()
