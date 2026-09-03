"""Route tests for the recommendations screen (#441).

TestClient + dependency_overrides; the real Jinja template renders to
catch markup errors. Every state renders 200 — never a 500.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import (
    get_portfolio_recommendation_service,
    get_trader_service,
)
from app.schemas.portfolio_recommendation import (
    EvaluationCoverageV1,
    EvaluationUnavailable,
    EvidenceState,
    NoAssignment,
    RecommendationReasonV1,
    RecommendationResultV1,
    RecommendationV1,
)

client = TestClient(app)

SESSION = date(2026, 8, 28)


def _result(freshness: str = "fresh") -> RecommendationResultV1:
    return RecommendationResultV1(
        portfolio_id=7,
        analysis_run_id="run-1",
        generated_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        market_session=SESSION,
        freshness=cast(Any, freshness),
        strategy_id="alpha",
        strategy_source_digest="a" * 64,
        parameters={"lookback": 20},
        recommendations=(
            RecommendationV1(
                action="sell",
                ticker="AAA",
                security_id="AAA",
                rule_id="exit_history_fall",
                reason="Strategy rule exit_history_fall",
            ),
            RecommendationV1(
                action="hold",
                ticker="BBB",
                security_id="BBB",
                rule_id="no_exit_signal",
                reason="No exit signal from the assigned Strategy — position held.",
            ),
            RecommendationV1(
                action="buy",
                ticker="CCC",
                security_id="CCC",
                rule_id="entry_history_rise",
                reason="Strategy rule entry_history_rise",
            ),
        ),
        evaluated_at=datetime.now(UTC),
    )


@pytest.fixture
def mocked(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    stub = MagicMock()
    trader = MagicMock()
    trader.get_portfolio_meta.return_value.name = "SIPP"
    app.dependency_overrides[get_portfolio_recommendation_service] = lambda: stub
    app.dependency_overrides[get_trader_service] = lambda: trader
    try:
        yield {"stub": stub}
    finally:
        app.dependency_overrides.clear()


def test_happy_path_renders_groups_in_order(mocked: dict[str, Any]) -> None:
    mocked["stub"].recommend.return_value = _result()
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    html = resp.text
    assert "Recommendations — SIPP" in html
    assert "digest aaaaaaaa" in html
    sell_at = html.index("Sell (1)")
    hold_at = html.index("Hold (1)")
    buy_at = html.index("Buy (1)")
    assert sell_at < hold_at < buy_at
    assert "exit_history_fall" in html
    assert "no_exit_signal" in html
    assert "entry_history_rise" in html


def test_stale_warning_renders_alongside_results(mocked: dict[str, Any]) -> None:
    mocked["stub"].recommend.return_value = _result(freshness="stale")
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert "alert-warning" in resp.text
    assert "Sell (1)" in resp.text


def test_no_assignment_renders_assign_link(mocked: dict[str, Any]) -> None:
    mocked["stub"].recommend.return_value = NoAssignment()
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert 'hx-get="/partials/strategy-assign?portfolio_id=7"' in resp.text
    assert "No Strategy assigned" in resp.text


def test_evaluation_unavailable_renders_alert_not_500(mocked: dict[str, Any]) -> None:
    mocked["stub"].recommend.return_value = EvaluationUnavailable(
        reason="Strategy runtime could not be loaded: boom"
    )
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert "alert-danger" in resp.text
    assert "Strategy runtime could not be loaded: boom" in resp.text
    assert "Retry" in resp.text


def test_all_states_return_200(mocked: dict[str, Any]) -> None:
    outcomes: list[Any] = [
        _result(),
        _result(freshness="missing"),
        NoAssignment(),
        EvaluationUnavailable(reason="x"),
    ]
    for outcome in outcomes:
        mocked["stub"].recommend.return_value = outcome
        resp = client.get("/portfolios/7/recommendations")
        assert resp.status_code == 200


def _incomplete_result(
    entry_state: EvidenceState = "incompatible",
    exit_state: EvidenceState = "incompatible",
    missing: tuple[str, ...] = ("scan_stage",),
) -> RecommendationResultV1:
    """A result whose evidence was incomplete — no Sell/Buy rows (#471)."""
    base = _result()
    return base.model_copy(
        update={
            "recommendations": (
                RecommendationV1(
                    action="hold",
                    ticker="AAA",
                    security_id="AAA",
                    rule_id="exit_evidence_unsupported",
                    reason="fails safe to Hold",
                    evidence_warnings=("exit_evidence_unsupported",) + missing,
                ),
            ),
            "coverage": EvaluationCoverageV1(
                entry_state=entry_state,
                exit_state=exit_state,
                entry_missing_evidence=missing,
                exit_missing_evidence=missing,
                evaluated_securities=2,
            ),
        }
    )


def test_incomplete_evidence_replaces_no_signals_wording(
    mocked: dict[str, Any],
) -> None:
    mocked["stub"].recommend.return_value = _incomplete_result()
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    html = resp.text
    assert "Evidence incomplete" in html
    assert "scan_stage" in html
    assert "Signals unavailable" in html
    assert "No Buy signals." not in html
    assert "No Sell signals." not in html
    # Hold was genuinely evaluated, so its own empty wording is unchanged.
    assert "Hold (1)" in html


def test_degraded_evidence_names_the_affected_securities(
    mocked: dict[str, Any],
) -> None:
    result = _incomplete_result(
        entry_state="degraded", exit_state="degraded", missing=()
    ).model_copy(
        update={
            "coverage": EvaluationCoverageV1(
                entry_state="degraded",
                exit_state="degraded",
                evaluated_securities=2,
                degraded_securities=("AAA", "BBB"),
            )
        }
    )
    mocked["stub"].recommend.return_value = result
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert "Evidence incomplete" in resp.text
    assert "AAA" in resp.text and "BBB" in resp.text
    # A degraded path IS invoked, so the honest empty wording stays.
    assert "No Buy signals." in resp.text
    assert "Signals unavailable" not in resp.text


def test_complete_evaluation_keeps_the_no_signals_wording(
    mocked: dict[str, Any],
) -> None:
    empty = _result().model_copy(update={"recommendations": ()})
    mocked["stub"].recommend.return_value = empty
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert "No Buy signals." in resp.text
    assert "Evidence incomplete" not in resp.text
    assert "Signals unavailable" not in resp.text


def test_one_incompatible_path_scopes_the_wording(mocked: dict[str, Any]) -> None:
    """Only the blocked path's consequence is claimed (#471)."""
    result = _incomplete_result(entry_state="compatible", exit_state="incompatible")
    mocked["stub"].recommend.return_value = result
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    html = resp.text
    assert "holdings fail safe to Hold" in html
    assert "no Buy signals are produced" not in html
    # Sell was never evaluated; Buy was, so only Sell loses its wording.
    assert "Signals unavailable" in html
    assert "No Buy signals." in html
    assert "No Sell signals." not in html


def test_degraded_security_list_is_capped(mocked: dict[str, Any]) -> None:
    """A wide degraded set is summarised rather than dumped (#471)."""
    tickers = tuple(f"T{index:02d}" for index in range(12))
    result = _result().model_copy(
        update={
            "recommendations": (),
            "coverage": EvaluationCoverageV1(
                entry_state="degraded",
                exit_state="degraded",
                evaluated_securities=12,
                degraded_securities=tickers,
            ),
        }
    )
    mocked["stub"].recommend.return_value = result
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert "+2 more" in resp.text
    assert "T09" in resp.text
    assert "T10" not in resp.text


# ---------------------------------------------------------------------------
# #472 -- Strategy explanations render on the screen
# ---------------------------------------------------------------------------

_EXPLAINED_SELL = RecommendationV1(
    action="sell",
    ticker="AAA",
    security_id="AAA",
    rule_id="weinstein_stage_exit_v1",
    reason=(
        "Close fell below the 150-session moving average. · "
        "The security is no longer in a Stage 2 advance."
    ),
    explanation=(
        RecommendationReasonV1(
            code="close_below_sma150",
            summary="Close fell below the 150-session moving average.",
            facts=("Close 92.1 < 101.44",),
        ),
        RecommendationReasonV1(
            code="stage_exit",
            summary="The security is no longer in a Stage 2 advance.",
            facts=("Weinstein stage Stage 3 is not Stage 2",),
        ),
    ),
)


def test_explanation_reasons_and_facts_render_with_rule_provenance(
    mocked: dict[str, Any],
) -> None:
    result = _result().model_copy(update={"recommendations": (_EXPLAINED_SELL,)})
    mocked["stub"].recommend.return_value = result

    html = client.get("/portfolios/7/recommendations").text

    assert "Close fell below the 150-session moving average." in html
    assert "The security is no longer in a Stage 2 advance." in html
    assert "Close 92.1 &lt; 101.44" in html
    assert "Weinstein stage Stage 3 is not Stage 2" in html
    assert "weinstein_stage_exit_v1" in html


def test_rows_without_an_explanation_keep_the_generic_reason(
    mocked: dict[str, Any],
) -> None:
    mocked["stub"].recommend.return_value = _result()

    html = client.get("/portfolios/7/recommendations").text

    assert "Strategy rule exit_history_fall" in html


# #473 -- the portfolio's own import spelling leads each row


def _aliased_result() -> RecommendationResultV1:
    """One aliased holding (display ``HSFWA``, canonical ``0P00013P6I.L``)
    beside one unaliased holding."""
    base = _result()
    return base.model_copy(
        update={
            "recommendations": (
                RecommendationV1(
                    action="sell",
                    ticker="HSFWA",
                    security_id="0P00013P6I.L",
                    rule_id="exit_history_fall",
                    reason="Strategy rule exit_history_fall",
                ),
                RecommendationV1(
                    action="hold",
                    ticker="WCOG",
                    security_id="WCOG",
                    rule_id="no_exit_signal",
                    reason="No exit signal from the assigned Strategy.",
                ),
            )
        }
    )


def test_screen_leads_with_import_spelling_and_shows_canonical_id(
    mocked: dict[str, Any],
) -> None:
    """The aliased row renders ``HSFWA`` first with the canonical id as
    secondary text underneath it."""
    mocked["stub"].recommend.return_value = _aliased_result()
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    html = resp.text
    assert "HSFWA" in html
    assert html.index("HSFWA") < html.index("0P00013P6I.L")


def test_screen_omits_canonical_line_for_unaliased_row(
    mocked: dict[str, Any],
) -> None:
    """An unaliased holding has one spelling, so nothing is echoed."""
    mocked["stub"].recommend.return_value = _aliased_result()
    assert client.get("/portfolios/7/recommendations").text.count("WCOG") == 1


# --- gh-474: the host-bound universe must not overflow the header ---------

#: A realistic large universe — hundreds of symbols, as a live scan binds.
LARGE_UNIVERSE = tuple(f"SYM{index:04d}" for index in range(612))


def _universe_result(
    parameters: dict[str, Any] | None = None,
    universe_parameter: str | None = "selected_securities",
) -> RecommendationResultV1:
    """A result whose parameters carry a host-bound universe."""
    base = _result()
    return base.model_copy(
        update={
            "parameters": (
                {"lookback": 20, "selected_securities": LARGE_UNIVERSE}
                if parameters is None
                else parameters
            ),
            "universe_parameter": universe_parameter,
        }
    )


def _header(html: str) -> str:
    """The provenance block, i.e. everything before the group tables."""
    return html.partition("<h3")[0]


def test_large_universe_is_summarised_not_printed_as_a_tuple(
    mocked: dict[str, Any],
) -> None:
    """AC1: a count plus the scalar badges, never the raw tuple."""
    mocked["stub"].recommend.return_value = _universe_result()
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    header = _header(resp.text)
    assert "Selected universe: 612 securities" in header
    assert "lookback=20" in header
    # No raw tuple/list rendering of the universe as one parameter value.
    assert "selected_securities=" not in header
    assert "&#39;SYM0000&#39;," not in header
    assert "'SYM0000'," not in header


def test_large_universe_lives_in_a_bounded_accessible_disclosure(
    mocked: dict[str, Any],
) -> None:
    """AC2: keyboard-reachable ``<details>``/``<summary>`` with a name,
    and a body that bounds its height and scrolls internally."""
    mocked["stub"].recommend.return_value = _universe_result()
    html = _header(client.get("/portfolios/7/recommendations").text)
    assert "<details" in html and "</details>" in html
    summary_at = html.index("<summary")
    summary_end = html.index("</summary>")
    summary = html[summary_at:summary_end]
    # The visible summary text is the disclosure's accessible name.
    assert "612 selected securities" in summary
    body_at = html.index('id="selected-universe-list"')
    body = html[body_at : html.index("</div>", body_at)]
    assert "max-height:" in body
    assert "overflow:auto" in body
    # The clipped region must be reachable and named for keyboard/AT users.
    assert 'tabindex="0"' in body
    assert "aria-label=" in body
    # Every symbol is still exposed inside the bounded container.
    assert body.count("SYM") == len(LARGE_UNIVERSE)


def test_no_element_forces_page_level_horizontal_scroll(
    mocked: dict[str, Any],
) -> None:
    """AC3: long values wrap/scroll locally — no fixed or unbounded width."""
    mocked["stub"].recommend.return_value = _universe_result(
        {"lookback": 20, "note": "x" * 4000, "selected_securities": LARGE_UNIVERSE}
    )
    header = _header(client.get("/portfolios/7/recommendations").text)
    assert "overflow-wrap:anywhere" in header
    assert "min-width:0" in header
    # Nothing pins a value open: no nowrap, and no fixed pixel/absolute width.
    assert "white-space:nowrap" not in header
    assert "text-nowrap" not in header


def test_legacy_result_without_universe_parameter_still_summarised(
    mocked: dict[str, Any],
) -> None:
    """Fallback key detection covers a pre-gh-474 stored result."""
    mocked["stub"].recommend.return_value = _universe_result(
        {"lookback": 20, "security_ids": list(LARGE_UNIVERSE)},
        universe_parameter=None,
    )
    header = _header(client.get("/portfolios/7/recommendations").text)
    assert "Selected universe: 612 securities" in header
    assert "security_ids=" not in header


def test_result_without_universe_key_renders_scalar_badges_only(
    mocked: dict[str, Any],
) -> None:
    mocked["stub"].recommend.return_value = _universe_result(
        {"lookback": 20}, universe_parameter=None
    )
    header = _header(client.get("/portfolios/7/recommendations").text)
    assert "lookback=20" in header
    assert "Selected universe" not in header
    assert "<details" not in header


def test_empty_universe_shows_zero_and_no_disclosure(
    mocked: dict[str, Any],
) -> None:
    mocked["stub"].recommend.return_value = _universe_result(
        {"lookback": 20, "selected_securities": ()}
    )
    header = _header(client.get("/portfolios/7/recommendations").text)
    assert "Selected universe: 0 securities" in header
    assert "<details" not in header


def test_scalar_universe_key_renders_as_an_ordinary_badge(
    mocked: dict[str, Any],
) -> None:
    """A string value is a tuning knob, not a selection — no count."""
    mocked["stub"].recommend.return_value = _universe_result(
        {"lookback": 20, "selected_securities": "AAA"}
    )
    header = _header(client.get("/portfolios/7/recommendations").text)
    assert "selected_securities=AAA" in header
    assert "Selected universe" not in header


def test_descriptor_named_key_outranks_a_stale_legacy_key(
    mocked: dict[str, Any],
) -> None:
    """The bound universe wins; the stale key still renders as a badge."""
    mocked["stub"].recommend.return_value = _universe_result(
        {
            "lookback": 20,
            "security_ids": ["STALE1", "STALE2"],
            "selected_securities": LARGE_UNIVERSE,
        }
    )
    header = _header(client.get("/portfolios/7/recommendations").text)
    assert "Selected universe: 612 securities" in header
    # The other key is not silently swallowed — it stays auditable.
    assert "security_ids=" in header
    assert "STALE1" in header


def test_single_security_universe_is_singular(mocked: dict[str, Any]) -> None:
    """A one-symbol universe reads "1 security", not "1 securities"."""
    mocked["stub"].recommend.return_value = _universe_result(
        {"lookback": 20, "selected_securities": ("AAA",)}
    )
    header = _header(client.get("/portfolios/7/recommendations").text)
    assert "Selected universe: 1 security" in header
    assert "1 securities" not in header


def test_universe_symbols_are_separated_for_copying(
    mocked: dict[str, Any],
) -> None:
    """Symbols must not concatenate when the list is copied as text."""
    mocked["stub"].recommend.return_value = _universe_result()
    html = _header(client.get("/portfolios/7/recommendations").text)
    body_at = html.index('id="selected-universe-list"')
    body = html[body_at : html.index("</div>", body_at)]
    assert "SYM0000SYM0001" not in body
