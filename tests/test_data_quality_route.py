"""Route and composition cover for the Data Quality tab (#639).

Offline: every view is composed in memory from records, so no artifact
file, repository, provider or Strategy runtime is touched. Only the
"unreadable input" row exercises the fail-soft loader itself.
"""

from __future__ import annotations

from datetime import date
import re

from fastapi.testclient import TestClient

from app.api.app import app
import app.api.routes.views as views_module
from app.schemas.analysis_artifact import (
    CurrentAnalysisEvidenceV1,
    CurrentEvidenceGapV1,
)
from app.schemas.market_regime import MarketRegimeSnapshotV1
from app.schemas.record import StockRecord
from app.services.backtest.scan_view import build_scan_market_view
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
)
from app.services.evidence_shared_inputs import StrategyImpactInputV1
import app.services.evidence_quality as evidence_quality
from app.services.evidence_quality import (
    DataQualityViewV1,
    build_data_quality_view,
    load_data_quality_view,
)

client = TestClient(app)

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


def _record(ticker: str, *sessions: date) -> StockRecord:
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
            "ohlcv_history": _bars(*sessions),
        }
    )


def _evidence(*gaps: CurrentEvidenceGapV1) -> CurrentAnalysisEvidenceV1:
    entries = tuple(gaps)
    return CurrentAnalysisEvidenceV1(
        schema_version="current_analysis_evidence.v1",
        run_id="run-1",
        as_of_session=SESSION,
        entries=entries,
        content_digest=CurrentAnalysisEvidenceV1._digest_payload(
            "run-1", SESSION, entries
        ),
    )


def _gap(security_id: str, reason: str, detail: str) -> CurrentEvidenceGapV1:
    return CurrentEvidenceGapV1.model_validate(
        {
            "schema_version": "current_scan_evidence_gap.v1",
            "security_id": security_id,
            "as_of_session": SESSION.isoformat(),
            "reason": reason,
            "detail": detail,
        }
    )


def _view(records: list[StockRecord], current_evidence=None):
    return build_scan_market_view(
        records, {}, as_of_session=SESSION, current_evidence=current_evidence
    )


def _requirements(minimum: int) -> StrategyEvidenceRequirementsV1:
    requirement = EvidenceRequirementV1(
        kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=minimum
    )
    return StrategyEvidenceRequirementsV1(entry=(requirement,), exit=(requirement,))


def _run_log_row(fetched: int, run_id: str = "run-1") -> dict[str, str]:
    return {
        "run_id": run_id,
        "source_health_json": (
            '{"yahoo_market_data": {"source": "yahoo_market_data",'
            ' "state": "ok", "detail_code": "", "display_message": "",'
            f' "count": {fetched}}}}}'
        ),
    }


def _render(view: DataQualityViewV1, monkeypatch) -> str:
    monkeypatch.setattr(
        views_module,
        "load_data_quality_view",
        lambda *args, **kwargs: view,
        raising=True,
    )
    response = client.get("/partials/data-quality")
    assert response.status_code == 200
    return response.text


# --- I/O matrix -------------------------------------------------------


def test_clean_census_renders_fully_usable(monkeypatch) -> None:
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    quality = build_data_quality_view(view, unresolved=unresolved)

    assert quality.census is not None
    assert quality.census.clean == quality.census.total == 1
    assert quality.census.fault_counts == {}

    body = _render(quality, monkeypatch)
    assert "Usable 1" in body
    assert "Needs attention 0" in body
    # The head and the band key are part of the screen, not decoration.
    assert "Evidence Census" in body
    assert "as_of_session" in body


