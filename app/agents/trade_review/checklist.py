"""Deterministic trade-process checklist (GH-17).

Each BUY and SELL is judged against a fixed set of rules using only evidence
dated strictly before its trade date: every read passes ``D - 1`` as its
bound, and bars on or after ``D`` are dropped again here, so a later price
move can never change a verdict. The one exception is an open lot's exit
check, which by definition watches the sessions after entry: it reads
through the store's latest session and cites that session.

The checks never read a sell price, a P&L figure or any other outcome, so a
lucky trade and a disciplined losing trade are judged alike. A missing
record is ``unknown``, never a mistake.

Two narrow reads are not prices: the split ratio effective on the trade
date itself (a split's ex-date is fixed before the session opens) restates
a pivot into the trade's own shares, and an open lot's exit check compares
the store's latest session with ``today`` to tell stale history apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from app.agents.trade_review.evidence import (
    PRICE_SOURCE,
    SCAN_SOURCE,
    EvidenceReader,
    PriceBar,
    PriceWindow,
    ScanContext,
)
from app.core.stage_classification import classify_weinstein_stage
from app.core.technical_indicators import (
    compute_reconstruction_technicals,
    weekly_closes,
)
from app.repositories.portfolio_strategies_repo import StrategyHistoryEntry
from app.schemas import Trade
from app.schemas.evidence_ref import EvidenceRefV1
from app.schemas.trade_review import (
    CheckKind,
    CheckStatus,
    ReviewRuleV1,
    TradeAnnotationV1,
    TradeCheckV1,
    TradeReviewV1,
)

MAX_ENTRY_RISK_PCT = 8
EXIT_GRACE_SESSIONS = 5
STAGE_SESSIONS = 252
SMA_WINDOW = 50
PIVOT_BAND = Decimal("1.05")
#: A read whose newest bar is older than this before its bound is stale.
STALE_AFTER_DAYS = 7
#: A committed scan older than this before the prior session is not recent.
SCAN_STALE_DAYS = 45
OPENING_LOT = "opening_lot"
ENTRY_KINDS: tuple[CheckKind, ...] = ("valid_setup", "entry_location", "evidenced_stop")

RULES: dict[CheckKind, ReviewRuleV1] = {
    "valid_setup": ReviewRuleV1(
        id="setup.stage_2",
        wording="Buy only when the Weinstein stage at the prior session is Stage 2.",
    ),
    "entry_location": ReviewRuleV1(
        id="entry.pivot_band",
        wording="Buy between the latest scan's pivot and 5% above it.",
    ),
    "evidenced_stop": ReviewRuleV1(
        id="stop.max_risk_8pct",
        wording="Buy with a stop that risks at most 8% of the entry price.",
    ),
    "exit_signal": ReviewRuleV1(
        id="exit.within_5_sessions",
        wording=(
            "Sell within 5 sessions of the first close below the 50-day SMA "
            "or at or below the stop."
        ),
    ),
    "strategy_alignment": ReviewRuleV1(
        id="strategy.assigned",
        wording="Follow the Strategy assigned to the portfolio at the time.",
    ),
    "data_completeness": ReviewRuleV1(
        id="data.complete",
        wording="Every other check had the evidence it needs.",
    ),
}


@dataclass(frozen=True)
class Entry:
    """A BUY lot a SELL closed, with that BUY's current annotation."""

    trade: Trade
    annotation: TradeAnnotationV1 | None = None


def review_buy(
    trade: Trade,
    reader: EvidenceReader,
    *,
    annotation: TradeAnnotationV1 | None,
    lot_open: bool,
    history: Sequence[StrategyHistoryEntry],
    today: date | None = None,
) -> TradeReviewV1:
    """Review a BUY: entry checks, plus the exit check while a lot is open.

    An opening lot (a position held before tracking began) has no entry to
    judge, so its entry checks are ``n_a``. ``today`` lets an open lot's exit
    check tell stale price history apart; None skips that test.
    """
    security = reader.resolve(trade.ticker, _currency(trade))
    if trade.source == OPENING_LOT:
        checks = [_held_before_tracking(kind) for kind in ENTRY_KINDS]
    else:
        checks = _entry_checks(trade, annotation, security, reader)
    if lot_open:
        entry = Entry(trade, annotation)
        checks.append(open_lot_exit(entry, security, reader, today))
    checks.append(strategy_alignment(trade_day(trade), history))
    return _review(trade, checks)


