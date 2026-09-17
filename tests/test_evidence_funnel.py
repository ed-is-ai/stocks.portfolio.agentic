"""Reconciliation tests for the evidence funnel (#635).

Offline: the artifact and the run log row are built in memory — no network,
no provider call, no published file.
"""

from __future__ import annotations

from datetime import date
import json

import pandas as pd

from app.agents.scanner.scanner_agent import _build_current_evidence
from app.schemas.analysis_artifact import (
    CurrentAnalysisEvidenceV1,
    CurrentEvidenceGapV1,
)
from app.schemas.source_health import SourceHealth, SourceName, SourceState
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.evidence_funnel import (
    build_evidence_funnel,
    find_run_log_row,
    parse_run_log_source_health,
)

RUN_ID = "run-2026-09-15"
SESSION = date(2026, 9, 15)


def _health(
    source: SourceName,
    count: int,
    *,
    detail_code: str = "",
    display_message: str = "",
) -> dict[str, object]:
    return SourceHealth(
        source=source,
        state=SourceState.OK if count else SourceState.EMPTY,
        count=count,
        detail_code=detail_code,
        display_message=display_message,
    ).model_dump(mode="json")


def _row(*health: dict[str, object], run_id: str = RUN_ID) -> dict[str, str]:
    payload = {str(item["source"]): item for item in health}
    return {
        "run_id": run_id,
        "scanned": "10",
        "analysed": "8",
        "source_health_json": json.dumps(payload),
    }


def _gap_evidence(*security_ids: str) -> CurrentAnalysisEvidenceV1:
    return CurrentAnalysisEvidenceV1.build(
        run_id=RUN_ID,
        as_of_session=SESSION,
        entries=tuple(
            CurrentEvidenceGapV1(
                schema_version="current_scan_evidence_gap.v1",
                security_id=security_id,
                reason="insufficient_history",
                detail="10 completed sessions; 252 required",
            )
            for security_id in security_ids
        ),
    )


def _clean_row() -> dict[str, str]:
    return _row(
        _health(SourceName.WHALE_WISDOM, 6),
        _health(SourceName.VCP_FMP, 4),
        _health(
            SourceName.YAHOO_MARKET_DATA,
            2,
            display_message="Yahoo market data completed successfully.",
        ),
    )


def test_clean_run_reconciles_with_no_unexplained_loss() -> None:
    funnel = build_evidence_funnel(_gap_evidence("AAA", "BBB"), _clean_row(), 0)

    assert funnel.available is True
    assert funnel.run_id == RUN_ID
    assert (funnel.requested, funnel.fetched, funnel.fetch_failures) == (2, 2, 0)
    assert funnel.artifact_entries == 2
    assert funnel.unexplained == 0
    assert funnel.universe_shortfall == 0
    assert [(item.source, item.count) for item in funnel.discovery] == [
        (SourceName.VCP_FMP, 4),
        (SourceName.WHALE_WISDOM, 6),
    ]


def test_fetch_failures_are_visible_rather_than_absorbed() -> None:
    row = _row(
        _health(
            SourceName.YAHOO_MARKET_DATA,
            8,
            detail_code="partial_ticker_failures",
            display_message="2 ticker request(s) failed; 8 succeeded.",
        ),
    )
    funnel = build_evidence_funnel(_gap_evidence(*"ABCDEFGH"), row, 8)

    assert (funnel.requested, funnel.fetched, funnel.fetch_failures) == (10, 8, 2)
    assert funnel.unexplained == 0


def test_missing_run_log_row_reports_unavailable_without_inferring() -> None:
    funnel = build_evidence_funnel(_gap_evidence("AAA"), None, 1)

    assert funnel.available is False
    assert funnel.run_id == RUN_ID
    assert funnel.discovery == ()
    assert funnel.fetched is None
    assert funnel.artifact_entries is None
    assert funnel.unexplained is None
    assert funnel.universe_shortfall is None


def test_source_order_does_not_change_any_figure() -> None:
    evidence = _gap_evidence("AAA", "BBB")
    row = _clean_row()
    payload = json.loads(row["source_health_json"])
    reordered = dict(row)
    reordered["source_health_json"] = json.dumps(dict(reversed(list(payload.items()))))

    assert build_evidence_funnel(evidence, row, 2) == build_evidence_funnel(
        evidence, reordered, 2
    )


