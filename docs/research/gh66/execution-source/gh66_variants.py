"""Research-only Skill-output variants for the GH #66 ranking experiment.

This module is deliberately outside ``skills/`` and the production engine.
It changes only emitted BUY priorities; eligibility and the selected
Buy-and-Hold basket remain owned by their original Skill.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal

from app.services.backtest.strategy_explanation import (
    EvidenceUnit,
    ExplanationFactV1,
    SignalExplanationV1,
    SignalReasonV1,
)
from app.services.backtest.strategy_protocol import (
    InitialEntrySelectionProviderV1,
    InitialEntrySelectionV1,
    MarketViewV1,
    PortfolioView,
    Signal,
    StrategyParameters,
    StrategyProtocolV1,
)

RankingVariant = Literal["legacy", "proposed", "random"]
RANDOM_ORDER_VERSION = "gh66-random-order-v1"
MINERVINI = "rtly-backtest-minervini"
BUY_AND_HOLD = "rtly-backtest-buy-and-hold"
UNRANKED_LEGACY = frozenset(
    {
        "rtly-backtest-weinstein",
        "rtly-backtest-darvas-box",
        "rtly-backtest-turtle-trend",
        "rtly-backtest-moving-average",
    }
)


def _raw_vcp_score(signal: Signal) -> Decimal:
    """Read Minervini's Skill-authored raw score for the actual old policy."""
    if signal.explanation is not None:
        for reason in signal.explanation.reasons:
            for fact in reason.facts:
                if fact.label == "Raw VCP score" and isinstance(fact.observed, Decimal):
                    return fact.observed
    raise ValueError(
        f"{MINERVINI} signal {signal.security_id} has no raw VCP score evidence"
    )


def _random_order_key(seed: int, session: date, security_id: str) -> str:
    """Stable per-seed/session/security ordering without Python ``hash()``."""
    material = (
        f"{RANDOM_ORDER_VERSION}\0{seed}\0{session.isoformat()}\0{security_id}"
    ).encode("utf-8")
    return sha256(material).hexdigest()


def _without_skill_ranking(signal: Signal) -> Signal:
    """Remove proposed-rank text when a control changes that policy."""
    if signal.explanation is None:
        return signal
    reasons = tuple(
        reason
        for reason in signal.explanation.reasons
        if reason.code not in {"entry_ranking", "research_random_order"}
    )
    explanation = SignalExplanationV1(reasons=reasons) if reasons else None
    return signal.model_copy(update={"explanation": explanation})


def _research_order_reason(
    *, seed: int, rank: int, count: int, priority: Decimal
) -> SignalReasonV1:
    return SignalReasonV1(
        code="research_random_order",
        summary=f"Research-only random ordering: {rank} of {count} for seed {seed}.",
        facts=(
            ExplanationFactV1(label="Ordering policy", observed=RANDOM_ORDER_VERSION),
            ExplanationFactV1(label="Seed", observed=Decimal(seed)),
            ExplanationFactV1(
                label="Random ordinal rank",
                observed=Decimal(rank),
                unit=EvidenceUnit.COUNT,
            ),
            ExplanationFactV1(
                label="Candidate count",
                observed=Decimal(count),
                unit=EvidenceUnit.COUNT,
            ),
            ExplanationFactV1(
                label="Encoded priority", observed=priority, unit=EvidenceUnit.SCORE
            ),
        ),
    )