def failed_review(trade: Trade, *, lot_open: bool) -> TradeReviewV1:
    """Every applicable check unknown: reviewing this trade raised."""
    kinds: list[CheckKind] = (
        [*ENTRY_KINDS, *(["exit_signal"] if lot_open else [])]
        if trade.action == "BUY"
        else ["exit_signal"]
    )
    checks = [
        _check(
            kind,
            "unknown",
            "review failed for this trade",
            (),
            {},
            "Reviewing this trade raised an error; the other trades are unaffected.",
        )
        for kind in (*kinds, "strategy_alignment")
    ]
    return _review(trade, checks)


def review_sell(
    trade: Trade,
    reader: EvidenceReader,
    *,
    entries: Sequence[Entry],
    history: Sequence[StrategyHistoryEntry],
) -> TradeReviewV1:
    """Review a SELL against the entries of the lots it closed (FIFO)."""
    day = trade_day(trade)
    checks = [
        sell_exit(day, entries, reader.resolve(trade.ticker, _currency(trade)), reader),
        strategy_alignment(day, history),
    ]
    return _review(trade, checks)


def _entry_checks(
    trade: Trade,
    annotation: TradeAnnotationV1 | None,
    security: str | None,
    reader: EvidenceReader,
) -> list[TradeCheckV1]:
    day = trade_day(trade)
    prior = day - timedelta(days=1)
    window = scan = None
    split: Decimal | None = Decimal(1)
    if security is not None:
        window = _before(reader.bars(security, prior, STAGE_SESSIONS), day, prior)
        scan = reader.scan(security, prior)
        if scan is not None and scan.as_of >= day:
            scan = None  # never trust a reader to honour the bound
        if scan is not None:
            split = reader.split_on(security, day)
    return [
        valid_setup(window),
        entry_location(trade, window, scan, split),
        evidenced_stop(trade, annotation, window),
    ]


def trade_day(trade: Trade) -> date:
    """Return the trade's session date (the ledger stores no time)."""
    return date.fromisoformat(trade.date)


def valid_setup(window: PriceWindow | None) -> TradeCheckV1:
    """Weinstein stage from the 252 sessions through the prior session."""
    if window is None or len(window.bars) < STAGE_SESSIONS:
        return _check(
            "valid_setup",
            "unknown",
            "fewer than 252 prior sessions",
            _price_refs(window),
            {"sessions": len(window.bars) if window else 0},
            "Needs 252 sessions before the trade date to classify the stage.",
        )
    rows = window.bars[-STAGE_SESSIONS:]
    try:
        technicals = compute_reconstruction_technicals(rows)
    except ValueError:
        return _check(
            "valid_setup",
            "unknown",
            "incomplete prior sessions",
            _price_refs(window),
            {"sessions": len(rows)},
            "A session in the 252-session window lacks volume or is out of order.",
        )
    stage = classify_weinstein_stage(
        price=technicals.price,
        sma150=technicals.sma150,
        sma200=technicals.sma200,
        price_history=weekly_closes(rows),
    )
    last = rows[-1].session.isoformat()
    return _check(
        "valid_setup",
        "followed" if stage == "Stage 2" else "deviated",
        "" if stage == "Stage 2" else f"{stage} at entry",
        _price_refs(window),
        {"stage": stage, "sessions": len(rows)},
        f"Weinstein stage from 252 sessions through {last}: {stage}.",
    )


