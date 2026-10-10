"""Build deterministic, bounded Strategy Manager outcome summaries."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Literal, cast
from urllib.parse import quote

from app.repositories.backtest_repo import (
    BacktestIntegrityError,
    BacktestRepository,
    BacktestResultV1,
)
from app.schemas.strategy_outcomes import (
    OutcomeCohortV1,
    OutcomeExclusionCountV1,
    OutcomeMetricsV1,
    OutcomeProvenanceV1,
    OutcomeRunV1,
    StrategyOutcomeSummaryV1,
)
from app.services.backtest.backtest_engine import (
    ExitFillEventV1,
    TerminalSettlementEventV1,
)
from app.services.backtest.result_presenter import (
    backtest_metrics_view,
    build_universe_view,
    multi_comparison_equity_payload,
    provenance_view,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutcomeSelectionV1:
    """Internal selection state retained only for one service call."""

    results: tuple[BacktestResultV1, ...]
    state: Literal["cohort", "individual", "empty", "unavailable"]
    comparison_exclusions: tuple[OutcomeExclusionCountV1, ...]
    cohort_candidate_count: int
    cohort_strategy_count: int


class StrategyOutcomeService:
    """Select a latest comparable cohort without changing comparison rules."""

    def __init__(self, repository: BacktestRepository):
        self._repository = repository

    def build_summary(self) -> StrategyOutcomeSummaryV1:
        try:
            page = self._repository.recent_verified_backtest_results(limit=25)
        except Exception:  # noqa: BLE001 - a summary failure must not hide landing
            logger.warning(
                "Verified Strategy outcomes could not be loaded", exc_info=True
            )
            cohort = OutcomeCohortV1(
                is_comparable_cohort=False,
                limitations=("Verified Backtest results are unavailable.",),
            )
            return StrategyOutcomeSummaryV1(
                state="unavailable",
                inspected_count=0,
                verified_count=0,
                cohort_candidate_count=0,
                cohort_strategy_count=0,
                integrity_excluded_count=0,
                missing_result_count=0,
                cohort=cohort,
                runs=(),
            )

        selection = self._select(page.results)
        display_results = selection.results
        if not page.results:
            cohort = self._cohort(selection, [])
            return StrategyOutcomeSummaryV1(
                state="empty",
                inspected_count=page.inspected_count,
                verified_count=0,
                cohort_candidate_count=0,
                cohort_strategy_count=0,
                integrity_excluded_count=page.integrity_excluded_count,
                missing_result_count=page.missing_result_count,
                job_exclusions=tuple(
                    OutcomeExclusionCountV1(reason=reason, count=count)
                    for reason, count in page.job_exclusion_counts
                ),
                runs=(),
                cohort=cohort,
            )
        roster_cache: dict[str, dict[str, tuple[str, str]]] = {}
        universe_cache: dict[tuple[str, str], tuple[str, ...] | None] = {}
        provenance_cache = {}
        spy_ids: set[str] = set()

        for result in display_results:
            profile_hash = result.profile_hash
            if profile_hash not in roster_cache:
                try:
                    roster_cache[profile_hash] = {
                        security_id: (symbol, mic)
                        for security_id, symbol, mic, _currency in self._repository.roster_member_identities(
                            profile_hash
                        )
                    }
                except BacktestIntegrityError:
                    roster_cache[profile_hash] = {}
            selection_pin = result.universe_selection
            ids = (
                None if selection_pin is None else selection_pin.canonical_security_ids
            )
            if (
                selection.state == "cohort"
                and result.strategy_id == "rtly-backtest-buy-and-hold"
                and ids is not None
                and len(ids) == 1
                and roster_cache[profile_hash].get(ids[0]) == ("SPY", "ARCX")
            ):
                spy_ids.add(result.run_id)

        handles = {
            result.run_id: f"R{index:02d}"
            for index, result in enumerate(display_results, start=1)
        }
        rows: list[OutcomeRunV1] = []
        for result in display_results:
            handle = handles[result.run_id]
            selection_pin = result.universe_selection
            ids = (
                None if selection_pin is None else selection_pin.canonical_security_ids
            )
            identities = roster_cache[result.profile_hash]
            if ids:
                runnable_key = (result.profile_hash, result.start_month)
                if runnable_key not in universe_cache:
                    try:
                        universe_cache[runnable_key] = tuple(
                            security_id
                            for security_id, _revision in self._repository.snapshot_member_revisions(
                                result.profile_hash, result.start_month
                            )
                        )
                    except BacktestIntegrityError:
                        universe_cache[runnable_key] = None
            universe = build_universe_view(
                ids,
                identities,
                runnable_ids=(
                    None
                    if not ids
                    else universe_cache.get((result.profile_hash, result.start_month))
                ),
            )
            try:
                coverage = provenance_cache.get(result.profile_hash)
                if coverage is None:
                    coverage = self._repository.snapshot_coverage(result.profile_hash)
                    provenance_cache[result.profile_hash] = coverage
                provenance = tuple(
                    OutcomeProvenanceV1(
                        quality=(
                            entry.provenance_quality
                            if entry.provenance_quality
                            in {
                                "observed_bau",
                                "best_effort_reconstructed",
                                "unavailable",
                            }
                            else "unavailable"
                        ),
                        snapshot_count=entry.snapshot_count,
                    )
                    for entry in provenance_view(result, coverage).entries
                )
            except (BacktestIntegrityError, ValueError):
                provenance = ()
            metric_availability = {
                "sharpe_ratio": (
                    None
                    if result.metric_availability.sharpe_unavailable is None
                    else result.metric_availability.sharpe_unavailable.value
                ),
                "win_rate": (
                    None
                    if result.metric_availability.win_rate_unavailable is None
                    else result.metric_availability.win_rate_unavailable.value
                ),
            }
            closed_trade_count = sum(
                isinstance(event, (ExitFillEventV1, TerminalSettlementEventV1))
                for event in result.events
            )
            parameters = dict(result.parameters)
            if selection_pin is not None:
                parameters.pop(selection_pin.universe_parameter, None)
            candidate_summary = result.candidate_audit_summary
            candidate_count = (
                candidate_summary.candidate_count
                if candidate_summary is not None and candidate_summary.recorded
                else None
            )
            metrics = OutcomeMetricsV1(
                total_return=result.metrics.total_return,
                sharpe_ratio=result.metrics.sharpe_ratio,
                win_rate=result.metrics.win_rate,
                max_drawdown=result.metrics.max_drawdown,
            )
            formatted_metrics = backtest_metrics_view(
                result.metrics, result.metric_availability
            )
            metric_display = {
                "total_return": formatted_metrics.total_return.value,
                "sharpe_ratio": formatted_metrics.sharpe_ratio.value,
                "win_rate": formatted_metrics.win_rate.value,
                "max_drawdown": formatted_metrics.max_drawdown.value,
            }
            row = OutcomeRunV1(
                run_id=result.run_id,
                evidence_handle=handle,
                result_url=f"/strategy-manager/results/{quote(result.run_id, safe='')}",
                strategy_id=result.strategy_id,
                strategy_api_version=result.strategy_api_version,
                strategy_source_digest=result.strategy_source_digest,
                completed_at=result.completed_at,
                start_month=result.start_month,
                end_month=result.end_month,
                base_currency=result.base_currency,
                starting_capital=str(result.starting_capital),
                profile_hash=result.profile_hash,
                parameters=parameters,
                universe=universe.tickers,
                metrics=metrics,
                metric_display=metric_display,
                metric_availability=metric_availability,
                closed_trade_count=closed_trade_count,
                equity_point_count=len(result.equity_curve),
                candidate_count=candidate_count,
                provenance=provenance,
                is_spy_reference=result.run_id in spy_ids,
            )
            rows.append(row)

        cohort = self._cohort(selection, rows)
        equity_payload = None
        equity_mode = "none"
        curve_error = None
        selected_spy_handle = next(
            (handles[run_id] for run_id in spy_ids if run_id in handles), None
        )
        if selection.state == "cohort" and len(display_results) >= 2:
            try:
                equity_payload = multi_comparison_equity_payload(display_results)
                dates = cast(tuple[str, ...], equity_payload["dates"])
                all_rows = cast(
                    tuple[dict[str, object], ...], equity_payload["table_rows"]
                )
                last_month_index: dict[str, int] = {}
                for index, trading_date in enumerate(dates):
                    last_month_index[str(trading_date)[:7]] = index
                month_end_indexes = set(last_month_index.values())
                equity_payload = {
                    **equity_payload,
                    "table_rows": tuple(
                        row
                        for index, row in enumerate(all_rows)
                        if index in month_end_indexes
                    ),
                }
                starting_capital = {
                    result.starting_capital for result in display_results
                }
                equity_mode = "indexed" if len(starting_capital) > 1 else "currency"
                if selected_spy_handle is not None:
                    renamed = []
                    for series in cast(
                        tuple[dict[str, object], ...], equity_payload["series"]
                    ):
                        if series["run_id"] in spy_ids:
                            series = {**series, "label": "SPY Buy and Hold"}
                        renamed.append(series)
                    equity_payload["series"] = tuple(renamed)
            except BacktestIntegrityError:
                curve_error = (
                    "Stored equity paths do not share a verified session sequence."
                )
                equity_payload = None

        return StrategyOutcomeSummaryV1(
            state=selection.state,
            inspected_count=page.inspected_count,
            verified_count=len(page.results),
            cohort_candidate_count=selection.cohort_candidate_count,
            cohort_strategy_count=selection.cohort_strategy_count,
            integrity_excluded_count=page.integrity_excluded_count,
            missing_result_count=page.missing_result_count,
            job_exclusions=tuple(
                OutcomeExclusionCountV1(reason=reason, count=count)
                for reason, count in page.job_exclusion_counts
            ),
            comparison_exclusions=selection.comparison_exclusions,
            runs=tuple(rows),
            cohort=cohort,
            equity_payload=equity_payload,
            equity_mode=equity_mode,
            curve_error=curve_error,
            selected_spy_handle=selected_spy_handle,
        )

    def _select(self, results: tuple[BacktestResultV1, ...]) -> OutcomeSelectionV1:
        if not results:
            return OutcomeSelectionV1((), "empty", (), 0, 0)

        by_strategy: dict[str, list[int]] = {}
        for index, result in enumerate(results):
            by_strategy.setdefault(result.strategy_id, []).append(index)
        strategy_groups = [
            sorted(
                indices,
                key=lambda index: (
                    -results[index].completed_at.timestamp(),
                    results[index].run_id,
                ),
            )
            for _strategy_id, indices in sorted(by_strategy.items())
        ]

        pair_facts: dict[tuple[int, int], tuple[bool, str | None]] = {}
        for left_index in range(len(results)):
            for right_index in range(left_index + 1, len(results)):
                left = results[left_index]
                right = results[right_index]
                try:
                    eligibility = self._repository.is_comparable(
                        left.run_id,
                        right.run_id,
                        left_result=left,
                        right_result=right,
                    )
                except (BacktestIntegrityError, ValueError):
                    pair_facts[(left_index, right_index)] = (False, "integrity_error")
                else:
                    pair_facts[(left_index, right_index)] = (
                        eligibility.eligible,
                        None
                        if eligibility.eligible
                        else (
                            eligibility.reason.value
                            if eligibility.reason is not None
                            else "ineligible"
                        ),
                    )

        def pair_is_eligible(left: int, right: int) -> bool:
            key = (min(left, right), max(left, right))
            return pair_facts[key][0]

        selected_indices: tuple[int, ...] = ()
        selected_strategy_count = 0
        selected_newest: datetime | None = None
        selected_run_ids: tuple[str, ...] = ()

        def consider(indices: list[int]) -> None:
            nonlocal selected_indices, selected_strategy_count
            nonlocal selected_newest, selected_run_ids
            if not indices:
                return
            newest = max(results[index].completed_at for index in indices)
            run_ids = tuple(sorted(results[index].run_id for index in indices))
            strategy_count = len({results[index].strategy_id for index in indices})
            if (
                strategy_count > selected_strategy_count
                or (
                    strategy_count == selected_strategy_count
                    and (selected_newest is None or newest > selected_newest)
                )
                or (
                    strategy_count == selected_strategy_count
                    and newest == selected_newest
                    and (not selected_run_ids or run_ids < selected_run_ids)
                )
            ):
                selected_indices = tuple(indices)
                selected_strategy_count = strategy_count
                selected_newest = newest
                selected_run_ids = run_ids

        def search(group_index: int, chosen: list[int]) -> None:
            possible_count = len(chosen) + len(strategy_groups) - group_index
            if possible_count < selected_strategy_count:
                return
            if group_index == len(strategy_groups):
                consider(chosen)
                return
            for candidate in strategy_groups[group_index]:
                if all(pair_is_eligible(candidate, previous) for previous in chosen):
                    search(group_index + 1, [*chosen, candidate])
            search(group_index + 1, chosen)

        search(0, [])
        selected_clique_indices = list(selected_indices)
        # Keep every pairwise-compatible candidate in the chosen cohort for
        # accurate coverage counts; the display still keeps only the newest
        # Result per Strategy.
        for index in range(len(results)):
            if index in selected_clique_indices:
                continue
            if all(
                pair_is_eligible(index, chosen) for chosen in selected_clique_indices
            ):
                selected_clique_indices.append(index)
        selected_clique = tuple(results[index] for index in selected_clique_indices)
        cross_strategy = selected_strategy_count > 1
        source_results = (
            self._latest_per_strategy(selected_clique)
            if cross_strategy
            else self._latest_per_strategy(results)
        )
        exclusions: Counter[str] = Counter()
        if selected_clique:
            selected_ids = {result.run_id for result in selected_clique}
            for index, result in enumerate(results):
                if result.run_id in selected_ids:
                    continue
                eligible = all(
                    pair_is_eligible(index, selected_index)
                    for selected_index in selected_clique_indices
                )
                if eligible:
                    continue
                reasons = [
                    pair_facts[
                        (min(index, selected_index), max(index, selected_index))
                    ][1]
                    for selected_index in selected_clique_indices
                    if not pair_is_eligible(index, selected_index)
                ]
                exclusions[next((item for item in reasons if item), "ineligible")] += 1
        state: Literal["cohort", "individual"] = (
            "cohort" if cross_strategy else "individual"
        )
        return OutcomeSelectionV1(
            results=tuple(source_results),
            state=state,
            comparison_exclusions=tuple(
                OutcomeExclusionCountV1(reason=reason, count=count)
                for reason, count in sorted(exclusions.items())
            ),
            cohort_candidate_count=len(selected_clique),
            cohort_strategy_count=selected_strategy_count,
        )

    @staticmethod
    def _latest_per_strategy(
        results: tuple[BacktestResultV1, ...],
    ) -> tuple[BacktestResultV1, ...]:
        newest: dict[str, BacktestResultV1] = {}
        for result in results:
            current = newest.get(result.strategy_id)
            if (
                current is None
                or result.completed_at > current.completed_at
                or (
                    result.completed_at == current.completed_at
                    and result.run_id < current.run_id
                )
            ):
                newest[result.strategy_id] = result
        return tuple(newest[key] for key in sorted(newest))

    @staticmethod
    def _cohort(
        selection: OutcomeSelectionV1,
        rows: list[OutcomeRunV1],
    ) -> OutcomeCohortV1:
        limitations: list[str] = [
            "These results describe historical tested configurations, not future returns or universal Strategy performance.",
            "The reconstructed universe may have survivorship bias; point-in-time membership is not guaranteed.",
            "Observed differences do not establish causation or isolate one Strategy rule.",
            "Job-status exclusion counts cover all retained Backtest jobs; the verified Result scan is capped at 25 candidates.",
        ]
        if (
            len(
                {
                    tuple(
                        sorted(
                            (name, repr(value))
                            for name, value in row.parameters.items()
                        )
                    )
                    for row in rows
                }
            )
            > 1
        ):
            limitations.append(
                "Tested parameter settings differ, so comparisons do not isolate one parameter change."
            )
        if not any(row.is_spy_reference for row in rows):
            limitations.append(
                "No pinned SPY reference is present in the selected outcome summary."
            )
        if selection.state != "cohort":
            limitations.append(
                "No multi-Strategy comparable cohort was selected; do not rank independent outcomes."
            )
        if any(
            item.closed_trade_count < 10 or item.candidate_count is None
            for item in rows
        ):
            limitations.append(
                "One or more result samples are small or have no candidate-audit count."
            )
        if any(
            not item.provenance
            or any(value.quality != "observed_bau" for value in item.provenance)
            for item in rows
        ):
            limitations.append(
                "Some runs have reconstructed or unavailable provenance; point-in-time membership is not established."
            )
        first = rows[0] if rows and selection.state == "cohort" else None
        return OutcomeCohortV1(
            is_comparable_cohort=selection.state == "cohort",
            period_start=None if first is None else first.start_month,
            period_end=None if first is None else first.end_month,
            base_currency=None if first is None else first.base_currency,
            limitations=tuple(dict.fromkeys(limitations)),
        )


__all__ = ["StrategyOutcomeService"]