def test_funnel_built_twice_from_the_same_row_compares_equal() -> None:
    evidence = _gap_evidence("AAA", "BBB")
    row = _clean_row()

    assert build_evidence_funnel(evidence, row, 2) == build_evidence_funnel(
        evidence, row, 2
    )


def test_unparsable_failure_count_degrades_to_unknown_not_zero() -> None:
    row = _row(
        _health(
            SourceName.YAHOO_MARKET_DATA,
            8,
            detail_code="partial_ticker_failures",
            display_message="some ticker requests failed.",
        ),
    )
    funnel = build_evidence_funnel(_gap_evidence("AAA"), row, 1)

    assert funnel.available is True
    assert funnel.fetched == 8
    assert funnel.fetch_failures is None
    assert funnel.requested is None


def test_malformed_source_health_json_still_yields_a_funnel() -> None:
    row = dict(_clean_row(), source_health_json="not json at all")
    funnel = build_evidence_funnel(_gap_evidence("AAA"), row, 1)

    assert parse_run_log_source_health(row) == []
    assert parse_run_log_source_health({"source_health_json": "[1, 2]"}) == []
    assert funnel.available is True
    assert funnel.discovery == ()
    assert funnel.fetched is None
    assert funnel.artifact_entries == 1


def test_missing_artifact_evidence_keeps_run_log_stages() -> None:
    funnel = build_evidence_funnel(None, _clean_row(), 2)

    assert funnel.available is True
    assert funnel.run_id == RUN_ID
    assert funnel.fetched == 2
    assert funnel.artifact_entries is None
    assert funnel.successes is None
    assert funnel.gaps is None
    assert funnel.unexplained is None


def test_successes_and_gaps_split_the_artifact_entries() -> None:
    sessions = tuple(
        TradingCalendar()._calendar("XNAS").sessions_window(pd.Timestamp(SESSION), -252)
    )
    frame = pd.DataFrame(
        {
            "open": [100.0 + index / 10 for index in range(len(sessions))],
            "high": [101.0 + index / 10 for index in range(len(sessions))],
            "low": [99.0 + index / 10 for index in range(len(sessions))],
            "close": [100.5 + index / 10 for index in range(len(sessions))],
            "volume": [1000.0 + index for index in range(len(sessions))],
        },
        index=pd.DatetimeIndex(sessions),
    )
    success = _build_current_evidence("AAA", frame)
    evidence = CurrentAnalysisEvidenceV1.build(
        run_id=RUN_ID,
        as_of_session=SESSION,
        entries=(success, *_gap_evidence("BBB").entries),
    )
    funnel = build_evidence_funnel(evidence, _clean_row(), 1)

    assert (funnel.artifact_entries, funnel.successes, funnel.gaps) == (2, 1, 1)
    assert funnel.unexplained == 0
    assert funnel.universe_shortfall == 0


def test_find_run_log_row_matches_on_run_id(tmp_path) -> None:
    path = tmp_path / "pipeline_runs.csv"
    path.write_text(
        "run_id,scanned\nrun-a,1\nrun-b,2\n",
        encoding="utf-8",
    )

    assert find_run_log_row("run-b", path) == {"run_id": "run-b", "scanned": "2"}
    assert find_run_log_row("run-c", path) is None
    assert find_run_log_row("run-a", tmp_path / "absent.csv") is None


def test_run_id_mismatch_is_unavailable_not_confidently_wrong() -> None:
    """The join is on run_id: a foreign row must never be reported as this run."""
    funnel = build_evidence_funnel(_gap_evidence("AAA"), _row(run_id="some-other-run"))

    assert funnel.available is False
    assert funnel.run_id == RUN_ID
    assert funnel.requested is None and funnel.unexplained is None


def test_legacy_row_without_run_id_does_not_join_open() -> None:
    """Older logs predate the run_id column; an absent id must not fail open."""
    for missing in ("", "   "):
        funnel = build_evidence_funnel(_gap_evidence("AAA"), _row(run_id=missing))

        assert funnel.available is False, missing
        assert funnel.unexplained is None, missing


