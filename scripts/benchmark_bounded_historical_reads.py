"""Read-only storage and replay benchmark for GH-602.

The storage probe opens the selected evidence database read-only. ``--run-id``
adds a matched full-materialization versus chunk-backed Engine replay from a
pinned manifest in the backtest database; neither mode calls a provider.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, timedelta
import json
from pathlib import Path
import platform
import resource
import shlex
import sqlite3
import sys
from time import perf_counter
from typing import cast

from app.core.config import BACKTEST_DB, HISTORICAL_PRICE_CACHE, SKILLS_DIR
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import (
    HistoricalEvidenceReadHandle,
    HistoricalPriceRepository,
)
from app.services.backtest.backtest_engine import (
    InMemorySessionBatchSink,
    MarketDataAccessV1,
    SecurityMarketDataV1,
    run_simulation,
)
from app.services.backtest.market_planes import HistoricalMarketPlanes
from app.services.backtest.market_view import MarketView
from app.services.backtest.run_input_manifest import read_run_input_manifest
from app.services.backtest.skill_discovery import discover_strategies
from app.services.backtest.worker import _load_strategy_instance

MANIFEST_DIGEST = "d568f7c5-afcd-48c7-aa6a-fb7aa78183e8"
READ_REFERENCE = {
    "Buy and Hold": 117.6,
    "Darvas": 180.4,
    "Moving Average": 316.7,
    "Turtle Trend": 169.5,
    "Minervini": 449.9,
    "Weinstein": 696.2,
    "Minervini, upgrade enabled": 484.9,
    "Weinstein, upgrade enabled": 692.0,
}
STAGING_REFERENCE = {
    "Buy and Hold": 119.5,
    "Darvas": 202.5,
    "Moving Average": 351.4,
    "Turtle Trend": 355.262,
    "Minervini": 547.5,
    "Weinstein": 818.164,
    "Minervini, upgrade enabled": 534.3,
    "Weinstein, upgrade enabled": 855.140,
}
STRATEGY_LABELS = {
    "rtly-backtest-buy-and-hold": "Buy and Hold",
    "rtly-backtest-darvas-box": "Darvas",
    "rtly-backtest-moving-average": "Moving Average",
    "rtly-backtest-turtle-trend": "Turtle Trend",
    "rtly-backtest-minervini": "Minervini",
    "rtly-backtest-weinstein": "Weinstein",
}


def _read_only_connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _rss_mib() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value / (1024 * 1024) if platform.system() == "Darwin" else value / 1024


def _select_revision(path: Path, requested: str | None) -> tuple[str, date, date, int]:
    with _read_only_connect(path) as conn:
        if requested is None:
            row = conn.execute(
                """SELECT data_revision, json_extract(metadata_json, '$.request.start'),
                          json_extract(metadata_json, '$.request.end'), observation_count
                   FROM historical_price_v2_revisions
                   WHERE observation_count >= 254
                   ORDER BY observation_count, data_revision LIMIT 1"""
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT data_revision, json_extract(metadata_json, '$.request.start'),
                          json_extract(metadata_json, '$.request.end'), observation_count
                   FROM historical_price_v2_revisions WHERE data_revision=?""",
                (requested,),
            ).fetchone()
    if row is None:
        raise SystemExit("selected v2 revision is unavailable")
    return (
        str(row[0]),
        date.fromisoformat(str(row[1])),
        date.fromisoformat(str(row[2])),
        int(row[3]),
    )


def _delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after[key] - before[key] for key in before}