def entry_location(
    trade: Trade,
    window: PriceWindow | None,
    scan: ScanContext | None,
    split: Decimal | None = Decimal(1),
) -> TradeCheckV1:
    """Entry price against the latest committed scan pivot before the trade.

    The pivot is restated into the prior session's shares (the window's
    split factor at the scan session), then into the trade date's shares by
    ``split``, the split ratio effective on the trade date itself.
    """
    refs = _scan_refs(scan)
    if scan is None or scan.pivot is None or scan.pivot <= 0:
        return _check(
            "entry_location",
            "unknown",
            "no pivot before the trade",
            refs,
            {},
            "No committed scan with a pivot before the trade date.",
        )
    prior = trade_day(trade) - timedelta(days=1)
    if scan.as_of < prior - timedelta(days=SCAN_STALE_DAYS):
        return _check(
            "entry_location",
            "unknown",
            "no recent scan",
            refs,
            {},
            f"The latest committed scan ({scan.as_of}) is more than "
            f"{SCAN_STALE_DAYS} days before the prior session.",
        )
    bar = _bar_at(window, scan.as_of)
    if bar is None:
        return _check(
            "entry_location",
            "unknown",
            "no recent scan in the price window",
            refs + _price_refs(window),
            {},
            f"No price session on the scan date ({scan.as_of}) to restate its pivot.",
        )
    if (
        scan.currency != _currency(trade)
        or bar.split_factor <= 0
        or split is None
        or split <= 0
    ):
        return _check(
            "entry_location",
            "unknown",
            "units cannot be reconciled",
            refs + _price_refs(window),
            {"scan_currency": scan.currency, "trade_currency": _currency(trade)},
            "The trade and scan prices are not in comparable units.",
        )
    pivot = scan.pivot / bar.split_factor / split
    price = Decimal(str(trade.price))
    pct = (price / pivot - 1) * 100
    status: CheckStatus = "followed"
    note = ""
    if price < pivot:
        status, note = "deviated", "before breakout"
    elif price > pivot * PIVOT_BAND:
        status, note = "deviated", "extended"
    return _check(
        "entry_location",
        status,
        note,
        refs + _price_refs(window),
        {
            "price": trade.price,
            "pivot": _round(pivot, 4),
            "entry_vs_pivot_pct": _round(pct, 2),
        },
        f"Entry {price} vs pivot {pivot:.4f} = {pct:+.2f}% (followed from 0% to +5%)"
        + (
            f"; pivot restated for a {split} split on the trade date."
            if split != 1
            else "."
        ),
    )


def evidenced_stop(
    trade: Trade,
    annotation: TradeAnnotationV1 | None,
    window: PriceWindow | None = None,
) -> TradeCheckV1:
    """Risk to the recorded stop, else to the stop the user annotated.

    When the resolved price line is in another currency than the trade, the
    stop cannot be tied to that line, so the risk is unknown.
    """
    stop, ref = _stop_for(Entry(trade, annotation))
    if stop is None:
        return _check(
            "evidenced_stop",
            "unknown",
            "no stop recorded",
            (),
            {},
            "Neither the trade nor an annotation records a stop.",
        )
    if window is not None and window.currency != _currency(trade):
        return _check(
            "evidenced_stop",
            "unknown",
            "stop and price line currencies differ",
            (ref, *_price_refs(window)),
            {"line_currency": window.currency, "trade_currency": _currency(trade)},
            f"The stop is in {_currency(trade)}; the price line is in "
            f"{window.currency}.",
        )
    risk = (trade.price - stop) / trade.price * 100
    status: CheckStatus = "followed"
    note = ""
    if stop >= trade.price:
        status, note = "deviated", "stop at or above entry"
    elif risk > MAX_ENTRY_RISK_PCT:
        status, note = "deviated", "risk above 8%"
    return _check(
        "evidenced_stop",
        status,
        note,
        (ref,),
        {"stop": stop, "price": trade.price, "risk_pct": round(risk, 2)},
        f"({trade.price} - {stop}) / {trade.price} = {risk:.2f}% risk "
        f"(followed at or below {MAX_ENTRY_RISK_PCT}%); stop from {ref.source}.",
    )