def test_unexplained_is_a_signed_residual_not_an_assertion() -> None:
    """A fetched/entry disagreement must surface rather than reconcile itself."""
    row = _row(
        _health(
            SourceName.YAHOO_MARKET_DATA,
            5,
            display_message="Yahoo market data completed successfully.",
        )
    )
    funnel = build_evidence_funnel(_gap_evidence("AAA", "BBB"), row)

    assert funnel.fetched == 5
    assert funnel.artifact_entries == 2
    assert funnel.unexplained == 3

    negative = build_evidence_funnel(_gap_evidence(*"ABCDEFG"), row)
    assert negative.unexplained == -2


def test_one_malformed_health_entry_does_not_discard_the_others() -> None:
    """A market-data loss beside a bad entry must still reach the funnel."""
    payload = {
        "broken": {"source": "not_a_real_source", "state": "OK"},
        "yahoo_market_data": _health(
            SourceName.YAHOO_MARKET_DATA,
            8,
            detail_code="partial_ticker_failures",
            display_message="2 ticker request(s) failed; 8 succeeded.",
        ),
    }
    row = {"run_id": RUN_ID, "source_health_json": json.dumps(payload)}
    funnel = build_evidence_funnel(_gap_evidence("AAA"), row)

    assert funnel.fetched == 8
    assert funnel.fetch_failures == 2
    assert funnel.requested == 10


def test_duplicate_source_entries_do_not_double_count() -> None:
    """A source repeated under two payload keys is counted once."""
    entry = _health(SourceName.WHALE_WISDOM, 6)
    payload = {"a": entry, "b": _health(SourceName.WHALE_WISDOM, 99)}
    row = {"run_id": RUN_ID, "source_health_json": json.dumps(payload)}

    health = parse_run_log_source_health(row)
    assert len(health) == 1
    assert health[0].count == 6


def test_failure_count_regex_tracks_the_scanner_message_format() -> None:
    """``requested`` is recovered from the scanner's own wording — pin it."""
    from app.services.evidence_funnel import _FAILURE_COUNT

    yahoo_failures, succeeded = 2, 8
    scanner_message = (
        f"{yahoo_failures} ticker request(s) failed; {succeeded} succeeded."
    )
    match = _FAILURE_COUNT.match(scanner_message)

    assert match is not None, "scanner_agent reworded its message; update the regex"
    assert int(match.group(1)) == yahoo_failures


def test_find_run_log_row_resolves_its_path_at_call_time(tmp_path) -> None:
    """The default must not bind the configured path at import."""
    log = tmp_path / "runs.csv"
    log.write_text("run_id,source_health_json\nrun-a,{}\nrun-a,{}\n")

    assert find_run_log_row("run-a", log) is not None
    assert find_run_log_row("absent", log) is None
    assert find_run_log_row("run-a", tmp_path / "nope.csv") is None


def test_unrecorded_closes_the_balance_equation() -> None:
    """requested - unrecorded == artifact_entries, and entries - gaps == successes."""
    row = _row(
        _health(
            SourceName.YAHOO_MARKET_DATA,
            2,
            detail_code="partial_ticker_failures",
            display_message="3 ticker request(s) failed",
        )
    )
    funnel = build_evidence_funnel(_gap_evidence("AAA", "BBB"), row, 0)

    assert funnel.requested == 5 and funnel.artifact_entries == 2
    assert funnel.unrecorded == 3
    assert funnel.requested - funnel.unrecorded == funnel.artifact_entries
    assert funnel.artifact_entries - funnel.gaps == funnel.successes


def test_unrecorded_stays_unknown_when_requested_is_unknown() -> None:
    """An unknown stage must not read as a zero exclusion."""
    row = _row(
        _health(
            SourceName.YAHOO_MARKET_DATA,
            2,
            detail_code="ticker_failures",
            display_message="the count did not survive",
        )
    )
    funnel = build_evidence_funnel(_gap_evidence("AAA"), row, 0)

    assert funnel.requested is None and funnel.unrecorded is None