def test_every_fault_present_renders_overlap_note(monkeypatch) -> None:
    records = [
        _record("AAA", SESSION, PREVIOUS),
        _record("BBB", SESSION, PREVIOUS),
        _record("CCC", SESSION, PREVIOUS),
    ]
    evidence = _evidence(
        _gap("AAA", "insufficient_history", "too short"),
        _gap("BBB", "incomplete_history", "gap inside window"),
        _gap("CCC", "malformed_history", "unreadable"),
        _gap("DDD", "stale_session", "stale"),
    )
    view, unresolved = _view(records, evidence)
    quality = build_data_quality_view(
        view,
        current_evidence=evidence,
        unresolved=unresolved,
        strategy_requirements={"demo": _requirements(500)},
    )

    assert quality.census is not None
    counts = dict(quality.census.fault_counts)
    assert set(counts) == {"thin", "gapped", "missing_fragment", "dropped"}
    # Overlapping by construction: a shortfall marks every in-universe row
    # thin on top of its own artifact gap.
    assert sum(counts.values()) > quality.census.faulted
    assert (
        quality.census.clean + quality.census.faulted + quality.census.dropped
        == quality.census.total
    )

    body = _render(quality, monkeypatch)
    assert "overlap" in body
    for fault in counts:
        assert fault in body


def test_unexplained_residual_is_flagged_not_hidden(monkeypatch) -> None:
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    evidence = _evidence(_gap("ZZZ", "stale_session", "stale"))
    quality = build_data_quality_view(
        view,
        current_evidence=evidence,
        unresolved=unresolved,
        run_log_row=_run_log_row(9),
    )

    assert quality.funnel.available is True
    assert quality.funnel.unexplained == 8

    body = _render(quality, monkeypatch)
    assert "Unexplained residual" in body


def test_missing_run_log_row_reads_unavailable_not_shortfall(monkeypatch) -> None:
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    quality = build_data_quality_view(view, unresolved=unresolved, run_log_row=None)

    assert quality.funnel.available is False

    body = _render(quality, monkeypatch)
    assert "unavailable for this run" in body
    assert "shortfall" not in body.lower()


def test_empty_universe_renders_empty_state(monkeypatch) -> None:
    body = _render(
        DataQualityViewV1(unavailable_reason="No published scan artifact."),
        monkeypatch,
    )
    assert "No published scan artifact." in body
    assert "<table" not in body


def test_unreadable_artifact_degrades_fail_soft(monkeypatch, tmp_path) -> None:
    corrupt = tmp_path / "analysis.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(evidence_quality, "ANALYSIS_JSON", corrupt)

    quality = load_data_quality_view()

    assert quality.available is False
    assert quality.census is None
    body = _render(quality, monkeypatch)
    assert "No published scan artifact to account for." in body


# --- sort contract ----------------------------------------------------


def test_fault_severity_sort_values_and_absent_last(monkeypatch) -> None:
    records = [
        _record("AAA", SESSION, PREVIOUS),
        _record("BBB", SESSION, PREVIOUS),
    ]
    evidence = _evidence(_gap("BBB", "stale_session", "stale"))
    view, unresolved = _view(records, evidence)
    quality = build_data_quality_view(
        view, current_evidence=evidence, unresolved=unresolved
    )
    body = _render(quality, monkeypatch)

    rows = re.findall(r"<tr data-dq-band=.*?</tr>", body, re.S)
    assert len(rows) == 2
    clean_row = next(row for row in rows if 'data-dq-faults="none"' in row)
    faulted_row = next(row for row in rows if 'data-dq-band="dropped"' in row)
    # dropped outranks no fault, on the Faults cell's own sort key.
    assert 'data-sort="4"' in faulted_row
    # The cause cell is absent on the clean row and present on the faulted
    # one, and absence is marked wherever it renders.
    assert 'data-sort="" data-absent="1" class="dq-absent">—</td>' in clean_row
    assert "dq-cause" in faulted_row and "dq-cause" not in clean_row

    # The comparator keeps absent rows last in both directions.
    assert "const absentLeft = left.dataset.absent === '1';" in body
    assert "if (absentLeft !== absentRight) { return absentLeft ? 1 : -1; }" in body
    assert "return descending ? -delta : delta;" in body


