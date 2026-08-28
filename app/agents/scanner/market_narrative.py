"""Market-narrative blurb: Claude summariser (#109 Phase 3) with a
deterministic evergreen fallback (#109 Phase 1).

``build_deterministic_narrative`` builds a plain-English ``MarketNarrative``
from sector-allocation deltas, portfolio sector weights, and the static FOMC
market-cycle context — no LLM and no network involved. ``build_llm_narrative``
builds the *same* ``MarketNarrative`` shape from a Claude Sonnet 5 summary of
those same structured inputs plus headline-level news context, run through
the hallucination guard (``app.agents.scanner.narrative_guard``). Callers
(``AlertAgent.send_summary_email``, the watchlist banner) only ever render a
``MarketNarrative`` and don't need to change based on which builder produced it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from app.agents.scanner.market_cycle import (
    PHASE_MID_CYCLE,
    PHASE_POST_FOMC,
    PHASE_PRE_FOMC_BLACKOUT,
)
from app.agents.scanner.narrative_guard import validate_against_context
from app.agents.scanner.news_context import gather_news_context
from app.core.config import MARKET_NARRATIVE_JSON
from app.integrations.anthropic_client import AnthropicNarrativeClient
from app.schemas.market_breadth import MarketBreadth
from app.schemas.market_cycle import MarketCycleContext
from app.schemas.market_narrative import MarketNarrative, MarketNarrativeSource
from app.schemas.news_context import NewsContext
from app.schemas.sector_allocation import (
    CongressionalBuy,
    PortfolioSectorWeight,
    SectorAllocationSnapshot,
)

if TYPE_CHECKING:
    from app.integrations.alpha_vantage import AlphaVantageClient

NARRATIVE_VERSION = "1"
"""Bump whenever the system prompt or either narrative builder changes.