def sell_exit(
    day: date, entries: Sequence[Entry], security: str | None, reader: EvidenceReader
) -> TradeCheckV1:
    """Judge a SELL's timing against each closed lot's first exit signal."""
    if not entries:
        return _exit_unknown("no matched entry", ())
    if security is None:
        return _exit_unknown("security not in the price store", ())
    prior = day - timedelta(days=1)
    earliest = min(trade_day(e.trade) for e in entries)
    limit = (prior - earliest).days + 2 * SMA_WINDOW
    window = _before(reader.bars(security, prior, max(limit, 1)), day, prior)
    checks = [_sell_lot(window, entry) for entry in entries]
    for status in ("deviated", "unknown"):
        worst = next((c for c in checks if c.status == status), None)
        if worst is not None:
            return worst
    return checks[0]


def open_lot_exit(
    entry: Entry,
    security: str | None,
    reader: EvidenceReader,
    today: date | None = None,
) -> TradeCheckV1:
    """Watch an open lot from entry through the store's latest session."""
    if security is None:
        return _exit_unknown("security not in the price store", ())
    latest = reader.bars(security, date.max, 1)
    if latest is None or not latest.bars:
        return _exit_unknown("missing history", ())
    last = latest.bars[-1].session
    if today is not None and last < today - timedelta(days=STALE_AFTER_DAYS):
        return _exit_unknown(
            "price history is stale",
            _price_refs(latest),
            f"The latest price session ({last}) is more than {STALE_AFTER_DAYS} "
            "days old.",
        )
    entered = trade_day(entry.trade)
    if last <= entered:
        return _exit_unknown("no sessions since entry", _price_refs(latest))
    window = reader.bars(security, last, (last - entered).days + 2 * SMA_WINDOW)
    signal = _first_signal(window, entry)
    if window is None or signal is None:
        return _exit_unknown("missing history", _price_refs(window))
    index, reason = signal
    if index is None:
        return _exit_check("n_a", "open, no exit signal yet", window, {}, "")
    since = len(window.bars) - 1 - index
    held = since > EXIT_GRACE_SESSIONS
    return _exit_check(
        "deviated" if held else "n_a",
        "held through signal" if held else "open, signal within grace",
        window,
        {"sessions_after_signal": since},
        f"{reason} on {window.bars[index].session}, {since} sessions ago.",
    )


def strategy_alignment(
    day: date, history: Sequence[StrategyHistoryEntry]
) -> TradeCheckV1:
    """Whether a Strategy covered the trade date; replay is not evaluated.

    Only a change recorded on a day strictly before the trade date covers
    it: the trade date has no time, so a same-day change may be later.
    """
    covering = [h for h in history if h.recorded_at[:10] < day.isoformat()]
    latest = covering[-1] if covering else None
    if latest is None or latest.strategy_id is None:
        refs = () if latest is None else (_history_ref(latest),)
        return _check(
            "strategy_alignment",
            "n_a",
            "no Strategy assigned then",
            refs,
            {},
            f"No Strategy assignment covered {day.isoformat()}.",
        )
    return _check(
        "strategy_alignment",
        "unknown",
        "Strategy replay not evaluated in this version",
        (_history_ref(latest),),
        {"strategy_id": latest.strategy_id},
        f"Strategy {latest.strategy_id} covered {day.isoformat()}.",
    )


def data_completeness(checks: Sequence[TradeCheckV1]) -> TradeCheckV1:
    """Followed when every other check had its evidence; else list the gaps."""
    missing = [
        c.kind
        for c in checks
        if c.status == "unknown" and c.kind != "strategy_alignment"
    ]
    if not missing:
        return _check(
            "data_completeness",
            "followed",
            "",
            (),
            {},
            "Every check had its evidence.",
        )
    return _check(
        "data_completeness",
        "unknown",
        "missing: " + ", ".join(missing),
        (),
        {"missing": ", ".join(missing)},
        f"{len(missing)} check(s) lacked evidence.",
    )