def test_severity_ranks_escalate_dropped_over_thin() -> None:
    ranks = evidence_quality.FAULT_SEVERITY_RANK
    assert ranks["dropped"] > ranks["missing_fragment"] > ranks["gapped"]
    assert ranks["gapped"] > ranks["thin"] > 0


# --- read-only + benchmark -------------------------------------------


def test_screen_is_read_only(monkeypatch) -> None:
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    body = _render(build_data_quality_view(view, unresolved=unresolved), monkeypatch)

    assert "hx-post" not in body
    assert "hx-get" not in body
    assert "<form" not in body
    assert "fetch(" not in body


def test_benchmark_in_universe_is_not_falsely_unsatisfied(monkeypatch) -> None:
    records = [
        _record("AAA", SESSION, PREVIOUS),
        _record("SPY", SESSION, PREVIOUS),
    ]
    view, unresolved = _view(records)
    assert "SPY" in view.selected_universe

    quality = build_data_quality_view(
        view,
        unresolved=unresolved,
        strategy_inputs=(
            StrategyImpactInputV1(
                strategy_id="demo",
                display_name="Demo",
                regime_filter_enabled=True,
                benchmark_security_id="SPY",
                ma_length=200,
                entry_minimum_sessions=1,
                exit_minimum_sessions=1,
            ),
        ),
        snapshot=MarketRegimeSnapshotV1(
            spy_uptrend=True, return_52w_pct=1.0, session_count=250
        ),
    )

    assert quality.shared_inputs is not None
    strategy = quality.shared_inputs.strategies[0]
    assert strategy.benchmark_satisfied is True

    body = _render(quality, monkeypatch)
    assert '<dd class="dq-ok-v">No</dd>' in body


