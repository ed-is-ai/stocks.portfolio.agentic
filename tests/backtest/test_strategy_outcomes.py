from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from markupsafe import escape
import pytest

from app.repositories.backtest_repo import (
    BacktestRepository,
    RecentBacktestResultSummariesV1,
)
from app.api.templating import templates
from app.services.backtest.metrics import MetricUnavailableReason
from app.services.backtest.benchmark_series import (
    BAD_EVIDENCE_NOTE,
    NO_PIN_NOTE,
    EvidenceReader,
)
from app.services.backtest.strategy_outcomes import (
    AMBIGUOUS_FX_NOTE,
    MISMATCHED_FX_NOTE,
    MISMATCHED_PIN_NOTE,
    NO_SPY_REFERENCE_LIMITATION,
    NO_USD_SECURITIES_NOTE,
    StrategyOutcomeService,
)
from tests.backtest.benchmark_fakes import FakePrices, fx_evidence, spy_evidence


class _Repo:
    def __init__(self, results):
        self.results = tuple(results)
        self.limit = None
        self.comparison_calls = []

    def recent_backtest_result_summaries(self, *, limit):
        self.limit = limit
        return RecentBacktestResultSummariesV1(
            self.results[:limit], min(len(self.results), limit), 0, 0, 0, ()
        )

    def with_display_evidence(self, summaries):
        return tuple(summaries)

    def compare_result_summaries(self, left_result, right_result):
        left, right = left_result.run_id, right_result.run_id
        self.comparison_calls.append((left, right))
        eligible = left_result.cohort == right_result.cohort
        return SimpleNamespace(
            eligible=eligible,
            reason=None if eligible else SimpleNamespace(value="period_mismatch"),
        )

    def roster_member_identities(self, _profile_hash):
        return (("spy-security", "SPY", "ARCX", "USD"),)

    def snapshot_member_revisions(self, _profile_hash, _month):
        return (("spy-security", "revision-1"),)

    def snapshot_coverage(self, _profile_hash):
        return SimpleNamespace(provenance=())


class _LegacyWildcardRepo(_Repo):
    def compare_result_summaries(self, left_result, right_result):
        left, right = left_result.run_id, right_result.run_id
        self.comparison_calls.append((left, right))
        left_selection = left_result.universe_selection
        right_selection = right_result.universe_selection
        left_digest = (
            None if left_selection is None else left_selection.run_universe_digest
        )
        right_digest = (
            None if right_selection is None else right_selection.run_universe_digest
        )
        eligible = (
            left_digest is None or right_digest is None or left_digest == right_digest
        )
        return SimpleNamespace(
            eligible=eligible,
            reason=(
                None if eligible else SimpleNamespace(value="evidence_digest_mismatch")
            ),
        )


class _GraphRepo(_Repo):
    def __init__(self, results, compatible_pairs):
        super().__init__(results)
        self.compatible_pairs = {frozenset(pair) for pair in compatible_pairs}

    def compare_result_summaries(self, left_result, right_result):
        left, right = left_result.run_id, right_result.run_id
        self.comparison_calls.append((left, right))
        eligible = frozenset((left, right)) in self.compatible_pairs
        return SimpleNamespace(
            eligible=eligible,
            reason=None if eligible else SimpleNamespace(value="period_mismatch"),
        )


def _result(
    run_id: str,
    strategy_id: str,
    cohort: str,
    completed_at: datetime,
    *,
    starting_capital: str = "10000",
    security_ids: tuple[str, ...] = ("spy-security",),
    sharpe: float | None = 1.2,
    base_currency: str = "USD",
    pin_digest: str | None = None,
):
    sessions = (
        date(2025, 1, 2),
        date(2025, 1, 31),
        date(2025, 2, 28),
    )
    return SimpleNamespace(
        run_id=run_id,
        strategy_id=strategy_id,
        strategy_api_version=1,
        strategy_source_digest="a" * 64,
        completed_at=completed_at,
        start_month="2025-01",
        end_month="2025-02",
        base_currency=base_currency,
        starting_capital=Decimal(starting_capital),
        run_input_manifest_digest=f"manifest-{run_id}",
        regime_benchmark=(
            None
            if pin_digest is None
            else SimpleNamespace(
                security_id="spy-security",
                price_revision="spy",
                evidence_digest=pin_digest,
            )
        ),
        profile_hash="pinned-profile",
        parameters={"lookback": 20, "selected_tickers": ["spy-security"]},
        universe_selection=SimpleNamespace(
            canonical_security_ids=security_ids,
            universe_parameter="selected_tickers",
        ),
        metrics=SimpleNamespace(
            total_return=0.12,
            sharpe_ratio=sharpe,
            win_rate=0.55,
            max_drawdown=-0.2,
        ),
        metric_availability=SimpleNamespace(
            sharpe_unavailable=(
                MetricUnavailableReason.INSUFFICIENT_DAILY_RETURNS
                if sharpe is None
                else None
            ),
            win_rate_unavailable=None,
        ),
        closed_trade_count=0,
        equity_point_count=len(sessions),
        equity_curve=tuple(
            SimpleNamespace(
                session=session,
                total_equity_base=Decimal(starting_capital) * Decimal(1 + index / 10),
            )
            for index, session in enumerate(sessions)
        ),
        candidate_count=12,
        cohort=cohort,
    )