def _review(trade: Trade, checks: list[TradeCheckV1]) -> TradeReviewV1:
    assert trade.id is not None and trade.portfolio_id is not None
    return TradeReviewV1(
        trade_id=trade.id,
        portfolio_id=trade.portfolio_id,
        ticker=trade.ticker,
        action="BUY" if trade.action == "BUY" else "SELL",
        trade_date=trade_day(trade),
        opening_lot=trade.source == OPENING_LOT,
        checks=(*checks, data_completeness(checks)),
    )


NO_PRIOR_SIGNAL = (
    "No close below the 50-day average or the stop before the sale; a same-day "
    "stop-out and a discretionary exit cannot be told apart."
)


def _sell_lot(window: PriceWindow | None, entry: Entry) -> TradeCheckV1:
    """One closed lot: did the sell come within the grace of the first signal?"""
    if window is not None and window.bars[-1].session <= trade_day(entry.trade):
        return _exit_unknown(
            "no session between entry and sale to judge",
            _price_refs(window),
            "The sale came before any session after the entry closed.",
        )
    signal = _first_signal(window, entry)
    if window is None or signal is None:
        return _exit_unknown("missing history", _price_refs(window))
    index, reason = signal
    if index is None:
        # A same-day stop-out and a discretionary exit look the same here.
        return _exit_check(
            "unknown", "no exit signal before the sale", window, {}, NO_PRIOR_SIGNAL
        )
    # Sessions from the signal to the sell: the bars after it plus the sell.
    after = len(window.bars) - index
    held = after > EXIT_GRACE_SESSIONS
    return _exit_check(
        "deviated" if held else "followed",
        "held through signal" if held else "",
        window,
        {"sessions_after_signal": after},
        f"{reason} on {window.bars[index].session}; sold {after} session(s) later.",
    )


def _first_signal(
    window: PriceWindow | None, entry: Entry
) -> tuple[int | None, str] | None:
    """Return (index, reason) of the first exit signal after entry.

    ``(None, "")`` when no signal occurred; None when the history cannot
    tell (no session before entry, too few for a 50-day SMA, or a stop
    that cannot be restated). Each day is judged with that day's own SMA50
    only. A stop in another currency than the price line is not used.
    """
    if window is None:
        return None
    bars = window.bars
    entered = trade_day(entry.trade)
    start = next((i for i, b in enumerate(bars) if b.session > entered), len(bars))
    if start == 0 or start < SMA_WINDOW - 1:
        return None
    stop, _ = _stop_for(entry)
    stop_now = None
    if stop is not None and window.currency == _currency(entry.trade):
        # The stop was stated in entry-day shares; restate it in the read's
        # shares with the entry session's own split factor.
        entry_bar = _bar_at(window, entered)
        if entry_bar is None or entry_bar.split_factor <= 0:
            return None
        stop_now = Decimal(str(stop)) / entry_bar.split_factor
    for i in range(start, len(bars)):
        close = bars[i].close
        if stop_now is not None and close <= stop_now:
            return i, "close at or below the stop"
        sma = sum((b.close for b in bars[i - SMA_WINDOW + 1 : i + 1]), Decimal(0))
        if close < sma / SMA_WINDOW:
            return i, "close below the 50-day SMA"
    return None, ""


