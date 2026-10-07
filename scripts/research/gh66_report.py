"""Validate and publish the frozen GH #66 run artifacts without rerunning them."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.research.gh66_experiment as gh  # noqa: E402

SEAL_NAME = "results-seal.json"
RESULTS_NAME = "results-seal.json"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _expected_arms() -> list[tuple[str, str, int | None]]:
    return [
        (strategy, variant, seed)
        for strategy in gh.STRATEGIES
        for variant, seed in (
            [("legacy", None), ("proposed", None)]
            + [("random", item) for item in gh.SEEDS]
        )
    ]


def _expected_path(output: Path, strategy: str, variant: str, seed: int | None) -> Path:
    return output / "runs" / f"{gh._arm_name(strategy, variant, seed)}.json"


def _validate_curve(result: dict[str, Any], fixed_inputs: dict[str, Any]) -> None:
    if result.get("status") != "completed":
        return
    curve = result.get("equity_curve")
    metrics = result.get("metrics")
    if not isinstance(curve, list) or not curve or not isinstance(metrics, dict):
        raise RuntimeError("completed result has no equity curve or metrics")
    sessions = [point.get("session") for point in curve]
    if any(not isinstance(session, str) for session in sessions):
        raise RuntimeError("equity curve contains a missing session")
    if sessions != sorted(set(sessions)):
        raise RuntimeError("equity curve sessions are duplicated or out of order")
    if not sessions[0].startswith(fixed_inputs["start_month"]):
        raise RuntimeError("equity curve does not start in the frozen horizon")
    if not sessions[-1].startswith(fixed_inputs["end_month"]):
        raise RuntimeError("equity curve does not end in the frozen horizon")
    for key, point_key in (
        ("start_value", "total_equity_base"),
        ("end_value", "total_equity_base"),
    ):
        point = curve[0] if key == "start_value" else curve[-1]
        if str(metrics.get(key)) != str(point.get(point_key)):
            raise RuntimeError(f"{key} differs from the saved equity curve")
    event_digest = result.get("event_sha256")
    if not isinstance(event_digest, str) or not HEX_SHA256.fullmatch(event_digest):
        raise RuntimeError("completed result has no valid event digest")
    expected_result_digest = gh._sha256({"events": event_digest, "equity_curve": curve})
    if result.get("simulation_result_sha256") != expected_result_digest:
        raise RuntimeError("completed result digest does not match its equity curve")
    for key in ("total_return", "cagr", "max_drawdown", "sharpe_ratio"):
        value = metrics.get(key)
        if not isinstance(value, (float, int)) or not math.isfinite(value):
            raise RuntimeError(f"completed result has invalid {key}")


def _load_matrix(output: Path, frozen: dict[str, Any]) -> list[dict[str, Any]]:
    expected = _expected_arms()
    expected_paths = {
        _expected_path(output, strategy, variant, seed)
        for strategy, variant, seed in expected
    }
    actual_paths = set((output / "runs").glob("*.json"))
    extras = sorted(path.name for path in actual_paths - expected_paths)
    if extras:
        raise RuntimeError(f"unexpected run files in the matrix: {', '.join(extras)}")

    results = []
    for strategy, variant, seed in expected:
        path = _expected_path(output, strategy, variant, seed)
        if path.exists():
            result = _read_json(path)
            gh._validate_existing_result(
                result,
                frozen,
                strategy_id=strategy,
                variant=variant,
                seed=seed,
            )
            _validate_curve(result, frozen["fixed_inputs"])
        else:
            result = {
                "schema": gh.RESULT_SCHEMA,
                "status": "missing",
                **gh._expected_run_identity(
                    frozen, strategy, variant, seed, None, None
                ),
            }
        results.append(result)
    if len(results) != 42:
        raise RuntimeError(f"expected 42 matrix arms; found {len(results)}")
    return results


def _load_spy_benchmark(
    output: Path, frozen: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    attempt_path = output / "spy_benchmark.json"
    attempt = _read_json(attempt_path) if attempt_path.exists() else None
    provenance: dict[str, Any] = {
        "initial_attempt": None,
        "selected_result": None,
    }
    if attempt is not None:
        excerpt = attempt.get("failure_detail") or attempt.get("failure_excerpt")
        lines = [] if not excerpt else str(excerpt).splitlines()
        validation_line = next(
            (index for index, line in enumerate(lines) if "input_type=list" in line),
            None,
        )
        failure_summary = (
            f"{lines[validation_line - 1].strip()}: {lines[validation_line].strip()}"
            if validation_line is not None and validation_line > 0
            else next((line.strip() for line in reversed(lines) if line.strip()), None)
        )
        provenance["initial_attempt"] = {
            "status": attempt.get("status"),
            "failure_type": attempt.get("failure_type")
            or ("ValidationError" if excerpt else None),
            "failure_detail": failure_summary,
        }
        if attempt.get("status") == "completed":
            gh._validate_existing_result(
                attempt, frozen, strategy_id="SPY", variant="legacy", seed=None
            )
            _validate_curve(attempt, frozen["fixed_inputs"])
            provenance["selected_result"] = "spy_benchmark.json"
            return attempt, provenance

    adapter_path = output / "spy_benchmark_adapter_manifest.json"
    result_path = output / "supplemental-spy-benchmark.json"
    if not adapter_path.exists() or not result_path.exists():
        return None, provenance
    adapter = _read_json(adapter_path)
    script_path = output / "spy_benchmark_adapter.py"
    actual_script_digest = _digest(script_path.read_bytes())
    if adapter.get("parent_experiment_id") != frozen["experiment_id"]:
        raise RuntimeError("supplemental SPY adapter belongs to another experiment")
    if adapter.get("adapter_sha256") != actual_script_digest:
        raise RuntimeError("supplemental SPY adapter source digest does not match")
    benchmark = _read_json(result_path)
    gh._validate_existing_result(
        benchmark, frozen, strategy_id="SPY", variant="legacy", seed=None
    )
    _validate_curve(benchmark, frozen["fixed_inputs"])
    if benchmark.get("status") != "completed":
        return None, provenance
    benchmark = dict(benchmark)
    benchmark["adapter_id"] = adapter.get("adapter_id")
    benchmark["adapter_sha256"] = actual_script_digest
    provenance["selected_result"] = "supplemental-spy-benchmark.json"
    provenance["adapter_id"] = adapter.get("adapter_id")
    provenance["adapter_sha256"] = actual_script_digest
    return benchmark, provenance


def _load_single_instrument_runs(
    output: Path, frozen: dict[str, Any]
) -> list[dict[str, Any]]:
    result_dir = output / "single-instrument"
    setup = _read_json(result_dir / "evidence-setup.json")
    _external_inputs(output)
    results = []
    missing = []
    for symbol in ("spy", "qqq"):
        for cap in (10, 1):
            name = f"{symbol}-cap{cap}"
            manifest_path = result_dir / f"{name}-manifest.json"
            result_path = result_dir / f"{name}-result.json"
            if not manifest_path.exists() or not result_path.exists():
                missing.append(name)
                continue
            derived = gh._read_frozen_manifest(manifest_path)
            if (
                derived.get("single_instrument_scope", {}).get("parent_experiment_id")
                != frozen["experiment_id"]
            ):
                raise RuntimeError(f"{name} is not derived from the frozen experiment")
            scope = derived["single_instrument_scope"]
            source = setup[symbol]
            expected_revision = source.get(
                "price_revision", source.get("data_revision")
            )
            if (
                scope.get("security_id") != source["security_id"]
                or scope.get("price_revision") != expected_revision
                or scope.get("fx_revision") != setup["fx"]["price_revision"]
            ):
                raise RuntimeError(f"{name} evidence does not match its source pin")
            result = _read_json(result_path)
            gh._validate_existing_result(
                result,
                derived,
                strategy_id="rtly-backtest-moving-average",
                variant="proposed",
                seed=None,
            )
            _validate_curve(result, frozen["fixed_inputs"])
            result = dict(result)
            result["instrument"] = symbol.upper()
            result["position_cap"] = cap
            results.append(result)
    if missing or len(results) != 4:
        raise RuntimeError(
            "single-instrument report requires all four SPY/QQQ cap runs; "
            f"missing: {', '.join(missing) if missing else 'unknown'}"
        )
    return results


def _external_inputs(output: Path) -> list[dict[str, str]]:
    setup = _read_json(output / "single-instrument" / "evidence-setup.json")
    cache = Path(setup["historical_cache"])
    expected = setup.get("historical_cache_sha256")
    if not cache.is_file() or not isinstance(expected, str):
        raise RuntimeError("pinned single-instrument evidence cache is unavailable")
    actual = _digest(cache.read_bytes())
    if actual != expected:
        raise RuntimeError("pinned single-instrument evidence cache digest changed")
    return [
        {
            "role": "single_instrument_historical_cache",
            "path": str(cache),
            "sha256": actual,
        }
    ]


def _verify_execution_source_snapshot(
    output: Path, frozen: dict[str, Any]
) -> dict[str, str]:
    snapshots = {}
    for relative, expected in frozen["research_code_sha256"].items():
        snapshot = output / "execution-source" / Path(relative).name
        if not snapshot.is_file() or _digest(snapshot.read_bytes()) != expected:
            raise RuntimeError(f"frozen execution source snapshot mismatch: {relative}")
        snapshots[relative] = snapshot.resolve().relative_to(ROOT).as_posix()
    return snapshots


def _seal_paths(output: Path) -> list[Path]:
    paths = [
        output / "experiment_manifest.json",
        output / "matrix-progress.json",
        output / "short-benchmark.json",
        output / "full-repeat-control.json",
        output / "spy_benchmark.json",
        output / "supplemental-spy-benchmark.json",
        output / "spy_benchmark_adapter.py",
        output / "spy_benchmark_adapter_manifest.json",
        output / "original-results-before.json",
        output / "original-results-preservation.json",
        output / "single-instrument" / "evidence-setup.json",
        ROOT / "scripts/research/gh66_experiment.py",
        ROOT / "scripts/research/gh66_variants.py",
        ROOT / "docs/research/gh66/single_instrument_ma_runner.py",
    ]
    paths.extend(sorted((output / "runs").glob("*.json")))
    paths.extend(sorted((output / "short-replays").glob("*.json")))
    paths.extend(sorted((output / "single-instrument").glob("*-manifest.json")))
    paths.extend(sorted((output / "single-instrument").glob("*-result.json")))
    paths.extend(sorted((output / "execution-source").glob("*.py")))
    paths.extend(sorted((output / "full-repeat").rglob("*.json")))
    return sorted({path.resolve() for path in paths if path.is_file()})


def _seal_payload(output: Path, frozen: dict[str, Any]) -> dict[str, Any]:
    _verify_execution_source_snapshot(output, frozen)
    external_inputs = _external_inputs(output)
    entries = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": _digest(path.read_bytes()),
        }
        for path in _seal_paths(output)
    ]
    if not any(
        entry["path"].endswith("/runs/rtly-backtest-moving-average--proposed.json")
        for entry in entries
    ):
        raise RuntimeError("seal is missing the representative proposed result")
    return {
        "schema": "gh66_results_seal.v1",
        "experiment_id": frozen["experiment_id"],
        "created_at": datetime.now(UTC).isoformat(),
        "report_source_sha256": _digest(Path(__file__).read_bytes()),
        "files": entries,
        "external_inputs": external_inputs,
    }


def _verify_seal(output: Path, frozen: dict[str, Any]) -> dict[str, Any]:
    seal_path = output / SEAL_NAME
    if not seal_path.exists():
        raise RuntimeError("results-seal.json is required before report generation")
    seal = _read_json(seal_path)
    if (
        seal.get("schema") != "gh66_results_seal.v1"
        or seal.get("experiment_id") != frozen["experiment_id"]
    ):
        raise RuntimeError("results seal belongs to another experiment")
    if seal.get("report_source_sha256") != _digest(Path(__file__).read_bytes()):
        raise RuntimeError("report source changed after the results were sealed")
    _verify_execution_source_snapshot(output, frozen)
    if seal.get("external_inputs") != _external_inputs(output):
        raise RuntimeError("external evidence changed after the results were sealed")
    recorded = {item["path"]: item["sha256"] for item in seal.get("files", [])}
    actual_paths = {
        path.relative_to(ROOT).as_posix(): path for path in _seal_paths(output)
    }
    if set(recorded) != set(actual_paths):
        raise RuntimeError("sealed result file inventory changed")
    changed = [
        path
        for path, digest in recorded.items()
        if _digest(actual_paths[path].read_bytes()) != digest
    ]
    if changed:
        raise RuntimeError("sealed files changed: " + ", ".join(sorted(changed)))
    return seal


def _write_single_instrument_html(path: Path, runs: list[dict[str, Any]]) -> None:
    rows = []
    charts = []
    for symbol in ("SPY", "QQQ"):
        symbol_runs = [run for run in runs if run.get("instrument") == symbol]
        if not symbol_runs:
            continue
        chart_runs = [
            {
                **run,
                "strategy_id": f"Moving Average / {symbol}",
                "variant": f"cap {run['position_cap']}",
                "seed": None,
            }
            for run in symbol_runs
        ]
        charts.append(gh._svg_panel(f"Moving Average · {symbol}", chart_runs, None))
        for run in sorted(
            symbol_runs, key=lambda item: item["position_cap"], reverse=True
        ):
            metrics = run["metrics"]
            rows.append(
                "<tr>"
                + "".join(
                    f"<td>{html.escape(str(value))}</td>"
                    for value in (
                        symbol,
                        run["position_cap"],
                        f"£{float(metrics['end_value']):,.2f}",
                        gh._percent(metrics["total_return"]),
                        gh._percent(metrics["cagr"]),
                        gh._percent(metrics["max_drawdown"]),
                        gh._number(metrics["sharpe_ratio"]),
                        gh._percent(metrics["mean_invested_exposure_pct"] / 100),
                        gh._percent(metrics["time_invested_pct"] / 100),
                        metrics["exit_count"],
                    )
                )
                + "</tr>"
            )
    if not rows:
        raise RuntimeError("no completed single-instrument Moving Average runs found")
    document = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Moving Average on SPY and QQQ</title>
<style>
body{{margin:0;background:#101519;color:#e6e3d7;font:15px/1.5 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:30px}}h1,h2{{font-family:Georgia,serif;font-weight:500}}p{{color:#bbc3c5}}section{{margin:24px 0;background:#1b2428;padding:14px}}svg{{width:100%;height:auto}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #354044;padding:9px;text-align:left;white-space:nowrap}}th{{color:#c59c74;position:sticky;top:0;background:#101519}}.note{{border-left:3px solid #e0a65e;padding-left:14px}}
</style><main><h1>Moving Average on SPY and QQQ</h1>
<p>2016-09-01 to 2026-08-31 · £10,000 starting capital · GBP valuation · pinned price, corporate-action and FX evidence</p>
<p class="note">Cap 10 preserves the original position limit and its per-slot sizing. Cap 1 is a sensitivity run that lets this single-symbol strategy use one full position slot. Both apply the Moving Average Skill and 200-session downtrend gate. SPY uses its own price history for the gate; QQQ uses the separately pinned SPY reference.</p>
{"".join(charts)}
<h2>Metrics</h2><div class="scroll"><table><thead><tr><th>ETF</th><th>Position cap</th><th>Ending value</th><th>Total return</th><th>CAGR</th><th>Max drawdown</th><th>Sharpe</th><th>Mean exposure</th><th>Time invested</th><th>Exits</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
<h2>Method and limits</h2><p>Dividend cash is credited without reinvestment. Results use the engine's existing gross convention, with no commission, spread or slippage. The QQQ history came from the pinned Yahoo Finance evidence adapter. Its data revision and provider response digest are recorded in the adjacent manifest and results seal. No passive QQQ buy-and-hold run is included in this comparison.</p>
<p>The cap 10 runs average 6.5% invested exposure for SPY and 2.2% for QQQ; the strategy entered only four SPY candidate batches and two QQQ batches. The cap 1 runs average 58.8% and 22.3% invested exposure respectively. These results describe this fixed, previously inspected decade.</p>
</main></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document)


def publish_report(manifest_path: Path, output: Path, *, create_seal: bool) -> None:
    frozen = gh._read_frozen_manifest(manifest_path)
    execution_source_snapshots = _verify_execution_source_snapshot(output, frozen)
    results = _load_matrix(output, frozen)
    benchmark, benchmark_provenance = _load_spy_benchmark(output, frozen)
    single_runs = _load_single_instrument_runs(output, frozen)
    if create_seal:
        seal_path = output / SEAL_NAME
        if seal_path.exists():
            raise FileExistsError(f"refusing to replace {seal_path}")
        seal = _seal_payload(output, frozen)
        gh._atomic_json(seal_path, seal)
    else:
        seal = _verify_seal(output, frozen)

    short_path = output / "short-benchmark.json"
    repeat_path = output / "full-repeat-control.json"
    preservation_path = output / "original-results-preservation.json"
    short = _read_json(short_path) if short_path.exists() else None
    repeat = _read_json(repeat_path) if repeat_path.exists() else None
    preservation = _read_json(preservation_path) if preservation_path.exists() else None
    summary = gh._summary(results, benchmark, short, repeat, preservation)
    summary["matrix_accounting"] = {
        "planned": len(results),
        "completed": sum(item.get("status") == "completed" for item in results),
        "failed": sum(item.get("status") == "failed" for item in results),
        "missing": sum(item.get("status") == "missing" for item in results),
    }
    summary["spy_benchmark_provenance"] = benchmark_provenance
    summary["execution_source_snapshots"] = execution_source_snapshots
    summary["single_instrument_external_evidence"] = _external_inputs(output)
    summary["single_instrument_moving_average"] = [
        {
            "instrument": run["instrument"],
            "position_cap": run["position_cap"],
            "result_path": f"single-instrument/{run['instrument'].lower()}-cap{run['position_cap']}-result.json",
            "metrics": run["metrics"],
        }
        for run in single_runs
    ]
    summary["results_seal"] = {
        "path": SEAL_NAME,
        "verified": True,
        "files": len(seal["files"]),
    }
    gh._atomic_json(output / "summary.json", summary)
    curves = [*results, *([benchmark] if benchmark else [])]
    gh._write_curve_csv(output / "equity-curves.csv", curves)
    gh._write_html_report(
        output / "report.html",
        frozen,
        results,
        summary,
        benchmark,
        short,
        repeat,
        preservation,
    )
    html_path = output / "report.html"
    report_html = html_path.read_text()
    completed = summary["matrix_accounting"]["completed"]
    failed = summary["matrix_accounting"]["failed"]
    missing = summary["matrix_accounting"]["missing"]
    accounting = (
        f"<p>{len(results)} expected arms: {completed} completed, "
        f"{failed} failed, {missing} missing.</p>"
    )
    report_html = report_html.replace(
        f"<p>{len(results)} planned arms have records; missing and failed arms remain visible.</p>",
        accounting,
    )
    report_html = report_html.replace(
        "Per-run JSON records include candidate-audit row counts and digests, priority/explanation coverage, contested/full-book outcomes, serialized audit payload bytes, isolated SQLite page allocation, runtime, and process peak RSS in bytes.",
        "Per-run JSON records include candidate-audit row counts and digests, priority/explanation coverage, contested/full-book outcomes, serialized audit payload bytes, isolated SQLite page allocation, runtime, and the shared process lifetime RSS high-water mark. That RSS is group-level, not an independent per-arm peak.",
    )
    if repeat is not None and repeat.get("status") == "incomplete":
        incomplete_note = (
            "Full-horizon duplicate incomplete after "
            f"{repeat.get('completed_sessions')} of {repeat.get('planned_sessions')} "
            "sessions; last progress "
            f"{repeat.get('last_progress_session')}; full-run equality is unconfirmed."
        )
        report_html = report_html.replace(
            "Full Moving Average proposed repeat deterministic=None.",
            html.escape(incomplete_note),
        )
    if benchmark_provenance.get("initial_attempt", {}).get("status") == "failed":
        attempt = benchmark_provenance["initial_attempt"]
        note = (
            "<p>The first SPY benchmark attempt failed and is retained as "
            "<code>spy_benchmark.json</code> ("
            + html.escape(str(attempt.get("failure_type")))
            + ": "
            + html.escape(str(attempt.get("failure_detail")))
            + "). The report uses the separately validated supplemental benchmark "
            "in <code>supplemental-spy-benchmark.json</code>.</p>"
        )
        report_html = report_html.replace(
            "<h2>SPY benchmark</h2>", "<h2>SPY benchmark</h2>" + note
        )
    if preservation is not None and not all(
        preservation.get("main_file_signatures_unchanged", {}).values()
    ):
        note = (
            '<p class="warning">Source database file signatures changed during '
            "the research window. The experiment used read-only database connections; "
            "the saved run identities and original result payload hashes still match. "
            "The available evidence does not attribute the file changes.</p>"
        )
        report_html = report_html.replace(
            "<h2>Reproducibility</h2>", note + "<h2>Reproducibility</h2>"
        )
    source_note = (
        "<p>The exact execution-source snapshots in <code>execution-source/</code> "
        "match the frozen source digests. The current runner rejects unregistered "
        "horizons. The QQQ evidence cache is local temporary storage; its path and "
        "SHA-256 are in the summary and results seal, but the cache is not committed.</p>"
    )
    report_html = report_html.replace(
        "<h2>Reproducibility</h2>", source_note + "<h2>Reproducibility</h2>"
    )
    html_path.write_text(report_html)
    _write_single_instrument_html(
        output / "single-instrument" / "report.html", single_runs
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("seal", "report"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    publish_report(args.manifest, args.output, create_seal=args.action == "seal")
    print(f"{args.action.upper()} complete: {args.output}", flush=True)


if __name__ == "__main__":
    main()
