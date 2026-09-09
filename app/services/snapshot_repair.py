"""Idempotent repair of zero-valued historical portfolio snapshots (#466).

Before #466 a snapshot whose holdings could not be priced was persisted as a
plausible-looking ``total_value = 0.00``, which the value-history chart drew
as a real crash to zero. This pass finds those rows -- and the ``NULL`` rows
an earlier pass wrote, which new evidence may since have made valuable --
and either reconstructs them from *dated historical evidence* or leaves them
as ``NULL``, an honest gap.

It never uses current prices, never guesses an FX rate, and never deletes a
row. Only rows whose portfolio actually held something at that timestamp are
candidates: a cash-only portfolio's ``0.00`` is correct and is left
byte-identical.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
import logging
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from app.core.quantity import QUANTITY_EPSILON
from app.repositories.portfolio_snapshots_repo import PortfolioSnapshotsRepository
from app.repositories.trades_repo import TradesRepository
from app.services.backtest.historical_price_evidence import FX_PAIR
from app.services.snapshot_price_backfill import (
    PriceEvidenceBackfillService,
    PriceEvidenceUnavailable,
)

logger = logging.getLogger(__name__)

#: A stored ``total_value`` at or below this magnitude is treated as the
#: defective "zero" the bug wrote, not as a meaningful valuation.
_ZERO_TOLERANCE = 0.005


def first_trade_dates(replay_rows: list[tuple[Any, ...]]) -> dict[str, str]:
    """Return ``{ticker: earliest replay date}`` in one pass over the rows.

    An opening-lot row is a trade row like any other, dated at its own
    entry date -- there is no earlier "real" purchase to prefer over it.
    Shared by :class:`SnapshotRepairService` and the snapshot backfill
    service so both replay trades identically (#502).
    """
    first: dict[str, str] = {}
    for row in replay_rows:
        ticker, trade_date = row[0], str(row[4])[:10]
        if ticker not in first or trade_date < first[ticker]:
            first[ticker] = trade_date
    return first


def last_trade_dates(replay_rows: list[tuple[Any, ...]]) -> dict[str, str]:
    """Return ``{ticker: latest replay date}`` in one pass over the rows.

    The mirror of :func:`first_trade_dates`, used by the snapshot backfill to
    cap a sold-and-gone position's evidence prefetch at the day it was last
    traded instead of chasing it to the fill window's end (#502).
    """
    last: dict[str, str] = {}
    for row in replay_rows:
        ticker, trade_date = row[0], str(row[4])[:10]
        if ticker not in last or trade_date > last[ticker]:
            last[ticker] = trade_date
    return last


def gbp_replay_rows(
    replay_rows: list[tuple[Any, ...]],
    gbp_rate: Callable[[str, str], float | None],
) -> list[tuple[Any, ...]]:
    """Return ``replay_rows`` with every price converted to GBP major units (#549).

    A replay row's 8th column is the ticker's resolved trading currency
    (``TradesRepository.open_rows``); its ``price`` is in that currency, not
    in GBP, so replaying ``shares * price`` as sterling overstated a foreign
    holding's cost basis by its FX rate -- 9988 carried ~£54k against ~£4.5k
    of market value, spiking the Portfolio Value line on every day it was
    unpriced.

    Conversion is ``price / gbp_rate(currency, trade_date)`` (the rate being
    units of the currency per GBP) through stored evidence only, never a live
    fetch. Whatever bound ``gbp_rate`` applies applies here: since #550 that
    is the trade date or the few days before it, which is far better evidence
    of what a trade cost than discarding its cost basis over an unpublished
    rate. A non-GBP row with no rate inside that bound keeps every other field
    but gets ``price = None``: the cost or cash it feeds is then *unavailable*,
    never a GBP-assumed figure.

    Rows already in GBP (and rows with no currency column at all, e.g. a
    hand-built tuple in a test) pass through untouched, so an all-GBP
    portfolio is byte-identical to before. Rates are memoised per
    ``(currency, date)``, so a multi-year replay costs one lookup per
    distinct pair-day however many trades share it.
    """
    rates: dict[tuple[str, str], float | None] = {}
    converted: list[tuple[Any, ...]] = []
    for row in replay_rows:
        currency = str(row[7]).strip().upper() if len(row) > 7 and row[7] else "GBP"
        if currency == "GBP":
            converted.append(row)
            continue
        trade_date = str(row[4])[:10]
        key = (currency, trade_date)
        if key not in rates:
            rates[key] = gbp_rate(currency, trade_date)
        rate = rates[key]
        price = (
            None
            if rate is None or rate <= 0 or row[3] is None
            else float(row[3]) / rate
        )
        converted.append((*row[:3], price, *row[4:]))
    return converted


def position_cost_basis_as_of(
    replay_rows: list[tuple[Any, ...]], as_of: str
) -> dict[str, float | None]:
    """Return ``{ticker: GBP carrying cost}`` for positions open on ``as_of`` (#519).

    Average-cost replay over trades dated on or before ``as_of``, matching
    ``TraderAgent._compute_positions`` exactly -- a sell reduces the position
    at the running average and leaves the average untouched, and a fully
    closed position resets to zero. Prices are taken as GBP major units, so
    callers holding raw repository rows must first run them through
    :func:`gbp_replay_rows` (#549); a row whose price that conversion could
    not evidence arrives here as ``None`` and makes its ticker's cost
    ``None`` -- unavailable, never a GBP-assumed number -- while still
    moving the position's share count, which is known regardless of FX.
    Only a BUY can do that: a sell's price never enters an average cost, and
    closing the position out clears the mark with it.

    This is the single replay both the total cost basis (:func:`cost_basis_as_of`,
    its sum) and the estimated valuation of an unpriceable holding are built
    from, so a snapshot's ``total_cost`` and its estimated legs cannot drift
    apart. Values are unrounded; callers round their own aggregate.
    """
    state: dict[str, dict[str, float]] = {}
    unpriced: set[str] = set()
    for row in replay_rows:
        ticker, action, shares, price, trade_date = (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
        )
        if str(trade_date)[:10] > as_of:
            continue
        held = state.setdefault(ticker, {"shares": 0.0, "avg_cost": 0.0})
        quantity = float(shares)
        if action == "BUY":
            if price is None:
                unpriced.add(ticker)
                held["shares"] += quantity
                continue
            total = held["avg_cost"] * held["shares"] + float(price) * quantity
            held["shares"] += quantity
            held["avg_cost"] = total / held["shares"] if held["shares"] else 0.0
        else:
            # A sell's price never enters an average cost, so an unconvertible
            # one costs this replay nothing (#549) -- only a BUY can poison a
            # position's cost.
            held["shares"] -= quantity
            if held["shares"] <= QUANTITY_EPSILON:
                held["shares"] = 0.0
                held["avg_cost"] = 0.0
                # The position is gone, and with it every unconvertible buy
                # that made it unpriceable: a later reopen is judged on its
                # own trades, not on a closed lot's missing FX rate.
                unpriced.discard(ticker)
    return {
        ticker: None if ticker in unpriced else held["avg_cost"] * held["shares"]
        for ticker, held in state.items()
        if held["shares"] > QUANTITY_EPSILON
    }


def cost_basis_as_of(replay_rows: list[tuple[Any, ...]], as_of: str) -> float | None:
    """Return the GBP cost basis of positions open on ``as_of`` (#514).

    The sum of :func:`position_cost_basis_as_of`, so a backfilled row's
    ``total_cost`` is computed on exactly the same basis as a live one, and
    on the same basis as the carrying-cost fallback used for an unpriceable
    holding (#519). ``None`` when any open position's own cost is
    unavailable (#549): a total missing one leg is not a smaller total, it
    is a wrong one, and the caller writes it as NULL.
    """
    costs = position_cost_basis_as_of(replay_rows, as_of).values()
    if any(cost is None for cost in costs):
        return None
    return round(sum(cost for cost in costs if cost is not None), 2)


def net_trade_cash(
    replay_rows: list[tuple[Any, ...]], start_exclusive: str, end_inclusive: str
) -> float | None:
    """Return the net cash trades moved in ``(start, end]`` (#543).

    ``Σ(SELL shares*price) − Σ(BUY shares*price)`` over replay rows dated in
    the half-open interval -- a sale puts cash *in*, a purchase takes it
    *out*. Prices must already be GBP major units (:func:`gbp_replay_rows`,
    #549), the same convention :func:`position_cost_basis_as_of` uses, so
    the result is GBP. The live writer reaches GBP by its own route -- it
    converts a whole position's cost at *today's* rate rather than each
    trade's dated one -- so the two agree on units, not to the penny. A row in the interval
    whose price could not be converted (``None``) makes the whole interval
    ``None``: the day's cash is then NULL rather than short by one trade.

    The interval excludes its start so it composes with a dated statement
    anchor: the anchor already states the balance *after* everything that
    happened on its own day. Returns ``0.0`` for an empty interval, and a
    reversed interval (``start >= end``) is empty by construction.
    """
    total = 0.0
    for row in replay_rows:
        action, shares, price, trade_date = row[1], row[2], row[3], str(row[4])[:10]
        if not (start_exclusive < trade_date <= end_inclusive):
            continue
        if price is None:
            return None
        proceeds = float(shares) * float(price)
        # "not BUY is a sell" mirrors :func:`holdings_as_of` exactly, so the
        # shares leaving the position and the cash arriving for them can
        # never disagree about what a row is.
        total += -proceeds if action == "BUY" else proceeds
    return total


def holdings_as_of(replay_rows: list[tuple[Any, ...]], as_of: str) -> dict[str, float]:
    """Return ``{ticker: net shares}`` from trades dated on/before ``as_of``.

    Replay columns are ``(ticker, action, shares, price, date, ...)``.
    Tickers whose net position is flat (or short) are omitted. Shared by
    :class:`SnapshotRepairService` and the snapshot backfill service (#502).
    """
    net: dict[str, float] = {}
    for row in replay_rows:
        ticker, action, shares, _price, trade_date = (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
        )
        if str(trade_date)[:10] > as_of:
            continue
        delta = float(shares) if action == "BUY" else -float(shares)
        net[ticker] = net.get(ticker, 0.0) + delta
    return {t: s for t, s in net.items() if s > QUANTITY_EPSILON}


def _is_weekend(as_of: str) -> bool:
    """True when ``as_of`` is a Saturday or Sunday (#550).

    Reads the date part only, so a stored snapshot timestamp answers the
    same as a bare day, and never raises on a malformed value -- a
    multi-year pass must not abort on one bad row.
    """
    try:
        return date.fromisoformat(as_of[:10]).weekday() >= 5
    except ValueError:
        return False


def market_was_closed(
    source: "HistoricalGbpPriceSource",
    holdings: dict[str, float],
    as_of: str,
    trading_days: frozenset[str],
) -> bool:
    """True when the market was shut on ``as_of``, so nothing could move (#547).

    A Saturday or Sunday is closed unconditionally whenever the portfolio
    holds anything -- no calendar, no price check (#550). The two-condition
    test below was defeated on 25 of portfolio 19's weekends by a single
    holding reporting a stray weekend NAV or a stale Friday close, and those
    days then neither carried forward nor resolved. No equity market the app
    tracks opens at the weekend, so there is no ambiguity to weigh: the
    evidence is wrong, not the calendar.

    On a weekday the ambiguity is real, so two independent evidence failures
    are required, because either alone is not enough:

    * ``as_of`` is not in ``trading_days`` -- the FX calendar, which the app
      already maintains across the whole window, published nothing that day;
    * and *no* held ticker has a dated close.

    One holding missing a close on a trading day is a genuine data gap, not
    a closure: that day still gets valued with the unpriceable holding
    carried at cost and flagged estimated (#519), and silencing it here
    would hide the very gap that flag exists to show. Conversely a thin FX
    calendar alone must not freeze a real trading day's valuation.

    An empty ``trading_days`` means the calendar is unknown, so this always
    returns False -- a checkout with no FX evidence writes honest gaps
    exactly as it did before, rather than declaring every day a holiday.

    The ticker scan short-circuits on the first priced holding, and only
    runs at all on a day the calendar has already flagged.

    Only the date part of ``as_of`` is read, so a stored snapshot key
    (``2024-01-06T00:00:00+00:00``) classifies the same as a bare day; a
    malformed one never raises, it simply falls through to the weekday path,
    which is what this did before #550.
    """
    if not holdings:
        return False
    if _is_weekend(as_of):
        return True
    if not trading_days or as_of in trading_days:
        return False
    return not any(source.gbp_price(ticker, as_of) is not None for ticker in holdings)


def value_holdings(
    source: "HistoricalGbpPriceSource",
    holdings: dict[str, float],
    as_of: str,
    carrying: dict[str, float | None],
) -> tuple[float | None, bool]:
    """Value ``holdings`` at ``as_of``, returning ``(value, is_estimated)`` (#519).

    A holding with a dated GBP close is always valued from that evidence. A
    holding with none falls back to its carrying cost from ``carrying``
    (``{ticker: GBP cost}``, from :func:`position_cost_basis_as_of`), and the
    result is flagged estimated. A ``None`` carrying cost -- a foreign trade
    with no dated FX rate to convert it (#549) -- counts as no carrying cost
    at all, so the point is unavailable rather than estimated from a figure
    in the wrong currency. Passing an empty ``carrying`` disables estimation
    and restores the pre-#519 all-or-nothing rule: one unpriced holding makes
    the whole point unavailable.

    A value that rounds to ``0.00`` is reported as unavailable either way --
    writing it back would recreate the very row the repair pass exists to
    remove, and would stop a re-run being a no-op.

    Shared by :class:`SnapshotRepairService` and the snapshot backfill service
    so the two cannot drift apart.
    """
    total = 0.0
    estimated = False
    for ticker, shares in holdings.items():
        price = source.gbp_price(ticker, as_of)
        if price is None:
            cost = carrying.get(ticker)
            if cost is None:
                return None, False
            total += cost
            estimated = True
            continue
        total += shares * price
    value = round(total, 2)
    return (None, False) if value == 0.0 else (value, estimated)


class HistoricalGbpPriceSource(Protocol):
    """Supplies a dated, GBP-denominated close for one holding.

    Implementations must return None whenever they have no *evidence* for
    ``ticker`` on ``as_of`` -- an approximation, a nearby date, or a
    current price is never an acceptable substitute.
    """

    def gbp_price(self, ticker: str, as_of: str) -> float | None:
        """Return the GBP close for ``ticker`` on ``as_of`` (YYYY-MM-DD)."""
        ...

    def gbp_rate(self, currency: str, as_of: str) -> float | None:
        """Return units of ``currency`` per GBP on ``as_of``, or None (#514).

        Same evidence-only contract as :meth:`gbp_price`: an exact-date
        stored rate or nothing. ``GBP`` is 1.0 by definition.
        """
        ...

    def trading_days(self, start: str, end: str) -> frozenset[str]:
        """Return the days in ``[start, end]`` known to be trading days (#547).

        An empty set means "cannot tell", never "the market never opened":
        a caller must not conclude a market was closed from silence here.
        """
        ...


class NoHistoricalPriceSource:
    """The deliberate opt-out: reconstruct nothing, null everything.

    Dated per-ticker closes *are* reachable -- ``historical_price_cache.db``
    keys its revisions by ``requested_symbol``, which the alias map maps a
    portfolio ticker onto (see
    :class:`app.services.snapshot_price_evidence.HistoricalCacheGbpPriceSource`,
    #481). This source is what a caller injects when it wants every
    candidate row turned into an honest gap regardless of the evidence on
    hand (the CLI's ``--no-historical-evidence``), and what tests use to
    exercise the no-evidence path.
    """

    def gbp_price(self, ticker: str, as_of: str) -> float | None:
        """Return None -- this source has no historical evidence."""
        return None

    def gbp_rate(self, currency: str, as_of: str) -> float | None:
        """Return 1.0 for GBP, else None -- no evidence to convert with."""
        return 1.0 if currency.strip().upper() == "GBP" else None

    def trading_days(self, start: str, end: str) -> frozenset[str]:
        """Return an empty set -- this source knows no market calendar."""
        return frozenset()


class SnapshotRepairReport(BaseModel):
    """Counts of what one repair pass did (or, in a dry run, would do).

    ``repaired``, ``marked_unavailable`` and ``unchanged`` partition
    ``scanned``. ``candidates`` cuts across them: it counts every row a
    reconstruction was attempted for, which includes an already-``NULL``
    row that stays ``NULL`` -- that row's stored state does not change, so
    it is reported as ``unchanged``, keeping a second pass a reported
    no-op. ``marked_unavailable`` counts only a real ``0.00`` -> ``NULL``
    transition.
    """

    model_config = ConfigDict(frozen=True)

    scanned: int
    candidates: int
    repaired: int
    marked_unavailable: int
    unchanged: int
    dry_run: bool
    #: How many of ``repaired`` were valued with at least one holding taken at
    #: its carrying cost rather than dated evidence (#519).
    estimated: int = 0
    fetch_failures: tuple[str, ...] = ()
    newly_unavailable: tuple[str, ...] = ()


class SnapshotRepairService:
    """Repairs zero-valued and unavailable snapshot rows, idempotently."""

    def __init__(
        self,
        trades: TradesRepository,
        snapshots: PortfolioSnapshotsRepository,
        price_source: HistoricalGbpPriceSource | None = None,
        backfill: PriceEvidenceBackfillService | None = None,
        estimate_unpriceable: bool = True,
    ) -> None:
        self._trades = trades
        self._snapshots = snapshots
        self._price_source: HistoricalGbpPriceSource = (
            price_source or NoHistoricalPriceSource()
        )
        self._backfill = backfill
        # False restores the pre-#519 all-or-nothing rule: a holding with no
        # dated evidence nulls the whole row instead of being carried at cost.
        self._estimate_unpriceable = estimate_unpriceable

    def repair(
        self, portfolio_id: int | None = None, dry_run: bool = False
    ) -> SnapshotRepairReport:
        """Repair stored-zero and unavailable snapshots, returning what changed.

        Both a defective ``0.00`` and an already-``NULL`` row are offered to
        the price source, so evidence acquired after an earlier pass can
        still restore a gap. Scoped to ``portfolio_id`` when given. With
        ``dry_run`` the counts are computed exactly as they would be
        applied, but nothing is written. Running the pass a second time
        reports every row as ``unchanged``.
        """
        rows = self._snapshots.rows_with_ids(portfolio_id)
        fetch_failures: tuple[str, ...] = ()
        newly_unavailable: tuple[str, ...] = ()
        if self._backfill is not None and not dry_run:
            fetch_failures, newly_unavailable = self._prefetch_evidence(rows)

        replay_cache: dict[int | None, list[tuple[Any, ...]]] = {}
        repaired = marked = unchanged = candidates = estimated = 0

        for row in rows:
            row_id, pf_id, timestamp, total_value, total_cost = (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
            )
            already_null = total_value is None
            if not (already_null or self._is_stored_zero(total_value)):
                unchanged += 1
                continue
            if pf_id not in replay_cache:
                # Converted once per portfolio, so this pass's carrying costs
                # are GBP rather than raw foreign price units (#549).
                replay_cache[pf_id] = gbp_replay_rows(
                    self._trades.open_rows(pf_id), self._price_source.gbp_rate
                )
            holdings = self._holdings_as_of(replay_cache[pf_id], str(timestamp)[:10])
            if not holdings:
                # A genuinely empty (cash-only) portfolio: 0.00 is correct.
                unchanged += 1
                continue

            if _is_weekend(str(timestamp)):
                # No venue opened, so there is nothing to revalue: pricing a
                # Saturday off whatever stray close exists writes back the
                # very phantom the backfill's carry-forward avoids (#550).
                unchanged += 1
                continue

            candidates += 1
            value, is_estimated = self._reconstruct(
                replay_cache[pf_id], holdings, str(timestamp)[:10]
            )
            if value is None:
                if already_null:
                    # Still no evidence: the row is exactly as it was.
                    unchanged += 1
                    continue
                marked += 1
                if not dry_run:
                    self._snapshots.update_valuation(int(row_id), None, total_cost)
                continue
            repaired += 1
            estimated += int(is_estimated)
            if not dry_run:
                self._snapshots.update_valuation(
                    int(row_id), value, total_cost, is_estimated
                )

        report = SnapshotRepairReport(
            scanned=len(rows),
            candidates=candidates,
            repaired=repaired,
            marked_unavailable=marked,
            unchanged=unchanged,
            dry_run=dry_run,
            estimated=estimated,
            fetch_failures=fetch_failures,
            newly_unavailable=newly_unavailable,
        )
        logger.info("snapshot repair: %s", report.model_dump())
        return report

    def _prefetch_evidence(
        self, rows: list[tuple[Any, ...]]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Backfill historical evidence for every candidate row's tickers.

        One bulk fetch per distinct ticker across every row in ``rows``
        (never per portfolio -- two portfolios holding the same ticker
        share one fetch), spanning that ticker's earliest trade date
        through the latest candidate-snapshot date needing repair. Runs
        before the reconstruction loop below so freshly committed evidence
        is available to it in the same pass. One ticker's failure never
        stops another's attempt -- including a failure just building that
        ticker's span (e.g. one portfolio's trade replay erroring), which
        must not abort every other portfolio's contribution either.
        """
        assert self._backfill is not None
        replay_cache: dict[int | None, list[tuple[Any, ...]]] = {}
        first_trade_cache: dict[int | None, dict[str, str]] = {}
        spans: dict[str, tuple[str, str]] = {}
        for row in rows:
            _row_id, pf_id, timestamp, total_value = row[0], row[1], row[2], row[3]
            if not (total_value is None or self._is_stored_zero(total_value)):
                continue
            try:
                if pf_id not in replay_cache:
                    replay_cache[pf_id] = self._trades.open_rows(pf_id)
                    first_trade_cache[pf_id] = self._first_trade_dates(
                        replay_cache[pf_id]
                    )
                as_of = str(timestamp)[:10]
                holdings = self._holdings_as_of(replay_cache[pf_id], as_of)
                for ticker in holdings:
                    first_trade = first_trade_cache[pf_id].get(ticker)
                    if first_trade is None:
                        continue
                    existing = spans.get(ticker)
                    spans[ticker] = (
                        (min(existing[0], first_trade), max(existing[1], as_of))
                        if existing is not None
                        else (first_trade, as_of)
                    )
            except Exception as exc:
                logger.warning(
                    "prefetch: could not build evidence span for portfolio %s: %s",
                    pf_id,
                    exc,
                )
                continue

        fetch_failures: list[str] = []
        newly_unavailable: list[str] = []
        overall_start: date | None = None
        overall_end: date | None = None
        for ticker, (start_str, end_str) in spans.items():
            start = date.fromisoformat(start_str)
            end = date.fromisoformat(end_str) + timedelta(days=1)
            overall_start = (
                start if overall_start is None else min(overall_start, start)
            )
            overall_end = end if overall_end is None else max(overall_end, end)
            try:
                self._backfill.ensure_coverage(ticker, start, end)
            except PriceEvidenceUnavailable:
                newly_unavailable.append(ticker)
            except Exception as exc:
                logger.warning("price evidence backfill failed for %s: %s", ticker, exc)
                fetch_failures.append(ticker)

        if overall_start is not None and overall_end is not None:
            # One shared FX fetch per run, spanning every ticker's span --
            # never per ticker, and never speculative when nothing needs
            # repair (no span means this is skipped entirely). Isolated the
            # same way as a per-ticker failure, including the date math
            # above, so a defect here never aborts the rest of the run.
            try:
                self._backfill.ensure_fx_coverage(overall_start, overall_end)
            except PriceEvidenceUnavailable:
                newly_unavailable.append(FX_PAIR)
            except Exception as exc:
                logger.warning("FX evidence backfill failed: %s", exc)
                fetch_failures.append(FX_PAIR)
        return tuple(fetch_failures), tuple(newly_unavailable)

    @staticmethod
    def _first_trade_dates(replay_rows: list[tuple[Any, ...]]) -> dict[str, str]:
        """Delegate to the module-level :func:`first_trade_dates` (#502)."""
        return first_trade_dates(replay_rows)

    def _reconstruct(
        self,
        replay_rows: list[tuple[Any, ...]],
        holdings: dict[str, float],
        as_of: str,
    ) -> tuple[float | None, bool]:
        """Return ``(value, is_estimated)`` for ``holdings`` at ``as_of`` (#519).

        Delegates to the shared :func:`value_holdings`, supplying per-ticker
        carrying costs from the same trade replay ``total_cost`` uses -- or an
        empty mapping when ``estimate_unpriceable`` is off, which keeps the
        pre-#519 all-or-nothing behaviour (the CLI's
        ``--no-historical-evidence``) exactly as it was.
        """
        carrying: dict[str, float | None] = (
            position_cost_basis_as_of(replay_rows, as_of)
            if self._estimate_unpriceable
            else {}
        )
        return value_holdings(self._price_source, holdings, as_of, carrying)

    @staticmethod
    def _is_stored_zero(total_value: Any) -> bool:
        """Return True for the defective ``0.00`` this pass repairs.

        A NULL is handled separately by the caller (it is a reconstruction
        candidate too, since evidence may have arrived since it was
        written); any non-zero number is not a candidate at all.
        """
        if total_value is None:
            return False
        try:
            return abs(float(total_value)) <= _ZERO_TOLERANCE
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _holdings_as_of(
        replay_rows: list[tuple[Any, ...]], as_of: str
    ) -> dict[str, float]:
        """Delegate to the module-level :func:`holdings_as_of` (#502)."""
        return holdings_as_of(replay_rows, as_of)