def _measure(repo: HistoricalPriceRepository, revision: str, through: date, limit: int):
    repo.reset_read_counters()
    access_started = perf_counter()
    handle = repo.open_read(revision)
    initialization_seconds = perf_counter() - access_started
    before = asdict(repo.read_counters)
    bounded_started = perf_counter()
    bounded = handle.bounded(through=through, limit=limit)
    bounded_seconds = perf_counter() - bounded_started
    bounded_counters = _delta(before, asdict(repo.read_counters))
    before = asdict(repo.read_counters)
    full_started = perf_counter()
    full = repo.get_v2(revision)
    full_seconds = perf_counter() - full_started
    full_counters = _delta(before, asdict(repo.read_counters))
    earliest = (
        date.fromisoformat(str(bounded.rows[0]["session"])) if bounded.rows else None
    )
    full_rows = tuple(
        row for row in full.rows if date.fromisoformat(str(row["session"])) <= through
    )
    if len(full_rows) > limit:
        full_rows = full_rows[-limit:]
    full_actions = (
        tuple(
            action
            for action in full.actions
            if earliest is not None and str(action["session"]) >= earliest.isoformat()
        )
        if earliest is not None
        else ()
    )
    metadata = handle.metadata
    equivalent = (
        tuple(full_rows) == tuple(bounded.rows) and full_actions == bounded.actions
    )
    handle.close()
    return {
        "revision": revision,
        "metadata": {
            "security_id": metadata.security_id,
            "start": metadata.start,
            "end": metadata.end,
            "observation_count": metadata.observation_count,
            "action_count": metadata.action_count,
        },
        "through": through.isoformat(),
        "limit": limit,
        "selected_price_chunk_years": list(bounded.selected_price_chunk_years),
        "selected_action_chunk_years": list(bounded.selected_action_chunk_years),
        "rows_retained": len(bounded.rows),
        "actions_retained": len(bounded.actions),
        "replay_projection_equivalent": equivalent,
        "initialization_seconds": initialization_seconds,
        "bounded_read_seconds": bounded_seconds,
        "full_read_seconds": full_seconds,
        "bounded_counters": bounded_counters,
        "full_counters": full_counters,
        "peak_rss_mib": _rss_mib(),
    }