Folded into the hashed digest dict, so a bump invalidates every cached
narrative without a separate explicit comparison.
"""

NARRATIVE_MAX_AGE = timedelta(hours=24)
"""Upper bound on how long a digest-matched narrative may be reused."""

NOT_ADVICE_NOTE = (
    "Informational only, not financial advice. Sector prevalence reflects "
    "scanner candidates, not a recommendation to buy or sell."
)

_PHASE_LABELS = {
    PHASE_PRE_FOMC_BLACKOUT: "in the pre-FOMC blackout window",
    PHASE_POST_FOMC: "just past an FOMC decision",
    PHASE_MID_CYCLE: "mid-cycle between FOMC meetings",
}

_TOP_N = 3


def _pct(share: float) -> str:
    """Format a 0..1 fraction as a whole-number percentage string."""
    return f"{share * 100:.0f}%"


def _prevalence_bullet(snapshot: SectorAllocationSnapshot) -> str | None:
    """Return the "most-prevalent sectors" bullet, or None if no candidates."""
    top_shares = snapshot.shares[:_TOP_N]
    if not top_shares:
        return None
    leaders = ", ".join(
        f"{s.sector} ({_pct(s.count_share)}, {_pct(s.strong_share)} at 7/10+)"
        for s in top_shares
    )
    return f"Most-prevalent scan sectors this run: {leaders}."


def _delta_bullets(snapshot: SectorAllocationSnapshot) -> list[str]:
    """Return gaining/losing week-on-week bullets, or a first-run note."""
    if not snapshot.deltas:
        return [
            "No prior ~7-day baseline yet — this is the first "
            "sector-prevalence snapshot."
        ]
    window = snapshot.lookback_days
    suffix = f" over the past ~{window}d" if window is not None else ""
    bullets: list[str] = []
    gainers = [d for d in snapshot.deltas if d.delta > 0][:_TOP_N]
    losers = [d for d in snapshot.deltas if d.delta < 0][:_TOP_N]
    if gainers:
        bullets.append(
            f"Gaining prevalence{suffix}: "
            + ", ".join(f"{d.sector} (+{_pct(d.delta)})" for d in gainers)
            + "."
        )
    if losers:
        bullets.append(
            f"Losing prevalence{suffix}: "
            + ", ".join(f"{d.sector} ({_pct(d.delta)})" for d in losers)
            + "."
        )
    return bullets


def _myb_bullet(snapshot: SectorAllocationSnapshot) -> str | None:
    """Return the "sectors with most multi-year breakouts" bullet, or None."""
    ranked = sorted(
        (s for s in snapshot.shares if s.myb_count > 0),
        key=lambda s: -s.myb_count,
    )[:_TOP_N]
    if not ranked:
        return None
    return (
        "Most multi-year breakouts by sector: "
        + ", ".join(f"{s.sector} ({s.myb_count})" for s in ranked)
        + "."
    )


def _breadth_bullet(breadth: MarketBreadth | None) -> str | None:
    """Return the S&P 500 market-breadth bullet, or None when unavailable."""
    if breadth is None:
        return None
    trend = (
        "rising"
        if breadth.trend_rising
        else "falling"
        if breadth.trend_rising is False
        else "flat"
    )
    divergence = " — breadth-divergence flag set" if breadth.bearish_signal else ""
    provenance = (
        "retrieved from cache"
        if breadth.retrieval_source == "cached"
        else "fetched from source"
    )
    return (
        f"S&P 500 breadth: {breadth.pct_above_200dma:.0f}% of members above "
        f"their 200DMA ({trend}){divergence}; {provenance}."
    )


def _congress_bullet(congress: list[CongressionalBuy]) -> str | None:
    """Return the "most heavily bought by lawmakers" bullet, or None if empty."""
    if not congress:
        return None
    names = ", ".join(f"{c.ticker} (+{c.congress_net})" for c in congress[:_TOP_N])
    return f"Most heavily net-bought by Congress/Senate: {names}."


def _portfolio_bullet(weights: list[PortfolioSectorWeight]) -> str | None:
    """Return the "current holdings concentrated in" bullet, or None if empty."""
    top_holdings = weights[:_TOP_N]
    if not top_holdings:
        return None
    return (
        "Current holdings concentrated in: "
        + ", ".join(f"{w.sector} ({_pct(w.value_share)})" for w in top_holdings)
        + "."
    )


def _cycle_bullet(cycle: MarketCycleContext) -> str:
    """Return the FOMC market-cycle context bullet."""
    phase_label = _PHASE_LABELS.get(cycle.phase, cycle.phase)
    bits = [f"Market is {phase_label}"]
    if cycle.days_since_last_decision is not None:
        bits.append(f"{cycle.days_since_last_decision}d since the last FOMC decision")
    if cycle.days_to_next_meeting is not None:
        bits.append(f"{cycle.days_to_next_meeting}d to the next meeting")
    return ", ".join(bits) + "."


def build_deterministic_narrative(
    scan_snapshot: SectorAllocationSnapshot,
    portfolio_weights: list[PortfolioSectorWeight],
    cycle: MarketCycleContext,
    breadth: MarketBreadth | None = None,
    congress: list[CongressionalBuy] | None = None,
) -> MarketNarrative:
    """Build the deterministic sector-allocation + market-cycle blurb.

    Fully rule-based: no API keys, no network, no LLM. This is the evergreen
    fallback rendered whenever a later, Claude-generated narrative isn't
    available. Breadth and congressional signals are folded in when supplied.
    """
    bullets: list[str] = []
    prevalence = _prevalence_bullet(scan_snapshot)
    if prevalence:
        bullets.append(prevalence)
    bullets.extend(_delta_bullets(scan_snapshot))
    for optional in (
        _myb_bullet(scan_snapshot),
        _breadth_bullet(breadth),
        _congress_bullet(congress or []),
        _portfolio_bullet(portfolio_weights),
    ):
        if optional:
            bullets.append(optional)
    bullets.append(_cycle_bullet(cycle))

    headline = (
        f"Scanner sector mix: {scan_snapshot.shares[0].sector} leads"
        if scan_snapshot.shares
        else "Sector-allocation snapshot"
    )

    return MarketNarrative(
        headline=headline,
        bullets=bullets,
        sources=[],
        not_advice=NOT_ADVICE_NOTE,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _sources_from_domains(
    cited_domains: list[str], news_context: NewsContext
) -> list[MarketNarrativeSource]:
    """Build the digest footnote list from the news items actually cited.

    One source per cited domain (first matching item wins), in the order
    Claude cited them — not every fed item, only the ones the guard
    confirmed were actually drawn on.
    """
    by_domain: dict[str, MarketNarrativeSource] = {}
    for item in news_context.items:
        domain = item.domain.strip().lower()
        domain = domain[4:] if domain.startswith("www.") else domain
        by_domain.setdefault(
            domain, MarketNarrativeSource(label=item.domain, url=item.url)
        )
    return [by_domain[d] for d in cited_domains if d in by_domain]


def build_llm_narrative(
    scan_snapshot: SectorAllocationSnapshot,
    portfolio_weights: list[PortfolioSectorWeight],
    cycle: MarketCycleContext,
    news_context: NewsContext,
    anthropic_client: AnthropicNarrativeClient,
    breadth: MarketBreadth | None = None,
    congress: list[CongressionalBuy] | None = None,
) -> MarketNarrative | None:
    """Attempt a Claude-generated ``MarketNarrative``; ``None`` on any degraded path.

    Returns ``None`` — signalling the caller to fall back to
    ``build_deterministic_narrative`` — when: the client has no API key
    configured, the news context came back degraded or empty (nothing to
    ground a summary in), the API call itself fails or is refused, or the
    hallucination guard rejects the draft outright (bad headline) or leaves
    no bullets standing. This must never raise: every failure mode here is
    absorbed into a ``None`` return, mirroring
    ``AnalystAgent.get_llm_client()``'s graceful-skip pattern.
    """
    if not anthropic_client.enabled:
        return None
    if news_context.degraded or not news_context.items:
        return None

    draft = anthropic_client.generate_market_narrative(
        scan_snapshot, portfolio_weights, cycle, news_context, breadth, congress
    )
    if draft is None:
        return None

    result = validate_against_context(draft, news_context)
    if not result.valid or not result.kept_bullets:
        return None

    return MarketNarrative(
        headline=draft.headline,
        bullets=result.kept_bullets,
        sources=_sources_from_domains(result.cited_domains, news_context),
        not_advice=NOT_ADVICE_NOTE,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


class _NonFiniteFloat(Exception):
    """Raised internally when a float that must be quantised is NaN/inf."""


def _quant(value: float, ndigits: int) -> float:
    """Round *value* to *ndigits*, normalising ``-0.0`` to ``0.0``.

    Raises :class:`_NonFiniteFloat` for NaN/inf so the caller can abort the
    digest rather than stamp one over corrupt data.
    """
    if not math.isfinite(value):
        raise _NonFiniteFloat
    return round(value, ndigits) + 0.0


def narrative_input_digest(
    sector_snapshot: SectorAllocationSnapshot,
    portfolio_weights: list[PortfolioSectorWeight],
    cycle: MarketCycleContext,
    breadth: MarketBreadth | None,
    congress: list[CongressionalBuy],
) -> str | None:
    """Return a sha256 hex digest over the narrative's semantic market facts.

    Builds an explicit canonical dict (never ``model_dump()`` wholesale),
    digesting each collection in its incoming order because the rendered
    narrative is order-sensitive (headline is ``shares[0]``, bullets slice
    ``[:3]``). Floats are quantised to the precision the prose renders. A
    non-finite float aborts the digest (returns ``None``), forcing a
    regeneration rather than caching corrupt data. Follows the local-sha256
    idiom in ``import_receipt_repo.canonical_row_digest``.
    """
    try:
        canonical = {
            "version": NARRATIVE_VERSION,
            "snapshot": {
                "total_candidates": sector_snapshot.total_candidates,
                "lookback_days": sector_snapshot.lookback_days,
                "shares": [
                    {
                        "sector": s.sector,
                        "count": s.count,
                        "count_share": _quant(s.count_share, 2),
                        "strong_count": s.strong_count,
                        "strong_share": _quant(s.strong_share, 2),
                        "myb_count": s.myb_count,
                    }
                    for s in sector_snapshot.shares
                ],
                "deltas": [
                    {"sector": d.sector, "delta": qd}
                    for d in sector_snapshot.deltas
                    if (qd := _quant(d.delta, 2)) != 0.0
                ],
            },
            "weights": [
                {
                    "sector": w.sector,
                    "count": w.count,
                    "value_share": _quant(w.value_share, 2),
                }
                for w in portfolio_weights
            ],
            "cycle": {"phase": cycle.phase},
            "breadth": None
            if breadth is None
            else {
                "as_of": breadth.as_of,
                "pct_above_200dma": _quant(breadth.pct_above_200dma, 0),
                "smoothed_8ma": None
                if breadth.smoothed_8ma is None
                else _quant(breadth.smoothed_8ma, 0),
                "trend_rising": breadth.trend_rising,
                "bearish_signal": breadth.bearish_signal,
                "retrieval_source": breadth.retrieval_source,
            },
            "congress": [
                {
                    "ticker": c.ticker,
                    "sector": c.sector,
                    "congress_net": c.congress_net,
                    "senate_net": c.senate_net,
                }
                for c in congress
            ],
        }
        raw = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (_NonFiniteFloat, ValueError, TypeError):
        # _NonFiniteFloat / ValueError: NaN or inf reached the digest.
        # TypeError: a non-numeric slipped into a quantised field. Either way,
        # abort rather than raise out of the narrative step — this module
        # degrades to a regeneration, it never crashes the pipeline.
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reusable_narrative(digest: str | None) -> MarketNarrative | None:
    """Return the stored narrative iff it is a safe verbatim reuse.

    Reuse requires: a truthy *digest*, a loadable stored narrative whose
    ``input_digest`` matches, that was not a deterministic fallback, and
    whose ``generated_at`` parses and is within ``NARRATIVE_MAX_AGE``. The
    version is gated implicitly — it is folded into *digest* — so it is not
    compared again here. Any failure is a miss (``None``).
    """
    if not digest:
        return None
    stored = load_market_narrative()
    if stored is None or stored.input_digest != digest or stored.from_fallback:
        return None
    try:
        generated_at = datetime.fromisoformat(stored.generated_at)
    except ValueError:
        return None
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - generated_at
    if age < timedelta(0) or age > NARRATIVE_MAX_AGE:
        return None
    return stored


def save_market_narrative(narrative: MarketNarrative) -> None:
    """Persist *narrative* atomically so re-renders reuse the same snapshot.

    Writes a process-unique temp sibling then ``os.replace``s it into place.
    The whole write is wrapped in ``try/except BaseException`` so an
    interrupt or ENOSPC cannot orphan a ``.tmp`` file in the source tree;
    the cleanup is itself nested so a failing ``unlink`` cannot mask the
    original error. Mirrors ``app/agents/extraction/heat_map_cache.py``.
    """
    MARKET_NARRATIVE_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = MARKET_NARRATIVE_JSON.with_name(
        f"{MARKET_NARRATIVE_JSON.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    try:
        tmp_path.write_text(
            json.dumps(narrative.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, MARKET_NARRATIVE_JSON)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def resolve_market_narrative(
    sector_snapshot: SectorAllocationSnapshot,
    portfolio_weights: list[PortfolioSectorWeight],
    cycle: MarketCycleContext,
    breadth: MarketBreadth | None,
    congress: list[CongressionalBuy],
    anthropic_client: AnthropicNarrativeClient,
    av_client: AlphaVantageClient | None,
) -> tuple[MarketNarrative, bool]:
    """Resolve the run's ``MarketNarrative``, reusing the cache when possible.

    Owns the whole decision: compute the digest, try ``reusable_narrative``,
    and on a miss run the news-fetch/LLM/deterministic-fallback ladder,
    stamp ``input_digest``/``narrative_version``/``from_fallback``, and save.
    Returns ``(narrative, reused)``. A reused narrative is returned verbatim
    (original ``generated_at`` preserved) and is **not** re-saved.
    """
    digest = narrative_input_digest(
        sector_snapshot, portfolio_weights, cycle, breadth, congress
    )
    reused = reusable_narrative(digest)
    if reused is not None:
        return reused, True

    narrative: MarketNarrative | None = None
    if anthropic_client.enabled:
        top_sectors = [s.sector for s in sector_snapshot.shares[:_TOP_N]]
        news_context = gather_news_context(top_sectors, av_client)
        narrative = build_llm_narrative(
            sector_snapshot,
            portfolio_weights,
            cycle,
            news_context,
            anthropic_client,
            breadth,
            congress,
        )
    from_fallback = narrative is None
    if narrative is None:
        narrative = build_deterministic_narrative(
            sector_snapshot, portfolio_weights, cycle, breadth, congress
        )

    stamped = narrative.model_copy(
        update={
            "input_digest": digest or "",
            "narrative_version": NARRATIVE_VERSION,
            "from_fallback": from_fallback,
        }
    )
    save_market_narrative(stamped)
    return stamped, False


def load_market_narrative() -> MarketNarrative | None:
    """Return the persisted narrative from the latest run, or None if absent."""
    try:
        payload = json.loads(MARKET_NARRATIVE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return MarketNarrative.model_validate(payload)
    except Exception:
        return None