def _install_presenter_fakes(monkeypatch):
    import app.services.backtest.strategy_outcomes as outcomes

    monkeypatch.setattr(
        outcomes,
        "provenance_view",
        lambda _result, _coverage: SimpleNamespace(entries=()),
    )


def test_selects_largest_latest_comparable_cohort_and_newest_per_strategy(
    monkeypatch,
):
    _install_presenter_fakes(monkeypatch)
    newest = datetime(2025, 3, 1, tzinfo=timezone.utc)
    repo = _Repo(
        (
            _result(
                "a-buy-new",
                "rtly-backtest-buy-and-hold",
                "a",
                newest,
                starting_capital="10000",
            ),
            _result(
                "z-buy-same-time",
                "rtly-backtest-buy-and-hold",
                "a",
                newest,
                starting_capital="10000",
            ),
            _result(
                "a-buy-old",
                "rtly-backtest-buy-and-hold",
                "a",
                datetime(2025, 2, 1, tzinfo=timezone.utc),
            ),
            _result(
                "a-weinstein",
                "weinstein",
                "a",
                newest,
                starting_capital="12000",
                sharpe=None,
            ),
            _result("c-trend", "trend-c", "c", newest),
            _result("d-trend", "trend-d", "c", newest),
        )
    )

    summary = StrategyOutcomeService(repo).build_summary()

    assert repo.limit == 25
    assert summary.state == "cohort"
    assert summary.cohort_candidate_count == 4
    assert summary.cohort_strategy_count == 2
    assert [run.run_id for run in summary.runs] == ["a-buy-new", "a-weinstein"]
    assert [(item.reason, item.count) for item in summary.comparison_exclusions] == [
        ("period_mismatch", 2)
    ]
    assert summary.selected_spy_handle == "R01"
    assert summary.runs[0].is_spy_reference
    assert summary.runs[1].metrics.sharpe_ratio is None
    assert (
        summary.runs[1].metric_availability["sharpe_ratio"]
        == MetricUnavailableReason.INSUFFICIENT_DAILY_RETURNS.value
    )
    assert summary.runs[1].metric_display["sharpe_ratio"] != "0.00"
    assert summary.equity_mode == "indexed"
    assert summary.equity_payload is not None
    assert summary.equity_payload["series"][0]["label"] == "SPY Buy and Hold"
    assert summary.equity_payload["series"][0]["is_benchmark"] is True
    assert "is_benchmark" not in summary.equity_payload["series"][1]
    assert len(summary.equity_payload["dates"]) == 3
    assert [row["date"] for row in summary.equity_payload["table_rows"]] == [
        "2025-01-31",
        "2025-02-28",
    ]
    assert "selected_tickers" not in summary.runs[0].parameters
    rendered = templates.get_template("_strategy_outcome_summary.html").render(
        outcome_summary=summary,
        pending_experiment_detail=None,
    )
    assert 'id="strategy-outcome-equity-chart"' in rendered
    assert "View monthly stored portfolio values and indexed growth" in rendered
    assert "SPY Buy and Hold" in rendered
    assert "Strategies vs SPY" in rendered
    assert 'id="strategy-outcome-drawdown-chart"' in rendered
    assert 'id="strategy-outcome-start"' in rendered
    assert 'id="strategy-outcome-end"' in rendered
    assert "2025-01" in rendered and "2025-02" in rendered