def test_loader_composes_from_a_published_artifact(monkeypatch, tmp_path) -> None:
    """The gatherer wires the real artifact through to a rendered view."""
    from datetime import datetime, timezone
    import json

    from app.schemas.analysis_artifact import build_analysis_payload

    evidence = _evidence(_gap("ZZZ", "stale_session", "stale"))
    path = tmp_path / "analysis.json"
    path.write_text(
        json.dumps(
            build_analysis_payload(
                [_record("AAA", SESSION, PREVIOUS).model_dump(mode="json")],
                run_id="run-1",
                generated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
                current_evidence=evidence,
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evidence_quality, "ANALYSIS_JSON", path)
    monkeypatch.setattr(evidence_quality, "_descriptors", lambda: ())
    monkeypatch.setattr(evidence_quality, "_snapshot", lambda: None)
    monkeypatch.setattr(evidence_quality, "_fx_sessions", lambda session: 300)
    monkeypatch.setattr(evidence_quality, "_holdings", lambda portfolio_id: ())

    quality = load_data_quality_view()

    assert quality.available is True
    assert quality.census is not None and quality.census.total == 2
    assert quality.shared_inputs is not None
    assert quality.shared_inputs.calendar.expected_sessions is not None
    body = _render(quality, monkeypatch)
    assert "AAA" in body


# --- regression cover for the triaged review findings -----------------


def test_holdings_are_accounted_for_and_exit_is_evaluated(monkeypatch) -> None:
    """P1: a held security the scan dropped is still accounted for."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    quality = build_data_quality_view(
        view,
        unresolved=unresolved,
        holdings=("AAA", "HELD"),
        strategy_requirements={"demo": _requirements(500)},
    )

    assert quality.census is not None
    rows = {row.security_id: row for row in quality.census.securities}
    assert set(rows) == {"AAA", "HELD"}
    # The dropped holding is in the disjoint accounting, so the template's
    # "these sum to N" claim stays true.
    assert (
        quality.census.clean + quality.census.faulted + quality.census.dropped
        == quality.census.total
        == 2
    )
    assert rows["HELD"].namespace == "none"
    # The exit path is now evaluated: it never was without holdings.
    assert any(item.path == "exit" for item in rows["AAA"].shortfalls)


def test_exit_eligibility_is_scoped_to_holdings(monkeypatch) -> None:
    """P2: eligible_exit counts holdings, not the whole scan universe."""
    records = [_record(ticker, SESSION, PREVIOUS) for ticker in ("AAA", "BBB", "CCC")]
    view, unresolved = _view(records)
    quality = build_data_quality_view(
        view,
        unresolved=unresolved,
        holdings=("AAA",),
        fx_sessions=300,
        strategy_inputs=(
            StrategyImpactInputV1(
                strategy_id="demo",
                display_name="Demo",
                entry_minimum_sessions=1,
                exit_minimum_sessions=1,
            ),
        ),
    )

    assert quality.shared_inputs is not None
    strategy = quality.shared_inputs.strategies[0]
    assert strategy.eligible_entry == 3
    assert strategy.eligible_exit == 1
    assert quality.holdings_count == 1

    body = _render(quality, monkeypatch)
    assert "3 of 3 scanned" in body
    # The card names both scopes, so neither count can be misread.
    assert "1 of 1 held" in body


def test_unreadable_alias_file_degrades_fail_soft(monkeypatch, tmp_path) -> None:
    """P3: a corrupt alias file must not 500 the screen that reports it."""
    from app.core.ticker_identity import AliasFileUnreadableError

    def _raise() -> dict[str, str]:
        raise AliasFileUnreadableError("alias file is corrupt")

    monkeypatch.setattr(evidence_quality, "load_aliases", _raise)
    monkeypatch.setattr(evidence_quality, "_descriptors", lambda: ())
    monkeypatch.setattr(evidence_quality, "_snapshot", lambda: None)
    monkeypatch.setattr(evidence_quality, "_fx_sessions", lambda session: None)
    monkeypatch.setattr(evidence_quality, "_holdings", lambda portfolio_id: ())

    assert evidence_quality._aliases() == {}

    response = client.get("/partials/data-quality")
    assert response.status_code == 200


def test_empty_portfolio_id_does_not_422(monkeypatch) -> None:
    """P1: the client sends ``portfolio_id=`` when no account is selected."""
    seen: list[int | None] = []

    def _load(portfolio_id: int | None = None) -> DataQualityViewV1:
        seen.append(portfolio_id)
        return DataQualityViewV1(unavailable_reason="none")

    monkeypatch.setattr(views_module, "load_data_quality_view", _load)
    assert client.get("/partials/data-quality?portfolio_id=").status_code == 200
    assert client.get("/partials/data-quality?portfolio_id=7").status_code == 200
    assert seen == [None, 7]


def test_each_column_sorts_by_what_it_displays(monkeypatch) -> None:
    """P4: no column sorts by row order or by another column's value."""
    records = [
        _record("AAA", SESSION, PREVIOUS),
        _record("BBB", SESSION, PREVIOUS),
    ]
    evidence = _evidence(_gap("BBB", "stale_session", "stale"))
    view, unresolved = _view(records, evidence)
    quality = build_data_quality_view(
        view, current_evidence=evidence, unresolved=unresolved, holdings=("ZZZ",)
    )
    body = _render(quality, monkeypatch)

    for row in re.findall(r"<tr data-dq-band=.*?</tr>", body, re.S):
        cells = re.findall(r"<td([^>]*)>(.*?)</td>", row, re.S)
        assert len(cells) == 10
        for attributes, text in cells:
            sort = re.search(r'data-sort="([^"]*)"', attributes)
            assert sort is not None
            shown = " ".join(re.sub(r"<[^>]+>", " ", text).split())
            if 'data-absent="1"' in attributes:
                assert shown == "—" and sort.group(1) == ""
            elif "dq-num" in attributes:
                # A number sorts by the number it shows, never by row order.
                assert sort.group(1) == shown

    # Every em-dash cell in the table is marked absent, on whichever column
    # renders it, so the last-in-both-directions rule covers them all.
    for attributes, text in re.findall(
        r"<td([^>]*)>(.*?)</td>", body.split("<tbody>")[1], re.S
    ):
        if text.strip() == "—":
            assert 'data-absent="1"' in attributes
    assert "a.localeCompare(b)" in body


def test_sorting_is_keyboard_reachable_and_announced(monkeypatch) -> None:
    """P6: sorting is an acceptance criterion, so it cannot be mouse-only."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    body = _render(build_data_quality_view(view, unresolved=unresolved), monkeypatch)

    headers = re.findall(r"<th[^>]*data-dq-sort[^>]*>", body)
    assert len(headers) == 10
    for header in headers:
        assert 'tabindex="0"' in header
        assert 'aria-sort="none"' in header
    assert "keydown" in body
    assert "aria-sort" in body

    assert 'aria-controls="dq-stock"' in body
    assert 'aria-labelledby="dq-tab-stock"' in body
    assert 'aria-controls="dq-strategy"' in body
    assert 'aria-labelledby="dq-tab-strategy"' in body


def test_failed_strategy_discovery_reads_as_unavailable(monkeypatch) -> None:
    """P5: a failed discovery must not read as a confident "none"."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    quality = build_data_quality_view(
        view, unresolved=unresolved, strategies_unavailable=True
    )
    body = _render(quality, monkeypatch)
    assert "Strategy discovery is unavailable" in body
    assert "No discoverable Strategy declares" not in body

    clean = _render(build_data_quality_view(view, unresolved=unresolved), monkeypatch)
    assert "No discoverable Strategy declares" in clean
    assert "Strategy discovery is unavailable" not in clean


def test_discovery_failure_is_distinguished_from_empty_roster(monkeypatch) -> None:
    """P5: the loader carries the distinction, not just the template."""

    def _boom() -> tuple[()]:
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(evidence_quality, "_descriptors", _boom)
    view, _unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    assert evidence_quality._strategies(view) == ({}, (), 0, False)

    monkeypatch.setattr(evidence_quality, "_descriptors", lambda: ())
    assert evidence_quality._strategies(view) == ({}, (), 0, True)


def test_zero_matches_has_an_empty_state(monkeypatch) -> None:
    """P7b: a header over an empty tbody reads as broken."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    body = _render(build_data_quality_view(view, unresolved=unresolved), monkeypatch)
    assert 'id="dq-empty"' in body
    assert "No securities match this filter." in body
    assert "empty.hidden = shown !== 0;" in body


def test_sub_tab_toggle_guards_a_missing_panel(monkeypatch) -> None:
    """P7a: a missing panel id must not throw and kill the toggle."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    body = _render(build_data_quality_view(view, unresolved=unresolved), monkeypatch)
    assert "if (panel) { panel.hidden = !on; }" in body


def test_clean_rows_are_not_coloured(monkeypatch) -> None:
    """P7c: colour is reserved for what needs attention."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    body = _render(build_data_quality_view(view, unresolved=unresolved), monkeypatch)
    clean_row = next(
        row
        for row in re.findall(r"<tr data-dq-band=.*?</tr>", body, re.S)
        if 'data-dq-faults="none"' in row
    )
    assert "dq-b-thin" not in clean_row
    assert "dq-b-drop" not in clean_row
    assert "clean" in clean_row


def test_stylesheet_uses_tokens_and_flat_geometry() -> None:
    """Acceptance: no literal hex colour, no non-zero structural radius."""
    from pathlib import Path

    import app.api.app as app_module

    css = (
        Path(app_module.__file__).parent / "static" / "css" / "data_quality.css"
    ).read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    assert re.search(r"#[0-9a-fA-F]{3,8}", body) is None
    radii = re.findall(r"border-radius:\s*([^;]+);", body)
    assert radii
    # Structural containers are flat; form controls keep the shared token.
    # Structural containers are flat; form controls keep the shared input
    # token and badges the shared badge token (the project's own pattern).
    assert all(
        value.strip() in {"0", "var(--radius-input)", "var(--radius-sm)"}
        for value in radii
    )


# --- mockup parity (#639) ---------------------------------------------


def test_balance_equation_renders_every_term(monkeypatch) -> None:
    """The funnel reads as an equation that closes, not a flat list."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    evidence = _evidence(_gap("ZZZ", "stale_session", "stale"))
    quality = build_data_quality_view(
        view,
        current_evidence=evidence,
        unresolved=unresolved,
        run_log_row=_run_log_row(1),
    )
    body = _render(quality, monkeypatch)

    for label in (
        "discovered",
        "excluded by criteria",
        "with records",
        "dropped with cause",
        "in universe",
        "unexplained",
    ):
        assert label in body
    assert "dq-equation" in body


def test_filter_chips_carry_their_own_counts(monkeypatch) -> None:
    """The fault filter is a chip row with counts, not a bare select."""
    records = [_record("AAA", SESSION, PREVIOUS), _record("BBB", SESSION, PREVIOUS)]
    evidence = _evidence(_gap("BBB", "stale_session", "stale"))
    view, unresolved = _view(records, evidence)
    quality = build_data_quality_view(
        view, current_evidence=evidence, unresolved=unresolved, holdings=("AAA",)
    )
    body = _render(quality, monkeypatch)

    assert "<select" not in body
    assert 'data-dq-filter="all"' in body
    assert 'data-dq-filter="dropped"' in body
    assert 'data-dq-filter="held"' in body
    assert "Held 1" in body


def test_subtotal_row_is_recalculated_client_side(monkeypatch) -> None:
    """The tfoot describes the filtered rows, so it cannot be server-static."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    body = _render(build_data_quality_view(view, unresolved=unresolved), monkeypatch)

    assert '<td colspan="10" id="dq-subtotal"></td>' in body
    assert "subtotal.textContent" in body
    assert "Showing ' + shown + ' of '" in body


def test_row_shows_its_fx_ceiling_from_the_shared_inputs(monkeypatch) -> None:
    """The per-security FX ceiling is joined in, never recomputed."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    quality = build_data_quality_view(
        view,
        unresolved=unresolved,
        currencies={"AAA": "USD"},
        fx_sessions=1,
    )

    assert quality.fx_by_security["AAA"].usable_sessions == 1
    body = _render(quality, monkeypatch)
    assert "dq-cap" in body  # capped below the security's own 2 sessions