def _execution_request(path: Path, requested: str | None) -> tuple[str, str, str]:
    with _read_only_connect(path) as conn:
        if requested is None:
            row = conn.execute(
                """SELECT strategy_runs.id, strategy_runs.strategy_id,
                          strategy_runs.run_input_manifest_digest
                   FROM strategy_runs
                   JOIN strategy_jobs ON strategy_jobs.id=strategy_runs.id
                   JOIN run_input_manifests
                     ON run_input_manifests.digest=strategy_runs.run_input_manifest_digest
                   WHERE strategy_jobs.status='complete'
                     AND json_array_length(
                           json_extract(run_input_manifests.canonical_manifest_json,
                                        '$.securities')
                         )=738
                   ORDER BY strategy_runs.created_at DESC LIMIT 1"""
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT strategy_runs.id, strategy_runs.strategy_id,
                          strategy_runs.run_input_manifest_digest
                   FROM strategy_runs
                   JOIN strategy_jobs ON strategy_jobs.id=strategy_runs.id
                   WHERE strategy_runs.id=? AND strategy_jobs.status='complete'""",
                (requested,),
            ).fetchone()
        if row is None:
            raise SystemExit(
                "selected completed 738-security backtest run is unavailable"
            )
        return str(row[0]), str(row[1]), str(row[2])


def _manifest_for_run(path: Path, run_id: str, expected_digest: str):
    with _read_only_connect(path) as conn:
        row = conn.execute(
            "SELECT canonical_manifest_json FROM run_input_manifests WHERE digest=?",
            (expected_digest,),
        ).fetchone()
    if row is None:
        raise SystemExit("selected run input manifest is unavailable")
    manifest = read_run_input_manifest(str(row[0]))
    if manifest.digest() != expected_digest:
        raise SystemExit(f"manifest digest mismatch for run {run_id}")
    if len(manifest.securities) != 738:
        raise SystemExit("selected run does not pin the approved 738-security workload")
    return manifest


def _measure_execution(
    *,
    prices: HistoricalPriceRepository,
    backtests: BacktestRepository,
    manifest_path: Path,
    run_id: str | None,
) -> dict[str, object]:
    selected_run_id, strategy_id, manifest_digest = _execution_request(
        manifest_path, run_id
    )
    manifest = _manifest_for_run(manifest_path, selected_run_id, manifest_digest)
    descriptor = next(
        (
            item
            for item in discover_strategies(SKILLS_DIR).strategies
            if item.strategy_id == strategy_id
        ),
        None,
    )
    if descriptor is None:
        raise SystemExit(f"strategy {strategy_id!r} is no longer discoverable")
    revisions = {item.price_revision for item in manifest.securities}
    fx_revisions = {
        item.fx_revision for item in manifest.securities if item.fx_revision
    }
    if len(fx_revisions) > 1:
        raise SystemExit("selected manifest pins more than one FX revision")
    fx_evidence = prices.get(next(iter(fx_revisions))) if fx_revisions else None
    security_revisions = {
        item.security_id: item.price_revision for item in manifest.securities
    }
    selected_universe = tuple(item.security_id for item in manifest.securities)

    def run(mode: str) -> dict[str, object]:
        prices.reset_read_counters()
        handles: list[HistoricalEvidenceReadHandle] = []
        prepared_planes: dict[str, HistoricalMarketPlanes] = {}
        try:
            started = perf_counter()
            if mode == "full":
                evidence_by_revision = {
                    revision: prices.get(revision) for revision in revisions
                }
                security_data = tuple(
                    SecurityMarketDataV1(
                        security_id=item.security_id,
                        price_evidence=evidence_by_revision[item.price_revision],
                    )
                    for item in manifest.securities
                )
                price_accesses = None
            else:
                security_data_list = []
                for item in manifest.securities:
                    handle = prices.open_read(item.price_revision)
                    handles.append(handle)
                    security_data_list.append(
                        SecurityMarketDataV1(
                            security_id=item.security_id,
                            price_access=cast(MarketDataAccessV1, handle),
                        )
                    )
                security_data = tuple(security_data_list)
                price_accesses = {
                    item.security_id: handle
                    for item, handle in zip(manifest.securities, handles)
                }
            resolution_seconds = perf_counter() - started

            def market_view_factory(session: date) -> MarketView:
                return MarketView(
                    as_of_session=session,
                    profile_hash=manifest.profile_hash,
                    security_price_revisions=security_revisions,
                    selected_universe=selected_universe,
                    backtest_repo=backtests,
                    historical_price_repo=prices,
                    prepared_planes=prepared_planes,
                    price_accesses=price_accesses,
                )

            engine_started = perf_counter()
            output = run_simulation(
                manifest=manifest,
                strategy=_load_strategy_instance(SKILLS_DIR / descriptor.runtime_path),
                market_view_factory=market_view_factory,
                security_market_data=security_data,
                fx_evidence=fx_evidence,
                sink=InMemorySessionBatchSink(),
                prepared_planes=prepared_planes,
            )
            total_seconds = perf_counter() - started
            return {
                "seconds": total_seconds,
                "resolution_seconds": resolution_seconds,
                "engine_seconds": perf_counter() - engine_started,
                "events": len(output.events),
                "equity_points": len(output.equity_curve),
                "final_open_positions": len(output.final_open_positions),
                "final_cash_base": str(output.final_cash_base),
                "counters": asdict(prices.read_counters),
                "peak_rss_mib": _rss_mib(),
                "sessions_processed": len(output.equity_curve),
                "output": output.model_dump(mode="python"),
            }
        finally:
            for handle in handles:
                handle.close()
            prepared_planes.clear()

    full = run("full")
    lazy = run("lazy")
    if full["sessions_processed"] != 406 or lazy["sessions_processed"] != 406:
        raise SystemExit(
            "selected run did not process the approved 406-session workload"
        )
    mode_label = STRATEGY_LABELS.get(strategy_id, strategy_id)
    if manifest.parameters.get("enable_position_upgrade") is True:
        mode_label += ", upgrade enabled"

    def comparison(
        measured: object, reference: float | None
    ) -> dict[str, object] | None:
        if not isinstance(measured, (int, float)) or reference is None:
            return None
        delta = float(measured) - reference
        return {
            "reference_seconds": reference,
            "delta_seconds": delta,
            "delta_percent": delta / reference * 100,
        }

    full_output = full["output"]
    lazy_output = lazy["output"]
    assert isinstance(full_output, dict) and isinstance(lazy_output, dict)
    return {
        "run_id": selected_run_id,
        "strategy_id": strategy_id,
        "mode_label": mode_label,
        "manifest_digest": manifest_digest,
        "security_count": len(manifest.securities),
        "fx_revision": next(iter(fx_revisions), None),
        "full": {key: value for key, value in full.items() if key != "output"},
        "lazy": {key: value for key, value in lazy.items() if key != "output"},
        "replay_equivalent": full_output == lazy_output,
        "manifest_identity_equivalent": full_output["manifest_digest"]
        == lazy_output["manifest_digest"],
        "fx_input_identity_equivalent": True,
        "full_vs_read_reference": comparison(
            full["seconds"], READ_REFERENCE.get(mode_label)
        ),
        "full_vs_staging_reference": comparison(
            full["seconds"], STAGING_REFERENCE.get(mode_label)
        ),
        "lazy_vs_read_reference": comparison(
            lazy["seconds"], READ_REFERENCE.get(mode_label)
        ),
        "lazy_vs_staging_reference": comparison(
            lazy["seconds"], STAGING_REFERENCE.get(mode_label)
        ),
    }


def _markdown(result: dict[str, object], *, database: Path) -> str:
    storage = result["storage_probe"]
    assert isinstance(storage, dict)
    rows = [
        "---",
        "story: 602.3",
        "issue: 613",
        f"date: {date.today().isoformat()}",
        "---",
        "",
        "# GH-602 bounded historical read benchmark",
        "",
        f"Command: `{result['command']}`",
        f"Database: `{database}` (opened read-only; no provider/network access)",
        f"Manifest/workload reference: `{result['manifest_digest']}`; the approved 738-security, 406-session workload is retained as the timing reference.",
        "",
        "## Storage probe",
        "",
        "| Metric | Result |",
        "| --- | ---: |",
        f"| Revision | `{storage['revision']}` |",
        f"| Through | {storage['through']} |",
        f"| Requested rows | {storage['limit']} |",
        f"| Selected price chunk years | {storage['selected_price_chunk_years']} |",
        f"| Selected action chunk years | {storage['selected_action_chunk_years']} |",
        f"| Rows retained | {storage['rows_retained']} |",
        f"| Actions retained | {storage['actions_retained']} |",
        f"| Full/bounded projection equivalent | {storage['replay_projection_equivalent']} |",
        f"| Bounded read seconds | {storage['bounded_read_seconds']:.6f} |",
        f"| Full v2 read seconds | {storage['full_read_seconds']:.6f} |",
        f"| Peak RSS MiB | {storage['peak_rss_mib']:.2f} |",
        "",
        "### Storage counters",
        "",
        "```json",
        json.dumps(
            {
                "bounded": storage["bounded_counters"],
                "full": storage["full_counters"],
            },
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "The bounded path must show zero complete revision materializations; annual chunks are decoded as whole JSON chunks under the current schema.",
        "",
        "## End-to-end replay",
        "",
    ]
    execution = result.get("execution_probe")
    if isinstance(execution, dict):
        full = execution["full"]
        lazy = execution["lazy"]
        full_read_comparison = execution["full_vs_read_reference"]
        full_staging_comparison = execution["full_vs_staging_reference"]
        lazy_read_comparison = execution["lazy_vs_read_reference"]
        lazy_staging_comparison = execution["lazy_vs_staging_reference"]
        assert isinstance(full, dict) and isinstance(lazy, dict)
        assert isinstance(full_read_comparison, dict)
        assert isinstance(full_staging_comparison, dict)
        assert isinstance(lazy_read_comparison, dict)
        assert isinstance(lazy_staging_comparison, dict)
        rows.extend(
            [
                f"| Run | `{execution['run_id']}` |",
                f"| Strategy/mode | {execution['mode_label']} |",
                f"| Manifest digest | `{execution['manifest_digest']}` |",
                f"| Pinned securities | {execution['security_count']} |",
                f"| FX revision | `{execution['fx_revision']}` |",
                f"| Sessions processed | {full['sessions_processed']} |",
                f"| Full replay seconds | {full['seconds']:.3f} |",
                f"| Chunk-backed replay seconds | {lazy['seconds']:.3f} |",
                f"| Full evidence/access resolution seconds | {full['resolution_seconds']:.3f} |",
                f"| Chunk-backed access resolution seconds | {lazy['resolution_seconds']:.3f} |",
                f"| Full Engine setup + simulation seconds | {full['engine_seconds']:.3f} |",
                f"| Chunk-backed Engine setup + simulation seconds | {lazy['engine_seconds']:.3f} |",
                f"| Full vs 613 read-workload delta | {full_read_comparison['delta_seconds']:.3f}s ({full_read_comparison['delta_percent']:.1f}%) |",
                f"| Full vs 613 staging delta | {full_staging_comparison['delta_seconds']:.3f}s ({full_staging_comparison['delta_percent']:.1f}%) |",
                f"| Chunk-backed vs 613 read-workload delta | {lazy_read_comparison['delta_seconds']:.3f}s ({lazy_read_comparison['delta_percent']:.1f}%) |",
                f"| Chunk-backed vs 613 staging delta | {lazy_staging_comparison['delta_seconds']:.3f}s ({lazy_staging_comparison['delta_percent']:.1f}%) |",
                f"| Canonical manifest identity equivalent | {execution['manifest_identity_equivalent']} |",
                f"| FX input identity equivalent | {execution['fx_input_identity_equivalent']} |",
                f"| Full/chunk-backed Engine output equivalent | {execution['replay_equivalent']} |",
                "",
                "The 613 references are different workloads: read-workload timing discards orders, while staging timing includes the worker sink. These new full/chunk-backed measurements run the real Engine with an in-memory sink, so the deltas are directional rather than a claim of like-for-like staging improvement.",
                "",
                "| Mode | Seconds | Events | Equity points | Final positions | Complete materializations | Chunks decompressed | Compressed bytes | Uncompressed bytes |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
                f"| Full materialization | {full['seconds']:.3f} | {full['events']} | {full['equity_points']} | {full['final_open_positions']} | {full['counters']['complete_revision_materializations']} | {full['counters']['chunks_decompressed']} | {full['counters']['compressed_bytes']} | {full['counters']['uncompressed_bytes']} |",
                f"| Chunk-backed | {lazy['seconds']:.3f} | {lazy['events']} | {lazy['equity_points']} | {lazy['final_open_positions']} | {lazy['counters']['complete_revision_materializations']} | {lazy['counters']['chunks_decompressed']} | {lazy['counters']['compressed_bytes']} | {lazy['counters']['uncompressed_bytes']} |",
            ]
        )
    else:
        rows.append(
            "Pass `--run-id` for the manifest-backed full/chunk-backed Engine replay and timing comparison."
        )
    rows.extend(
        [
            "",
            "## GH-601 timing reference (not attributed to GH-602)",
            "",
            "| Mode | Read workload seconds | Full staging seconds | GH-602 matched timing |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for mode, read_seconds in READ_REFERENCE.items():
        execution_label = result.get("execution_probe")
        matched_timing = "not measured by this storage probe"
        if (
            isinstance(execution_label, dict)
            and execution_label.get("mode_label") == mode
        ):
            full = execution_label["full"]
            lazy = execution_label["lazy"]
            assert isinstance(full, dict) and isinstance(lazy, dict)
            matched_timing = (
                f"full {full['seconds']:.3f}s / chunk-backed {lazy['seconds']:.3f}s"
            )
        rows.append(
            f"| {mode} | {read_seconds} | {STAGING_REFERENCE[mode]} | {matched_timing} |"
        )
    rows.extend(
        [
            "",
            "## Compatibility/failure probes",
            "",
            "- Active v2 bounded read: passed for the selected pinned revision.",
            "- Active v1, rollback, missing chunk, and corrupt digest: focused repository tests cover these on disposable databases; the durable benchmark database was not mutated.",
            "- Worker staging/promotion timing is not executed by this benchmark; the end-to-end probe uses the Engine's in-memory sink.",
            "",
            "## Environment and limitations",
            "",
            f"- Python: `{sys.version.split()[0]}`; platform: `{platform.platform()}`; PID RSS peak is process-level `ru_maxrss`.",
            "- SQLite page/read-byte counters are not available from the repository connection without adding tracing hooks; compressed and uncompressed chunk bytes are reported by repository counters.",
            "- Quality gates are not executed by this benchmark; the story Dev Agent Record is the source for test and static-check results.",
            "- Current annual chunks intentionally decode all items in a selected year. A subannual/indexed schema is a follow-up only if this ceiling is material in the matched production workload.",
            "",
        ]
    )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=HISTORICAL_PRICE_CACHE)
    parser.add_argument("--backtest-database", type=Path, default=BACKTEST_DB)
    parser.add_argument(
        "--run-id",
        help="completed 738-security backtest run to replay in full and lazy modes",
    )
    parser.add_argument("--revision")
    parser.add_argument("--through", type=date.fromisoformat)
    parser.add_argument("--limit", type=int, default=254)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be positive")
    revision, start, end, _ = _select_revision(args.database, args.revision)
    through = args.through or end - timedelta(days=1)
    if not start <= through < end:
        parser.error("--through must be inside the selected evidence interval")
    result: dict[str, object] = {
        "command": shlex.join(
            [
                sys.executable,
                "-m",
                "scripts.benchmark_bounded_historical_reads",
                *sys.argv[1:],
            ]
        ),
        "manifest_digest": MANIFEST_DIGEST,
        "storage_probe": _measure(
            HistoricalPriceRepository(lambda: _read_only_connect(args.database)),
            revision,
            through,
            args.limit,
        ),
    }
    if args.run_id is not None:
        result["execution_probe"] = _measure_execution(
            prices=HistoricalPriceRepository(lambda: _read_only_connect(args.database)),
            backtests=BacktestRepository(
                lambda: _read_only_connect(args.backtest_database)
            ),
            manifest_path=args.backtest_database,
            run_id=args.run_id,
        )
    else:
        result["execution_probe"] = None
    output = (
        args.output
        or Path("_bmad-output/implementation-artifacts")
        / f"benchmark-602-{date.today().isoformat()}.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_markdown(result, database=args.database), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