def test_cohort_membership_checks_universe_compatibility_against_every_member():
    completed = datetime(2025, 3, 1, tzinfo=timezone.utc)
    legacy = _result("legacy-run", "legacy", "same-period", completed)
    legacy.universe_selection = None
    universe_a = _result("universe-a-run", "strategy-a", "same-period", completed)
    universe_a.universe_selection.run_universe_digest = "universe-a"
    universe_b = _result("universe-b-run", "strategy-b", "same-period", completed)
    universe_b.universe_selection.run_universe_digest = "universe-b"
    repo = _LegacyWildcardRepo((legacy, universe_a, universe_b))

    selection = StrategyOutcomeService(repo)._select(repo.results)

    assert selection.state == "cohort"
    assert {result.run_id for result in selection.results} == {
        "legacy-run",
        "universe-a-run",
    }
    assert selection.cohort_candidate_count == 2
    assert [(item.reason, item.count) for item in selection.comparison_exclusions] == [
        ("evidence_digest_mismatch", 1)
    ]
    assert ("universe-a-run", "universe-b-run") in repo.comparison_calls


def test_selects_largest_pairwise_comparable_cohort_not_first_fit():
    completed = datetime(2025, 3, 1, tzinfo=timezone.utc)
    candidates = (
        _result("a", "strategy-a", "unused", completed),
        _result("c", "strategy-c", "unused", completed),
        _result("b", "strategy-b", "unused", completed),
        _result("d", "strategy-d", "unused", completed),
    )
    repo = _GraphRepo(
        candidates,
        compatible_pairs=(("a", "b"), ("a", "c"), ("a", "d"), ("b", "d")),
    )

    selection = StrategyOutcomeService(repo)._select(repo.results)

    assert selection.state == "cohort"
    assert {result.run_id for result in selection.results} == {"a", "b", "d"}
    assert selection.cohort_strategy_count == 3


def test_no_shared_cohort_keeps_individual_results_unranked(monkeypatch):
    _install_presenter_fakes(monkeypatch)
    completed = datetime(2025, 3, 1, tzinfo=timezone.utc)
    repo = _Repo(
        (
            _result("a-run", "rtly-backtest-buy-and-hold", "a", completed),
            _result("b-run", "weinstein", "b", completed),
        )
    )

    summary = StrategyOutcomeService(repo).build_summary()

    assert summary.state == "individual"
    assert not summary.cohort.is_comparable_cohort
    assert [run.run_id for run in summary.runs] == ["a-run", "b-run"]
    assert summary.equity_payload is None
    assert summary.cohort.period_start is None
    assert summary.cohort.period_end is None
    assert summary.cohort.base_currency is None
    assert [(item.reason, item.count) for item in summary.comparison_exclusions] == [
        ("period_mismatch", 1)
    ]


def test_single_strategy_is_shown_without_a_cross_strategy_curve(monkeypatch):
    _install_presenter_fakes(monkeypatch)
    completed = datetime(2025, 3, 1, tzinfo=timezone.utc)
    repo = _Repo((_result("only-run", "weinstein", "a", completed),))

    summary = StrategyOutcomeService(repo).build_summary()

    assert summary.state == "individual"
    assert len(summary.runs) == 1
    assert summary.equity_payload is None
    rendered = templates.get_template("_strategy_outcome_summary.html").render(
        outcome_summary=summary,
        pending_experiment_detail=None,
    )
    assert "No comparable multi-Strategy cohort was found" in rendered
    assert "outcomes are not ranked across rows" in rendered


def test_pending_experiment_remains_visible_when_outcome_summary_fails():
    experiment = SimpleNamespace(
        id="draft-1",
        status=SimpleNamespace(value="draft"),
        draft=SimpleNamespace(
            strategy_id="weinstein",
            strategy_api_version=2,
            strategy_source_digest="a" * 64,
            hypothesis="Keep the independently loaded draft visible.",
            baseline_run_id="baseline-1",
            parameter_name="exit_sma",
            baseline_value=150,
            proposed_value=100,
            metric=SimpleNamespace(value="max_drawdown"),
            expected_direction=SimpleNamespace(value="improve"),
            effect_summary="Compare drawdown.",
        ),
        draft_digest="b" * 64,
    )
    detail = SimpleNamespace(experiment=experiment, locked_manifest_json="{}")

    rendered = templates.get_template("_strategy_outcome_summary.html").render(
        outcome_summary=None,
        pending_experiment_detail=detail,
    )

    assert "The bounded Backtest outcome summary is unavailable." in rendered
    assert "Keep the independently loaded draft visible." in rendered
    assert 'action="/strategy-manager/experiments/draft-1/discard"' in rendered


