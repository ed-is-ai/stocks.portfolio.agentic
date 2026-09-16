"""Unit cover for the aggregate evidence census (#636).

Offline and pure: every view is built in memory from records — no
repository, no provider, no artifact file.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, get_args

import pandas as pd

from app.schemas.analysis_artifact import (
    CurrentAnalysisEvidenceV1,
    CurrentEvidenceGapV1,
)
from app.schemas.evidence_census import (
    FAULT_DROPPED,
    FAULT_GAPPED,
    FAULT_MISSING_FRAGMENT,
    FAULT_THIN,
    EvidenceCensusV1,
)
from app.schemas.record import StockRecord
from app.services.backtest.scan_view import (
    PortfolioHistoryRead,
    build_scan_market_view,
)
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
)
from app.services.evidence_census import GAP_REASON_FAULTS, build_evidence_census

SESSION = date(2026, 8, 28)
PREVIOUS = date(2026, 8, 27)


def _bars(*sessions: date) -> list[dict[str, float | int | str]]:
    """Newest-first daily bars (the artifact's convention)."""
    return [
        {
            "date": session.isoformat(),
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": 1000,
        }
        for session in sorted(sessions, reverse=True)
    ]


def _record(ticker: str, bars: list[dict[str, float | int | str]]) -> StockRecord:
    return StockRecord.model_validate(
        {
            "ticker": ticker,
            "as_of": SESSION.isoformat(),
            "price": 10.0,
            "volume": 1000,
            "rel_volume": 1.0,
            "high_52w": 11.0,
            "low_52w": 9.0,
            "pct_from_52w_high": -1.0,
            "pct_change_week": 0.5,
            "ohlcv_history": bars,
        }
    )


GapReason = Literal[
    "insufficient_history",
    "incomplete_history",
    "malformed_history",
    "stale_session",
    "detector_failure",
    "identity_conflict",
]


def _gap(security_id: str, reason: GapReason, detail: str) -> CurrentEvidenceGapV1:
    return CurrentEvidenceGapV1(
        schema_version="current_scan_evidence_gap.v1",
        security_id=security_id,
        as_of_session=SESSION,
        reason=reason,
        detail=detail,
    )


def _strategy(entry: int = 0, exit_: int = 0) -> StrategyEvidenceRequirementsV1:
    return StrategyEvidenceRequirementsV1(
        entry=(
            EvidenceRequirementV1(
                kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=entry
            ),
        ),
        exit=(
            EvidenceRequirementV1(
                kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=exit_
            ),
        ),
    )


def _portfolio_read(ticker: str, sessions: int) -> PortfolioHistoryRead:
    frame = pd.DataFrame(
        {"close": [10.0] * sessions},
        index=pd.Index(
            [date(2026, 1, 1) + pd.Timedelta(days=i) for i in range(sessions)],
            dtype=object,
        ),
    )
    return PortfolioHistoryRead(ticker, ticker, history=frame)


def _assert_accounting(census: EvidenceCensusV1) -> None:
    """Assert the disjoint buckets against independently counted rows."""
    dropped = [
        row for row in census.securities if not row.in_universe and not row.sessions
    ]
    faulted = [row for row in census.securities if row.faults and row not in dropped]
    clean = [
        row
        for row in census.securities
        if not row.faults and row not in dropped and row not in faulted
    ]
    assert census.dropped == len(dropped)
    assert census.faulted == len(faulted)
    assert census.clean == len(clean)
    assert census.clean + census.faulted + census.dropped == census.total


REASON_FAULTS: dict[GapReason, str] = {
    "insufficient_history": FAULT_THIN,
    "incomplete_history": FAULT_GAPPED,
    "malformed_history": FAULT_MISSING_FRAGMENT,
    "detector_failure": FAULT_MISSING_FRAGMENT,
    "stale_session": FAULT_DROPPED,
    "identity_conflict": FAULT_DROPPED,
}


def test_every_gap_reason_maps_to_its_fault_and_is_verbatim() -> None:
    entries = tuple(
        _gap(f"SEC{index}", reason, f"detail for {reason}")
        for index, reason in enumerate(sorted(REASON_FAULTS))
    )
    evidence = CurrentAnalysisEvidenceV1.build(
        run_id="run-gaps", as_of_session=SESSION, entries=entries
    )
    view, _ = build_scan_market_view([], {}, as_of_session=SESSION)
    census = build_evidence_census(view, current_evidence=evidence)

    assert census.total == len(entries)
    rows = {row.security_id: row for row in census.securities}
    for entry in entries:
        row = rows[entry.security_id]
        assert row.gap_reason == entry.reason
        assert row.gap_detail == entry.detail
        assert row.cause == f"{entry.reason}: {entry.detail}"
        assert REASON_FAULTS[entry.reason] in row.faults


def test_overlapping_faults_are_counted_once_in_accounting() -> None:
    evidence = CurrentAnalysisEvidenceV1.build(
        run_id="run-overlap",
        as_of_session=SESSION,
        entries=(_gap("AAA", "incomplete_history", "one missing session"),),
    )
    view, _ = build_scan_market_view(
        [_record("AAA", _bars(PREVIOUS, SESSION))], {}, as_of_session=SESSION
    )
    census = build_evidence_census(
        view,
        current_evidence=evidence,
        strategies={"trend": _strategy(entry=252)},
    )

    (row,) = census.securities
    assert row.faults == (FAULT_THIN, FAULT_GAPPED)
    assert census.total == 1
    assert census.faulted == 1
    _assert_accounting(census)
    assert census.fault_counts == {FAULT_GAPPED: 1, FAULT_THIN: 1}
    assert sum(census.fault_counts.values()) > census.faulted


def test_scan_absent_holding_reports_portfolio_sessions() -> None:
    view, _ = build_scan_market_view(
        [_record("AAA", _bars(SESSION))], {}, as_of_session=SESSION
    )
    census = build_evidence_census(
        view,
        holdings=("BBB",),
        portfolio_reads=(_portfolio_read("BBB", 120),),
    )

    row = {item.security_id: item for item in census.securities}["BBB"]
    assert row.namespace == "portfolio"
    assert row.sessions == 120
    assert FAULT_DROPPED not in row.faults
    assert census.dropped == 0


def test_entry_and_exit_shortfalls_are_independent() -> None:
    view, _ = build_scan_market_view(
        [_record("AAA", _bars(PREVIOUS, SESSION))], {}, as_of_session=SESSION
    )
    census = build_evidence_census(
        view,
        holdings=("AAA",),
        strategies={"trend": _strategy(entry=200, exit_=1)},
    )

    (row,) = census.securities
    assert [shortfall.path for shortfall in row.shortfalls] == ["entry"]
    assert row.shortfalls[0].required == 200
    assert row.shortfalls[0].available == 2


def test_strategy_minimum_above_scan_bar_marks_only_that_strategy() -> None:
    view, _ = build_scan_market_view(
        [_record("AAA", _bars(PREVIOUS, SESSION))], {}, as_of_session=SESSION
    )
    census = build_evidence_census(
        view,
        strategies={"deep": _strategy(entry=252), "shallow": _strategy(entry=1)},
    )

    (row,) = census.securities
    assert row.gap_reason is None
    assert row.faults == (FAULT_THIN,)
    assert [(item.strategy, item.required) for item in row.shortfalls] == [
        ("deep", 252)
    ]


def test_empty_universe_and_no_holdings_is_an_empty_census() -> None:
    view, _ = build_scan_market_view([], {}, as_of_session=SESSION)
    census = build_evidence_census(view)

    assert census.securities == ()
    assert (census.total, census.clean, census.faulted, census.dropped) == (0, 0, 0, 0)
    assert census.fault_counts == {}


def test_holding_in_neither_namespace_is_dropped() -> None:
    view, _ = build_scan_market_view(
        [_record("AAA", _bars(SESSION))], {}, as_of_session=SESSION
    )
    census = build_evidence_census(view, holdings=("ZZZ",))

    row = {item.security_id: item for item in census.securities}["ZZZ"]
    assert row.namespace == "none"
    assert row.sessions == 0
    assert row.faults == (FAULT_DROPPED,)
    assert census.dropped == 1
    _assert_accounting(census)


def test_census_is_deterministic() -> None:
    evidence = CurrentAnalysisEvidenceV1.build(
        run_id="run-det",
        as_of_session=SESSION,
        entries=(_gap("CCC", "stale_session", "session is stale"),),
    )
    view, unresolved = build_scan_market_view(
        [_record("AAA", _bars(SESSION)), _record("BBB", _bars(SESSION))],
        {},
        as_of_session=SESSION,
    )
    kwargs = {
        "current_evidence": evidence,
        "unresolved": unresolved,
        "holdings": ("BBB", "DDD"),
        "portfolio_reads": (_portfolio_read("DDD", 10),),
        "strategies": {"trend": _strategy(entry=5, exit_=5)},
    }
    first = build_evidence_census(view, **kwargs)
    second = build_evidence_census(view, **kwargs)

    assert first == second
    assert [row.security_id for row in first.securities] == ["AAA", "BBB", "CCC", "DDD"]
    _assert_accounting(first)


def test_fault_mapping_covers_every_declared_gap_reason() -> None:
    """The mapping must track the artifact's reason vocabulary exactly."""
    annotation = CurrentEvidenceGapV1.model_fields["reason"].annotation
    assert annotation is not None
    declared = set(get_args(annotation))

    assert set(GAP_REASON_FAULTS) == declared
    assert set(REASON_FAULTS) == declared


def test_unknown_gap_reason_is_reported_not_raised() -> None:
    """A widened upstream vocabulary must not abort the census."""
    gap = CurrentEvidenceGapV1.model_construct(
        schema_version="current_scan_evidence_gap.v1",
        security_id="ZZZ",
        as_of_session=SESSION,
        reason="newly_invented_reason",
        detail="from a newer artifact",
    )
    evidence = CurrentAnalysisEvidenceV1.model_construct(
        schema_version="current_scan_evidence.v1",
        run_id="run-unknown",
        as_of_session=SESSION,
        entries=(gap,),
    )
    view, _ = build_scan_market_view([], {}, as_of_session=SESSION)
    census = build_evidence_census(view, current_evidence=evidence)

    row = next(row for row in census.securities if row.security_id == "ZZZ")
    assert row.gap_reason == "newly_invented_reason"
    assert row.faults
    _assert_accounting(census)


def test_unresolved_security_is_accounted_even_when_only_unresolved() -> None:
    """A canonicalisation collision is a row, not a silent omission."""
    view, _ = build_scan_market_view(
        [_record("AAA", _bars(SESSION))], {}, as_of_session=SESSION
    )
    census = build_evidence_census(view, unresolved=("GHOST",))

    ids = [row.security_id for row in census.securities]
    assert "GHOST" in ids
    row = next(row for row in census.securities if row.security_id == "GHOST")
    assert row.cause is not None and "unresolved" in row.cause
    assert not row.in_universe
    _assert_accounting(census)


def test_census_touches_no_repository_provider_or_clock(monkeypatch) -> None:
    """AC7: the aggregation is pure — prove it, do not merely avoid it."""
    import app.services.evidence_census as module

    def _fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("the census must not read the clock")

    monkeypatch.setattr(module, "date", _fail, raising=False)
    view, unresolved = build_scan_market_view(
        [_record("AAA", _bars(SESSION)), _record("BBB", _bars(SESSION))],
        {},
        as_of_session=SESSION,
    )
    census = build_evidence_census(
        view,
        unresolved=unresolved,
        holdings=("BBB",),
        strategies={"trend": _strategy(entry=1, exit_=1)},
    )

    assert census.as_of_session == SESSION
    _assert_accounting(census)
