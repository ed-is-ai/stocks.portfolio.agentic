from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.repositories.backtest_repo import RecentBacktestResultsV1
from app.api.templating import templates
from app.services.backtest.metrics import MetricUnavailableReason
from app.services.backtest.strategy_outcomes import StrategyOutcomeService


class _Repo:
    def __init__(self, results):
        self.results = tuple(results)
        self.limit = None
        self.comparison_calls = []

    def recent_verified_backtest_results(self, *, limit):
        self.limit = limit
        return RecentBacktestResultsV1(
            self.results[:limit], min(len(self.results), limit), 0, 0, ()
        )

    def is_comparable(self, left, right, *, left_result=None, right_result=None):
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
    def is_comparable(self, left, right, *, left_result=None, right_result=None):
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

    def is_comparable(self, left, right, *, left_result=None, right_result=None):
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
        base_currency="USD",
        starting_capital=Decimal(starting_capital),
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
        events=(),
        equity_curve=tuple(
            SimpleNamespace(
                session=session,
                total_equity_base=Decimal(starting_capital) * Decimal(1 + index / 10),
            )
            for index, session in enumerate(sessions)
        ),
        candidate_audit_summary=SimpleNamespace(recorded=True, candidate_count=12),
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