def test_pinned_spy_is_not_relabelled_when_universe_is_not_exact_spy(monkeypatch):
    _install_presenter_fakes(monkeypatch)
    completed = datetime(2025, 3, 1, tzinfo=timezone.utc)
    repo = _Repo(
        (
            _result(
                "buy-run",
                "rtly-backtest-buy-and-hold",
                "a",
                completed,
                security_ids=("not-spy",),
            ),
            _result("wein-run", "weinstein", "a", completed),
        )
    )

    summary = StrategyOutcomeService(repo).build_summary()

    assert summary.selected_spy_handle is None
    assert all(not run.is_spy_reference for run in summary.runs)
    assert all(
        series["label"] != "SPY Buy and Hold"
        for series in summary.equity_payload["series"]
    )


def _service(repo, prices) -> StrategyOutcomeService:
    return StrategyOutcomeService(
        cast(BacktestRepository, repo), cast(EvidenceReader, prices)
    )


def _payload(summary) -> Any:
    assert summary.equity_payload is not None
    return summary.equity_payload


def _benchmark_prices() -> FakePrices:
    closes = (date(2025, 1, 2), date(2025, 1, 31), date(2025, 2, 28))
    return FakePrices(
        spy=spy_evidence(
            [(session, close, 0.0) for session, close in zip(closes, (125, 150, 175))],
            end="2025-03-31",
        ),
        fx=fx_evidence([(date(2025, 1, 2), 1.25)]),
    )


def _install_manifest_fx(monkeypatch, repo, revisions=("fx",), per_run=None):
    """Fake each run's manifest: its securities carry ``revisions`` (or the
    run's own entry in ``per_run``) as their ``fx_revision``."""
    import app.services.backtest.strategy_outcomes as outcomes

    by_run = per_run or {}
    repo.run_input_manifest_json = lambda digest: digest.removeprefix("manifest-")
    monkeypatch.setattr(
        outcomes,
        "read_run_input_manifest",
        lambda run_id: SimpleNamespace(
            securities=tuple(
                SimpleNamespace(fx_revision=revision)
                for revision in by_run.get(run_id, revisions)
            )
        ),
    )


def _render(summary):
    return templates.get_template("_strategy_outcome_summary.html").render(
        outcome_summary=summary, pending_experiment_detail=None
    )


def _pinned_cohort(base_currency="GBP", digests=("pin", "pin")):
    completed = datetime(2025, 3, 1, tzinfo=timezone.utc)
    return _Repo(
        tuple(
            _result(
                f"{strategy}-run",
                strategy,
                "a",
                completed,
                base_currency=base_currency,
                pin_digest=digest,
            )
            for strategy, digest in zip(("trend", "weinstein"), digests)
        )
    )


def test_pinned_gbp_cohort_appends_spy_total_return_benchmark(monkeypatch):
    _install_presenter_fakes(monkeypatch)
    repo = _pinned_cohort()
    _install_manifest_fx(monkeypatch, repo)

    summary = _service(repo, _benchmark_prices()).build_summary()

    assert summary.state == "cohort"
    assert summary.benchmark_note is None
    spy = _payload(summary)["series"][-1]
    assert spy["run_id"] is None
    assert spy["label"] == "SPY (benchmark)"
    assert spy["is_benchmark"] is True
    assert spy["values"] == (100.0, 120.0, 140.0)
    assert spy["equity_values"] == (10000.0, 12000.0, 14000.0)
    assert all(len(row["equities"]) == 3 for row in _payload(summary)["table_rows"])
    assert NO_SPY_REFERENCE_LIMITATION not in summary.cohort.limitations
    rendered = _render(summary)
    assert "Strategies vs SPY" in rendered
    assert "pinned GBP/USD rates" in rendered
    assert "SPY Buy and Hold Result" not in rendered