def _stop_for(entry: Entry) -> tuple[float | None, EvidenceRefV1]:
    """The BUY's recorded stop, else its annotation's stated stop, with source."""
    trade, annotation = entry.trade, entry.annotation
    if trade.stop_loss:
        return trade.stop_loss, EvidenceRefV1(
            kind="trade", id=f"trade:{trade.id}", source="trade record"
        )
    if annotation is not None and annotation.stated_stop is not None:
        stated = annotation.created_at[:10]
        after = stated >= trade.date
        return annotation.stated_stop, EvidenceRefV1(
            kind="annotation_after_trade" if after else "annotation",
            id=f"annotation:{annotation.id}",
            source=(
                f"your annotation, stated by you on {stated}, after the trade"
                if after
                else "your annotation"
            ),
        )
    return None, EvidenceRefV1(kind="trade", id=f"trade:{trade.id}", source="none")


def _before(window: PriceWindow | None, day: date, prior: date) -> PriceWindow | None:
    """Drop bars on or after ``day``; a window stale by its bound is no window."""
    if window is None:
        return None
    bars = tuple(b for b in window.bars if b.session < day)
    if not bars or bars[-1].session < prior - timedelta(days=STALE_AFTER_DAYS):
        return None
    return replace(window, bars=bars)


def _bar_at(window: PriceWindow | None, session: date) -> PriceBar | None:
    """Return the window's bar for exactly ``session``, if present."""
    if window is None:
        return None
    return next((b for b in window.bars if b.session == session), None)


def _price_refs(window: PriceWindow | None) -> tuple[EvidenceRefV1, ...]:
    if window is None or not window.bars:
        return ()
    return (
        EvidenceRefV1(
            kind="price_history",
            id=f"{window.security_id}@{window.revision[:12]}",
            as_of=window.bars[-1].session,
            source=PRICE_SOURCE,
        ),
    )


def _scan_refs(scan: ScanContext | None) -> tuple[EvidenceRefV1, ...]:
    if scan is None:
        return ()
    return (
        EvidenceRefV1(
            kind="scan",
            id=f"{scan.snapshot_month}:{scan.security_id}",
            as_of=scan.as_of,
            source=SCAN_SOURCE,
        ),
    )


def _history_ref(entry: StrategyHistoryEntry) -> EvidenceRefV1:
    return EvidenceRefV1(
        kind="strategy_history",
        id=f"strategy_history:{entry.id}",
        as_of=date.fromisoformat(entry.recorded_at[:10]),
        source="Strategy assignment history",
    )


def _exit_unknown(
    note: str,
    refs: tuple[EvidenceRefV1, ...],
    calculation: str = "Needs 50 sessions before the first day after entry.",
) -> TradeCheckV1:
    return _check("exit_signal", "unknown", note, refs, {}, calculation)


def _held_before_tracking(kind: CheckKind) -> TradeCheckV1:
    return _check(
        kind,
        "n_a",
        "position held before tracking began",
        (),
        {},
        "An opening lot records a holding; there is no entry to judge.",
    )


def _exit_check(
    status: CheckStatus,
    note: str,
    window: PriceWindow,
    observed: dict[str, float | str | None],
    detail: str,
) -> TradeCheckV1:
    last = window.bars[-1].session.isoformat()
    return _check(
        "exit_signal",
        status,
        note,
        _price_refs(window),
        observed,
        detail or f"No close below the 50-day SMA or the stop through {last}.",
    )


def _check(
    kind: CheckKind,
    status: CheckStatus,
    note: str,
    evidence: tuple[EvidenceRefV1, ...],
    observed: dict[str, float | str | None],
    calculation: str,
) -> TradeCheckV1:
    return TradeCheckV1(
        kind=kind,
        status=status,
        rule=RULES[kind],
        note=note,
        evidence=evidence,
        observed=observed,
        calculation=calculation,
    )


def _currency(trade: Trade) -> str:
    """The trade's currency; pence spellings fold to GBP as the trader does.

    Trade prices are major units even for a pence-quoted listing (the SIPP
    CSV quotes LSE trades in pounds), so ``GBX``/``GBp`` is GBP, unscaled.
    """
    code = (trade.currency or "GBP").strip().upper()
    return "GBP" if code == "GBX" else code


def _round(value: Decimal, places: int) -> float:
    return round(float(value), places)
