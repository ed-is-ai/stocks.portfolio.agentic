"""Sector prevalence + run-over-run delta computation (deterministic).

Mirrors the style of ``scan_history.py``: pure functions over
``list[StockRecord]``, persisted as a small sibling JSON store
(``SECTOR_ALLOCATION_HISTORY_JSON``) so deltas can be computed run over run
without entangling ``scan_history``'s ticker/zone snapshots.

Also includes ``compute_portfolio_sector_weights``, a pure function that
joins held positions to a ticker→sector map so the digest can contrast the
scan signal against current holdings.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

from app.core.config import SECTOR_ALLOCATION_HISTORY_JSON
from app.schemas.sector_allocation import (
    PortfolioSectorWeight,
    SectorAllocationSnapshot,
    SectorDelta,
    SectorShare,
)

if TYPE_CHECKING:
    from app.schemas import Position, StockRecord

_HISTORY_FILE = SECTOR_ALLOCATION_HISTORY_JSON
_MAX_SNAPSHOTS = 30
_UNKNOWN_SECTOR = "Unknown"


def compute_sector_prevalence(
    records: list[StockRecord],
) -> SectorAllocationSnapshot:
    """Return today's per-sector share of the current run's scored candidates.

    Missing/unset sectors are grouped under "Unknown" rather than dropped.
    ``count_share`` is a plain fraction of candidates; ``score_share`` weights
    each candidate by its analyst score (0 for records without an analysis).
    """
    today = date.today().isoformat()
    total = len(records)
    counts: dict[str, int] = {}
    scores: dict[str, int] = {}
    for record in records:
        sector = record.sector or _UNKNOWN_SECTOR
        counts[sector] = counts.get(sector, 0) + 1
        scores[sector] = scores.get(sector, 0) + (
            record.analysis.score if record.analysis else 0
        )
    total_score = sum(scores.values())
    shares = [
        SectorShare(
            sector=sector,
            count=count,
            count_share=(count / total) if total else 0.0,
            score_share=(scores[sector] / total_score) if total_score else 0.0,
        )
        for sector, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return SectorAllocationSnapshot(as_of=today, total_candidates=total, shares=shares)


def compute_sector_deltas(
    current: SectorAllocationSnapshot,
    prior: SectorAllocationSnapshot | None,
) -> list[SectorDelta]:
    """Return run-over-run count-share deltas, sorted by magnitude descending.

    Returns ``[]`` when there is no prior baseline (first run), mirroring how
    ``scan_history.get_new_tickers`` treats empty history.
    """
    if prior is None:
        return []
    prior_by_sector = {s.sector: s.count_share for s in prior.shares}
    current_by_sector = {s.sector: s.count_share for s in current.shares}
    all_sectors = set(prior_by_sector) | set(current_by_sector)
    deltas = [
        SectorDelta(
            sector=sector,
            prior_share=prior_by_sector.get(sector, 0.0),
            current_share=current_by_sector.get(sector, 0.0),
            delta=(
                current_by_sector.get(sector, 0.0) - prior_by_sector.get(sector, 0.0)
            ),
        )
        for sector in all_sectors
    ]
    return sorted(deltas, key=lambda d: -abs(d.delta))


def with_deltas(
    current: SectorAllocationSnapshot,
    prior: SectorAllocationSnapshot | None,
) -> SectorAllocationSnapshot:
    """Return *current* with ``deltas`` populated against *prior* (if any)."""
    return current.model_copy(update={"deltas": compute_sector_deltas(current, prior)})


def load_sector_history() -> dict[str, SectorAllocationSnapshot]:
    """Return {date_str: snapshot} for all past runs."""
    try:
        raw: dict[str, dict] = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, SectorAllocationSnapshot] = {}
    for dt, payload in raw.items():
        try:
            result[dt] = SectorAllocationSnapshot.model_validate(payload)
        except Exception:
            continue
    return result


def latest_snapshot(
    history: dict[str, SectorAllocationSnapshot],
) -> SectorAllocationSnapshot | None:
    """Return the most recent snapshot in *history*, or None if empty."""
    if not history:
        return None
    return history[max(history.keys())]


def save_sector_snapshot(
    snapshot: SectorAllocationSnapshot,
    history: dict[str, SectorAllocationSnapshot],
) -> None:
    """Append *snapshot* under its ``as_of`` date and write to disk.

    Caps the store at ``_MAX_SNAPSHOTS`` entries, dropping the oldest first,
    mirroring ``scan_history.save_history``.
    """
    history[snapshot.as_of] = snapshot
    if len(history) > _MAX_SNAPSHOTS:
        for key in sorted(history.keys())[:-_MAX_SNAPSHOTS]:
            del history[key]
    payload = {k: v.model_dump(mode="json") for k, v in history.items()}
    _HISTORY_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def compute_portfolio_sector_weights(
    positions: list[Position],
    ticker_sector: dict[str, str],
    gbpusd: float,
) -> list[PortfolioSectorWeight]:
    """Return held-portfolio sector weights (GBP value share) for the digest.

    Values are converted to GBP using *gbpusd* (USD positions divided by the
    rate) so cross-currency holdings don't distort the aggregate, mirroring
    ``PortfolioService.gbp_totals``. A holding whose sector is unknown (not
    present in *ticker_sector*) is grouped under "Unknown" rather than
    dropped. Positions with no priced value are skipped.
    """
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for position in positions:
        if position.current_value is None:
            continue
        value = (
            position.current_value / gbpusd
            if position.price_currency == "USD"
            else position.current_value
        )
        sector = ticker_sector.get(position.ticker, _UNKNOWN_SECTOR)
        totals[sector] = totals.get(sector, 0.0) + value
        counts[sector] = counts.get(sector, 0) + 1
    grand_total = sum(totals.values())
    return [
        PortfolioSectorWeight(
            sector=sector,
            count=counts[sector],
            value_share=(value / grand_total) if grand_total else 0.0,
        )
        for sector, value in sorted(totals.items(), key=lambda kv: -kv[1])
    ]