def test_single_pinned_run_is_charted_with_spy(monkeypatch):
    _install_presenter_fakes(monkeypatch)
    older = _result(
        "old-run", "weinstein", "a", datetime(2025, 2, 1, tzinfo=timezone.utc)
    )
    newest = _result(
        "new-run",
        "weinstein",
        "a",
        datetime(2025, 3, 1, tzinfo=timezone.utc),
        pin_digest="pin",
    )
    repo = _Repo((older, newest))

    summary = _service(repo, _benchmark_prices()).build_summary()

    assert summary.state == "individual"
    series = _payload(summary)["series"]
    assert [item["run_id"] for item in series] == ["new-run", None]
    assert series[1]["equity_values"] == (10000.0, 12000.0, 14000.0)
    assert summary.equity_mode == "currency"
    rendered = _render(summary)
    assert "Strategies vs SPY" in rendered
    assert "Values are shown in USD." in rendered


def test_single_unpinned_run_has_no_chart_and_a_note(monkeypatch):
    _install_presenter_fakes(monkeypatch)
    completed = datetime(2025, 3, 1, tzinfo=timezone.utc)
    repo = _Repo((_result("only-run", "weinstein", "a", completed),))

    summary = _service(repo, _benchmark_prices()).build_summary()

    assert summary.equity_payload is None
    assert summary.benchmark_note == "No pinned SPY evidence for this run."
    assert "No pinned SPY evidence for this run." in _render(summary)


def test_default_constructor_gives_no_benchmark_and_no_note(monkeypatch):
    _install_presenter_fakes(monkeypatch)
    repo = _pinned_cohort()

    summary = StrategyOutcomeService(cast(BacktestRepository, repo)).build_summary()

    assert summary.benchmark_note is None
    assert len(_payload(summary)["series"]) == 2
    assert NO_SPY_REFERENCE_LIMITATION in summary.cohort.limitations


@pytest.mark.parametrize(
    ("digests", "revisions", "per_run", "empty_prices", "note"),
    [
        ((None, "pin"), ("fx",), None, False, NO_PIN_NOTE),
        (("pin", "pin"), (None,), None, False, NO_USD_SECURITIES_NOTE),
        (("pin", "pin"), ("fx", "fx-2"), None, False, AMBIGUOUS_FX_NOTE),
        (("pin", "other"), ("fx",), None, False, MISMATCHED_PIN_NOTE),
        (
            ("pin", "pin"),
            ("fx",),
            {"weinstein-run": ("fx-2",)},
            False,
            MISMATCHED_FX_NOTE,
        ),
        (("pin", "pin"), ("fx",), None, True, BAD_EVIDENCE_NOTE),
    ],
    ids=[
        "unpinned",
        "gbp-only-universe",
        "ambiguous-fx",
        "mismatched-pins",
        "mismatched-fx",
        "bad-evidence",
    ],
)
def test_benchmark_failure_keeps_runs_and_sets_note(
    monkeypatch, digests, revisions, per_run, empty_prices, note
):
    _install_presenter_fakes(monkeypatch)
    repo = _pinned_cohort(digests=digests)
    _install_manifest_fx(monkeypatch, repo, revisions, per_run)
    prices = FakePrices() if empty_prices else _benchmark_prices()

    summary = _service(repo, prices).build_summary()

    assert summary.benchmark_note == note
    assert len(_payload(summary)["series"]) == 2
    assert not any(item.get("is_benchmark") for item in _payload(summary)["series"])
    assert NO_SPY_REFERENCE_LIMITATION in summary.cohort.limitations
    rendered = _render(summary)
    assert str(escape(note)) in rendered
    assert "Strategies vs SPY" not in rendered
    with_curve_error = summary.model_copy(update={"curve_error": "Broken curves."})
    assert str(escape(note)) not in _render(with_curve_error)


def test_existing_spy_buy_and_hold_run_suppresses_pinned_benchmark(monkeypatch):
    _install_presenter_fakes(monkeypatch)
    completed = datetime(2025, 3, 1, tzinfo=timezone.utc)
    repo = _Repo(
        (
            _result(
                "buy-run",
                "rtly-backtest-buy-and-hold",
                "a",
                completed,
                pin_digest="pin",
            ),
            _result("wein-run", "weinstein", "a", completed, pin_digest="pin"),
        )
    )

    summary = _service(repo, _benchmark_prices()).build_summary()

    labels = [item["label"] for item in _payload(summary)["series"]]
    assert labels[0] == "SPY Buy and Hold"
    assert "SPY (benchmark)" not in labels
    assert summary.benchmark_note is None