def test_strategy_tab_renders_cards_with_verdict_badges(monkeypatch) -> None:
    """The strategy tab is cards with a headline verdict, not label rows."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    quality = build_data_quality_view(
        view,
        unresolved=unresolved,
        fx_sessions=300,
        strategy_inputs=(
            StrategyImpactInputV1(
                strategy_id="demo",
                display_name="Demo",
                entry_minimum_sessions=500,
                exit_minimum_sessions=1,
            ),
        ),
    )
    body = _render(quality, monkeypatch)

    assert "0 can enter" in body
    assert "dq-blocked" in body
    assert "Nothing can be bought." in body


def test_corporate_actions_note_is_absent_not_invented(monkeypatch) -> None:
    """The pipeline carries no actions evidence, so the card says so."""
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    body = _render(build_data_quality_view(view, unresolved=unresolved), monkeypatch)

    assert "Corporate actions" in body
    corporate = body.split("Corporate actions", 1)[1][:400]
    assert "not available" in corporate


def test_footnotes_name_the_modules_the_figures_come_from(monkeypatch) -> None:
    view, unresolved = _view([_record("AAA", SESSION, PREVIOUS)])
    body = _render(build_data_quality_view(view, unresolved=unresolved), monkeypatch)

    assert "dq-footnotes" in body
    for source in ("evidence_coverage", "evidence_requirements", "SourceHealth.count"):
        assert source in body