def transform_signal_batch(
    strategy_id: str,
    signals: list[Signal] | tuple[Signal, ...],
    *,
    variant: RankingVariant,
    seed: int | None = None,
) -> list[Signal]:
    """Apply one frozen research policy to a complete Skill BUY batch."""
    if variant == "proposed":
        return list(signals)

    if variant == "legacy":
        if strategy_id == MINERVINI:
            return [
                _without_skill_ranking(signal).model_copy(
                    update={"priority": _raw_vcp_score(signal)}
                )
                for signal in signals
            ]
        if strategy_id in UNRANKED_LEGACY:
            return [
                _without_skill_ranking(signal).model_copy(update={"priority": None})
                for signal in signals
            ]
        if strategy_id == BUY_AND_HOLD:
            return list(signals)
        raise ValueError(f"no GH #66 legacy policy for strategy {strategy_id!r}")

    if variant != "random":
        raise ValueError(f"unknown GH #66 ranking variant {variant!r}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("random ranking variant requires an integer seed")
    if strategy_id not in {MINERVINI, BUY_AND_HOLD, *UNRANKED_LEGACY}:
        raise ValueError(f"no GH #66 random policy for strategy {strategy_id!r}")

    by_session: dict[date, list[Signal]] = defaultdict(list)
    for signal in signals:
        by_session[signal.session].append(signal)

    random_ranks: dict[tuple[date, str, str], tuple[Decimal, int, int]] = {}
    for session, cohort in by_session.items():
        ordered = sorted(
            cohort,
            key=lambda signal: (
                _random_order_key(seed, session, signal.security_id),
                signal.security_id,
                signal.rule_id,
            ),
        )
        random_ranks.update(
            {
                (signal.session, signal.security_id, signal.rule_id): (
                    Decimal(len(ordered) - rank),
                    rank + 1,
                    len(ordered),
                )
                for rank, signal in enumerate(ordered)
            }
        )
    ranked: list[Signal] = []
    for signal in signals:
        priority, rank, count = random_ranks[
            (signal.session, signal.security_id, signal.rule_id)
        ]
        original = _without_skill_ranking(signal)
        reasons = () if original.explanation is None else original.explanation.reasons
        explanation = SignalExplanationV1(
            reasons=(
                *reasons,
                _research_order_reason(
                    seed=seed, rank=rank, count=count, priority=priority
                ),
            )
        )
        ranked.append(
            original.model_copy(
                update={"priority": priority, "explanation": explanation}
            )
        )
    return ranked


class ResearchRankingAdapter:
    """Proxy a Skill while changing only its ordinary entry BUY batch."""

    def __init__(
        self,
        strategy: StrategyProtocolV1,
        strategy_id: str,
        variant: RankingVariant,
        seed: int | None = None,
        *,
        entry_signal_cache: dict[date, tuple[Signal, ...]] | None = None,
        initial_selection_cache: dict[date, InitialEntrySelectionV1] | None = None,
    ) -> None:
        self._strategy = strategy
        self._strategy_id = strategy_id
        self._variant = variant
        self._seed = seed
        self._entry_signal_cache = (
            entry_signal_cache if entry_signal_cache is not None else {}
        )
        self._initial_selection_cache = (
            initial_selection_cache if initial_selection_cache is not None else {}
        )
        self.entry_signal_cache_hits = 0
        self.entry_signal_cache_misses = 0
        self.initial_selection_cache_hits = 0
        self.initial_selection_cache_misses = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._strategy, name)

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        session = view.as_of_session
        if session not in self._entry_signal_cache:
            self.entry_signal_cache_misses += 1
            self._entry_signal_cache[session] = tuple(
                self._strategy.entry_signals(view, parameters)
            )
        else:
            self.entry_signal_cache_hits += 1
        return transform_signal_batch(
            self._strategy_id,
            self._entry_signal_cache[session],
            variant=self._variant,
            seed=self._seed,
        )

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        return self._strategy.exit_signals(view, portfolio, parameters)

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int | Decimal:
        return self._strategy.position_size(signal, view, portfolio, parameters)


class ResearchInitialSelectionAdapter(ResearchRankingAdapter):
    """Adapter that preserves Buy & Hold's optional initial-selection API."""

    def initial_entry_selection(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> InitialEntrySelectionV1:
        session = view.as_of_session
        selection = self._initial_selection_cache.get(session)
        if selection is None:
            self.initial_selection_cache_misses += 1
            selection = self._strategy.initial_entry_selection(  # type: ignore[attr-defined]
                view, parameters
            )
            self._initial_selection_cache[session] = selection
        else:
            self.initial_selection_cache_hits += 1
        signals = transform_signal_batch(
            self._strategy_id,
            selection.signals,
            variant=self._variant,
            seed=self._seed,
        )
        return selection.model_copy(update={"signals": signals})


def research_variant_adapter(
    strategy: StrategyProtocolV1,
    strategy_id: str,
    variant: RankingVariant,
    seed: int | None = None,
    *,
    entry_signal_cache: dict[date, tuple[Signal, ...]] | None = None,
    initial_selection_cache: dict[date, InitialEntrySelectionV1] | None = None,
) -> ResearchRankingAdapter:
    """Wrap a loaded runtime without advertising a new production Skill."""
    adapter_type = (
        ResearchInitialSelectionAdapter
        if isinstance(strategy, InitialEntrySelectionProviderV1)
        else ResearchRankingAdapter
    )
    return adapter_type(
        strategy,
        strategy_id,
        variant,
        seed,
        entry_signal_cache=entry_signal_cache,
        initial_selection_cache=initial_selection_cache,
    )


__all__ = [
    "BUY_AND_HOLD",
    "MINERVINI",
    "RANDOM_ORDER_VERSION",
    "RankingVariant",
    "ResearchRankingAdapter",
    "ResearchInitialSelectionAdapter",
    "research_variant_adapter",
    "transform_signal_batch",
]
