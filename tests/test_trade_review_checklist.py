"""Unit tests for the deterministic trade-process checklist (GH-17).

Evidence comes from ``FakeReader``, an in-memory ``EvidenceReader`` that
records every bound it was asked for, so no store is ever opened. Covers the
checklist rows of the I/O matrix: no look-ahead, missing evidence, outcome
independence, extended entry, held through signal, stop from annotation and
no Strategy then.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.agents.trade_review.checklist import (
    Entry,
    review_buy,
    review_sell,
    strategy_alignment,
)
from app.agents.trade_review.evidence import PriceBar, PriceWindow, ScanContext
from app.repositories.portfolio_strategies_repo import StrategyHistoryEntry
from app.schemas import Trade
from app.schemas.trade_review import TradeAnnotationV1, TradeReviewV1

SECURITY = "sid-zeta"
START = date(2024, 1, 1)


def sessions(count: int, start: date = START) -> list[date]:
    """Return ``count`` weekday sessions from ``start``."""
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def make_bars(closes: list[float], start: date = START) -> tuple[PriceBar, ...]:
    """One bar per close on consecutive weekday sessions."""
    return tuple(
        PriceBar(
            session=day,
            open=Decimal(str(c)),
            high=Decimal(str(c)),
            low=Decimal(str(c)),
            close=Decimal(str(c)),
            volume=Decimal(1000),
        )
        for day, c in zip(sessions(len(closes), start), closes)
    )


def rising(count: int) -> list[float]:
    return [100 + i * 0.5 for i in range(count)]


@dataclass
class FakeReader:
    """In-memory ``EvidenceReader`` that records every read's bound."""

    bars_: tuple[PriceBar, ...] = ()
    scans: list[ScanContext] = field(default_factory=list)
    security: str | None = SECURITY
    leaky: bool = False
    leak_scans: bool = False
    failed: bool = False
    currency: str = "USD"
    splits: dict[date, Decimal] = field(default_factory=dict)
    reads: list[tuple[str, date]] = field(default_factory=list)

    def resolve(self, ticker: str, currency: str) -> str | None:
        return self.security

    def bars(self, security_id: str, through: date, limit: int) -> PriceWindow | None:
        self.reads.append(("bars", through))
        rows = (
            self.bars_
            if self.leaky
            else tuple(b for b in self.bars_ if b.session <= through)
        )
        rows = rows if self.leaky else rows[-limit:]
        return (
            PriceWindow(security_id, "rev-000000000001", through, rows, self.currency)
            if rows
            else None
        )

    def scan(self, security_id: str, as_of: date) -> ScanContext | None:
        self.reads.append(("scan", as_of))
        visible = [s for s in self.scans if self.leak_scans or s.as_of <= as_of]
        return visible[-1] if visible else None

    def split_on(self, security_id: str, session: date) -> Decimal | None:
        # The trade date's split ratio only; not a price read.
        return self.splits.get(session, Decimal(1))

    def close(self) -> None:
        return None


def buy(
    day: date, price: float = 103.0, trade_id: int = 1, stop: float | None = None
) -> Trade:
    return Trade(
        id=trade_id,
        ticker="ZETA",
        action="BUY",
        shares=77,
        price=price,
        date=day.isoformat(),
        stop_loss=stop,
        portfolio_id=1,
        currency="USD",
    )


def sell(day: date, price: float, trade_id: int = 2) -> Trade:
    return buy(day, price, trade_id).model_copy(update={"action": "SELL"})


def scan(as_of: date, pivot: str | None = "100", currency: str = "USD") -> ScanContext:
    return ScanContext(
        security_id=SECURITY,
        snapshot_month=as_of.strftime("%Y-%m"),
        as_of=as_of,
        currency=currency,
        stage="Stage 2",
        pivot=None if pivot is None else Decimal(pivot),
    )


def annotation(stop: float | None, trade_id: int = 1) -> TradeAnnotationV1:
    return TradeAnnotationV1(
        id=9,
        portfolio_id=1,
        trade_id=trade_id,
        intent="secret intent",
        stated_stop=stop,
        created_at="2024-12-31T00:00:00+00:00",
    )


def statuses(review: TradeReviewV1) -> dict[str, tuple[str, str]]:
    return {c.kind: (c.status, c.note) for c in review.checks}


def _buy_review(
    reader: FakeReader,
    trade: Trade,
    annotation: TradeAnnotationV1 | None = None,
    lot_open: bool = False,
) -> TradeReviewV1:
    return review_buy(
        trade, reader, annotation=annotation, lot_open=lot_open, history=[]
    )


DAYS = sessions(320)
BUY_DAY = DAYS[260]


def _buy_reader(**overrides: object) -> FakeReader:
    reader = FakeReader(bars_=make_bars(rising(320)), scans=[scan(DAYS[250])])
    for key, value in overrides.items():
        setattr(reader, key, value)
    return reader


# --- no look-ahead ------------------------------------------------------------


@pytest.mark.parametrize("leaky", [False, True], ids=["bounded", "leaky-reader"])
def test_buy_reads_only_sessions_before_the_trade_date(leaky: bool) -> None:
    calm = _buy_reader(leaky=leaky)
    closes = rising(320)
    closes[260] = closes[261] = 1.0  # a crash on D and D+1
    crash = _buy_reader(bars_=make_bars(closes), leaky=leaky)
    later_scan = replace(scan(BUY_DAY), pivot=Decimal("1"))
    crash.scans.append(later_scan)

    first = _buy_review(calm, buy(BUY_DAY))
    second = _buy_review(crash, buy(BUY_DAY))

    assert first == second
    assert statuses(first)["valid_setup"] == ("followed", "")
    assert statuses(first)["entry_location"] == ("followed", "")
    for review in (first, second):
        for check in review.checks:
            assert all(
                ref.as_of is None or ref.as_of < BUY_DAY for ref in check.evidence
            )
    if not leaky:
        assert crash.reads and all(bound < BUY_DAY for _, bound in crash.reads)


def test_sell_reads_only_sessions_before_the_sell_date() -> None:
    closes = rising(200)
    sell_day = DAYS[120]
    reader = FakeReader(bars_=make_bars(closes))
    entries = [Entry(buy(DAYS[80]))]

    review = review_sell(sell(sell_day, 150.0), reader, entries=entries, history=[])

    assert reader.reads and all(bound < sell_day for _, bound in reader.reads)
    exit_check = review.check("exit_signal")
    assert exit_check is not None
    assert all(
        ref.as_of is not None and ref.as_of < sell_day for ref in exit_check.evidence
    )


def test_a_scan_dated_on_or_after_the_trade_is_never_used() -> None:
    reader = _buy_reader(scans=[scan(BUY_DAY, "1")], leak_scans=True)

    got = statuses(_buy_review(reader, buy(BUY_DAY)))["entry_location"]

    assert got == ("unknown", "no pivot before the trade")


# --- missing evidence ---------------------------------------------------------


def test_unresolved_security_and_no_stop_are_unknown_not_mistakes() -> None:
    review = _buy_review(_buy_reader(security=None), buy(BUY_DAY))

    got = statuses(review)
    assert got["valid_setup"][0] == "unknown"
    assert got["entry_location"][0] == "unknown"
    assert got["evidenced_stop"] == ("unknown", "no stop recorded")
    assert got["data_completeness"] == (
        "unknown",
        "missing: valid_setup, entry_location, evidenced_stop",
    )


def test_fewer_than_252_sessions_and_no_pivot_are_unknown() -> None:
    reader = _buy_reader(bars_=make_bars(rising(200)), scans=[scan(DAYS[150], None)])

    got = statuses(_buy_review(reader, buy(DAYS[200], stop=100.0)))

    assert got["valid_setup"] == ("unknown", "fewer than 252 prior sessions")
    assert got["entry_location"] == ("unknown", "no pivot before the trade")
    assert got["evidenced_stop"][0] == "followed"
    assert got["data_completeness"][0] == "unknown"


def test_complete_evidence_makes_data_completeness_followed() -> None:
    got = statuses(_buy_review(_buy_reader(), buy(BUY_DAY, stop=100.0)))

    assert got["data_completeness"] == ("followed", "")
    assert got["strategy_alignment"] == ("n_a", "no Strategy assigned then")


def test_falling_series_is_not_stage_2() -> None:
    reader = _buy_reader(bars_=make_bars([300 - i * 0.5 for i in range(320)]))

    status, note = statuses(_buy_review(reader, buy(BUY_DAY)))["valid_setup"]

    assert status == "deviated"
    assert note.endswith("at entry")


# --- entry location -------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (108.0, ("deviated", "extended")),
        (99.0, ("deviated", "before breakout")),
        (100.0, ("followed", "")),
        (105.0, ("followed", "")),
    ],
)
def test_entry_location_band(price: float, expected: tuple[str, str]) -> None:
    review = _buy_review(_buy_reader(), buy(BUY_DAY, price))

    assert statuses(review)["entry_location"] == expected


def test_extended_entry_reports_percent_above_pivot() -> None:
    check = _buy_review(_buy_reader(), buy(BUY_DAY, 108.0)).check("entry_location")

    assert check is not None
    assert check.observed["entry_vs_pivot_pct"] == 8.0
    assert [ref.source for ref in check.evidence][0] == "committed monthly scan"


def test_currency_mismatch_cannot_be_reconciled() -> None:
    reader = _buy_reader(scans=[scan(DAYS[250], currency="GBP")])

    got = statuses(_buy_review(reader, buy(BUY_DAY)))["entry_location"]

    assert got == ("unknown", "units cannot be reconciled")


def test_split_after_the_scan_restates_the_pivot() -> None:
    # A 2:1 split after the scan session: the scan's pivot of 200 is 100 in
    # the prior session's shares, so an entry at 103 is inside the band.
    bars = list(make_bars(rising(320)))
    for i in range(0, 255):
        bars[i] = replace(bars[i], split_factor=Decimal(2))
    reader = _buy_reader(bars_=tuple(bars), scans=[scan(DAYS[250], "200")])

    assert statuses(_buy_review(reader, buy(BUY_DAY)))["entry_location"] == (
        "followed",
        "",
    )


# --- evidenced stop -------------------------------------------------------------


def test_stop_from_annotation_is_followed_and_cited() -> None:
    review = _buy_review(
        _buy_reader(), buy(BUY_DAY, 100.0), annotation=annotation(95.0)
    )

    check = review.check("evidenced_stop")
    assert check is not None and check.status == "followed"
    assert check.evidence[0].source.startswith("your annotation")
    assert check.observed["risk_pct"] == 5.0


@pytest.mark.parametrize(
    ("stop", "expected"),
    [
        (90.0, ("deviated", "risk above 8%")),
        (100.0, ("deviated", "stop at or above entry")),
        (92.0, ("followed", "")),
    ],
)
def test_trade_stop_risk_thresholds(stop: float, expected: tuple[str, str]) -> None:
    review = _buy_review(_buy_reader(), buy(BUY_DAY, 100.0, stop=stop))

    check = review.check("evidenced_stop")
    assert check is not None
    assert (check.status, check.note) == expected
    assert check.evidence[0].source == "trade record"


# --- exit signal ----------------------------------------------------------------


def _exit_closes(drop_at: int | None) -> list[float]:
    return [
        50.0 if drop_at is not None and i >= drop_at else 100 + i for i in range(200)
    ]


@pytest.mark.parametrize(
    ("drop_at", "expected"),
    [
        (108, ("deviated", "held through signal")),
        (117, ("followed", "")),
        (None, ("unknown", "no exit signal before the sale")),
    ],
    ids=["12-sessions-before", "3-sessions-before", "no-signal"],
)
def test_sell_exit_timing(drop_at: int | None, expected: tuple[str, str]) -> None:
    reader = FakeReader(bars_=make_bars(_exit_closes(drop_at)))

    review = review_sell(
        sell(DAYS[120], 150.0), reader, entries=[Entry(buy(DAYS[80]))], history=[]
    )

    check = review.check("exit_signal")
    assert check is not None and (check.status, check.note) == expected
    if drop_at == 108:
        assert check.observed["sessions_after_signal"] == 12


def test_close_at_the_annotated_stop_is_a_signal() -> None:
    closes = [100.0 + i for i in range(200)]
    closes[110] = 175.0  # above SMA50 but at the stated stop
    reader = FakeReader(bars_=make_bars(closes))
    entries = [Entry(buy(DAYS[80]), annotation(176.0))]

    check = review_sell(
        sell(DAYS[120], 150.0), reader, entries=entries, history=[]
    ).check("exit_signal")

    assert check is not None and check.note == "held through signal"
    assert "stop" in check.calculation


def test_sell_without_warm_up_or_entry_is_unknown() -> None:
    reader = FakeReader(bars_=make_bars(_exit_closes(None)))

    early = review_sell(
        sell(DAYS[40], 150.0), reader, entries=[Entry(buy(DAYS[20]))], history=[]
    )
    unmatched = review_sell(sell(DAYS[120], 150.0), reader, entries=[], history=[])

    assert statuses(early)["exit_signal"] == ("unknown", "missing history")
    assert statuses(unmatched)["exit_signal"] == ("unknown", "no matched entry")
    assert statuses(early)["data_completeness"][0] == "unknown"


def test_outcome_does_not_change_any_status() -> None:
    reader = FakeReader(bars_=make_bars(_exit_closes(108)))
    entries = [Entry(buy(DAYS[80]))]

    win = review_sell(sell(DAYS[120], 500.0), reader, entries=entries, history=[])
    loss = review_sell(sell(DAYS[120], 5.0), reader, entries=entries, history=[])

    assert statuses(win) == statuses(loss)
    assert [c.calculation for c in win.checks] == [c.calculation for c in loss.checks]


@pytest.mark.parametrize(
    ("drop_at", "expected"),
    [
        (150, ("deviated", "held through signal")),
        (197, ("n_a", "open, signal within grace")),
        (None, ("n_a", "open, no exit signal yet")),
    ],
)
def test_open_lot_exit(drop_at: int | None, expected: tuple[str, str]) -> None:
    reader = FakeReader(bars_=make_bars(_exit_closes(drop_at)))

    review = _buy_review(reader, buy(DAYS[80]), lot_open=True)

    assert statuses(review)["exit_signal"] == expected


# --- strategy alignment ---------------------------------------------------------


def _history(*rows: tuple[str | None, str]) -> list[StrategyHistoryEntry]:
    return [StrategyHistoryEntry(i + 1, sid, at) for i, (sid, at) in enumerate(rows)]


def test_no_strategy_then_is_not_applicable() -> None:
    history = _history(("weinstein", "2024-06-01T10:00:00+00:00"))

    before = strategy_alignment(date(2024, 5, 31), history)
    covered = strategy_alignment(date(2024, 6, 2), history)
    cleared = strategy_alignment(
        date(2024, 7, 2),
        history + _history((None, "2024-07-01T00:00:00+00:00")),
    )

    assert (before.status, before.note) == ("n_a", "no Strategy assigned then")
    assert (covered.status, covered.note) == (
        "unknown",
        "Strategy replay not evaluated in this version",
    )
    assert cleared.status == "n_a"


def test_strategy_unknown_does_not_count_as_missing_evidence() -> None:
    history = _history(("weinstein", "2020-01-01T00:00:00+00:00"))

    review = review_buy(
        buy(BUY_DAY, stop=100.0),
        _buy_reader(),
        annotation=None,
        lot_open=False,
        history=history,
    )

    assert statuses(review)["strategy_alignment"][0] == "unknown"
    assert statuses(review)["data_completeness"] == ("followed", "")


def test_a_same_day_assignment_does_not_cover_the_trade() -> None:
    history = _history(("weinstein", "2024-06-01T00:00:01+00:00"))

    same_day = strategy_alignment(date(2024, 6, 1), history)

    assert (same_day.status, same_day.note) == ("n_a", "no Strategy assigned then")


# --- review fixes: entry ----------------------------------------------------------


def test_opening_lot_entry_checks_are_not_applicable() -> None:
    trade = buy(BUY_DAY, stop=50.0).model_copy(update={"source": "opening_lot"})

    review = _buy_review(_buy_reader(), trade)

    got = statuses(review)
    for kind in ("valid_setup", "entry_location", "evidenced_stop"):
        assert got[kind] == ("n_a", "position held before tracking began")
    assert got["data_completeness"] == ("followed", "")
    assert review.opening_lot


def test_opening_lots_never_feed_recurring_deviations() -> None:
    from app.agents.trade_review.weekly import weekly_facts

    reader = FakeReader(bars_=make_bars(_exit_closes(150)))
    held = [
        _buy_review(reader, buy(DAYS[80], trade_id=i), lot_open=True) for i in (1, 2)
    ]
    lots = [
        _buy_review(
            reader,
            buy(DAYS[80], trade_id=i).model_copy(update={"source": "opening_lot"}),
            lot_open=True,
        )
        for i in (3, 4)
    ]

    assert [r.rule_id for r in weekly_facts("w", held).recurring] == [
        "exit.within_5_sessions"
    ]
    facts = weekly_facts("w", lots)
    assert facts.recurring == ()
    assert facts.counts["exit_signal"]["deviated"] == 2


def test_split_on_the_trade_date_restates_the_pivot() -> None:
    # Scan pivot 200 before a 2:1 split effective on the BUY date: 100 in the
    # trade's own shares, so an entry at 103 is inside the band.
    reader = _buy_reader(scans=[scan(DAYS[250], "200")], splits={BUY_DAY: Decimal(2)})

    check = _buy_review(reader, buy(BUY_DAY)).check("entry_location")

    assert check is not None and (check.status, check.note) == ("followed", "")
    assert check.observed["pivot"] == 100.0
    assert "split on the trade date" in check.calculation


def test_unknown_split_on_the_trade_date_is_unknown() -> None:
    reader = _buy_reader(splits={BUY_DAY: None})

    got = statuses(_buy_review(reader, buy(BUY_DAY)))["entry_location"]

    assert got == ("unknown", "units cannot be reconciled")


def test_a_scan_older_than_45_days_is_not_recent() -> None:
    reader = _buy_reader(scans=[scan(DAYS[200])])

    got = statuses(_buy_review(reader, buy(BUY_DAY)))["entry_location"]

    assert got == ("unknown", "no recent scan")


def test_a_scan_without_a_price_session_says_so() -> None:
    saturday = date(2024, 12, 21)
    reader = _buy_reader(scans=[scan(saturday)])

    got = statuses(_buy_review(reader, buy(BUY_DAY)))["entry_location"]

    assert got == ("unknown", "no recent scan in the price window")


@pytest.mark.parametrize("currency", ["GBX", "gbp", "GBp", " GBX "])
def test_pence_trade_currency_folds_to_gbp(currency: str) -> None:
    reader = _buy_reader(scans=[scan(DAYS[250], currency="GBP")], currency="GBP")
    trade = buy(BUY_DAY, stop=100.0).model_copy(update={"currency": currency})

    got = statuses(_buy_review(reader, trade))

    assert got["entry_location"] == ("followed", "")
    assert got["evidenced_stop"][0] == "followed"


def test_zero_pivot_and_zero_split_factor_are_unknown() -> None:
    zero_pivot = _buy_reader(scans=[scan(DAYS[250], "0")])
    bars = list(make_bars(rising(320)))
    bars[250] = replace(bars[250], split_factor=Decimal(0))
    zero_factor = _buy_reader(bars_=tuple(bars))

    pivot = statuses(_buy_review(zero_pivot, buy(BUY_DAY)))["entry_location"]
    factor = statuses(_buy_review(zero_factor, buy(BUY_DAY)))["entry_location"]

    assert pivot == ("unknown", "no pivot before the trade")
    assert factor == ("unknown", "units cannot be reconciled")


# --- review fixes: stops ----------------------------------------------------------


def test_stop_stated_after_the_trade_is_labelled_as_such() -> None:
    before = replace_created(annotation(95.0), "2024-01-02T00:00:00+00:00")
    same_day = replace_created(annotation(95.0), f"{BUY_DAY}T23:00:00+00:00")

    earlier = _buy_review(_buy_reader(), buy(BUY_DAY, 100.0), annotation=before)
    later = _buy_review(_buy_reader(), buy(BUY_DAY, 100.0), annotation=same_day)

    prior = earlier.check("evidenced_stop")
    assert prior is not None
    ref = prior.evidence[0]
    assert (ref.kind, ref.source) == ("annotation", "your annotation")
    check = later.check("evidenced_stop")
    assert check is not None and check.status == "followed"
    after = check.evidence[0]
    assert after.kind == "annotation_after_trade" and after.as_of is None
    assert f"stated by you on {BUY_DAY}, after the trade" in after.source


def replace_created(note: TradeAnnotationV1, created_at: str) -> TradeAnnotationV1:
    return note.model_copy(update={"created_at": created_at})


def test_stop_against_a_price_line_in_another_currency_is_not_used() -> None:
    closes = [100.0 + i for i in range(200)]
    closes[110] = 190.0  # at the stated stop, above that day's SMA50 (185.1)
    entries = [Entry(buy(DAYS[80]), annotation(191.0))]

    same = review_sell(
        sell(DAYS[120], 150.0),
        FakeReader(bars_=make_bars(closes)),
        entries=entries,
        history=[],
    ).check("exit_signal")
    exit_check = review_sell(
        sell(DAYS[120], 150.0),
        FakeReader(bars_=make_bars(closes), currency="GBP"),
        entries=entries,
        history=[],
    ).check("exit_signal")
    stop = _buy_review(_buy_reader(currency="GBP"), buy(BUY_DAY, stop=100.0)).check(
        "evidenced_stop"
    )

    assert same is not None and same.note == "held through signal"
    assert exit_check is not None
    assert (exit_check.status, exit_check.note) == (
        "unknown",
        "no exit signal before the sale",
    )
    assert stop is not None
    assert (stop.status, stop.note) == (
        "unknown",
        "stop and price line currencies differ",
    )


def test_stop_needs_the_exact_entry_session_to_be_restated() -> None:
    bars = [b for b in make_bars(_exit_closes(None)) if b.session != DAYS[80]]
    reader = FakeReader(bars_=tuple(bars))
    zero = list(make_bars(_exit_closes(None)))
    zero[80] = replace(zero[80], split_factor=Decimal(0))

    with_stop = [Entry(buy(DAYS[80], stop=50.0))]
    missing = review_sell(sell(DAYS[120], 1.0), reader, entries=with_stop, history=[])
    no_stop = review_sell(
        sell(DAYS[120], 1.0), reader, entries=[Entry(buy(DAYS[80]))], history=[]
    )
    zeroed = review_sell(
        sell(DAYS[120], 1.0),
        FakeReader(bars_=tuple(zero)),
        entries=with_stop,
        history=[],
    )

    assert statuses(missing)["exit_signal"] == ("unknown", "missing history")
    assert statuses(zeroed)["exit_signal"] == ("unknown", "missing history")
    assert statuses(no_stop)["exit_signal"] == (
        "unknown",
        "no exit signal before the sale",
    )


# --- review fixes: exits ----------------------------------------------------------


@pytest.mark.parametrize("sold", [80, 81], ids=["same-day", "next-session"])
def test_a_sale_with_no_session_after_entry_cannot_be_judged(sold: int) -> None:
    reader = FakeReader(bars_=make_bars(_exit_closes(None)))

    review = review_sell(
        sell(DAYS[sold], 150.0), reader, entries=[Entry(buy(DAYS[80]))], history=[]
    )

    assert statuses(review)["exit_signal"] == (
        "unknown",
        "no session between entry and sale to judge",
    )


def test_open_lot_with_stale_price_history_is_unknown() -> None:
    reader = FakeReader(bars_=make_bars(_exit_closes(150)))
    trade = buy(DAYS[80])

    stale = review_buy(
        trade,
        reader,
        annotation=None,
        lot_open=True,
        history=[],
        today=DAYS[199] + timedelta(days=8),
    )
    fresh = review_buy(
        trade,
        reader,
        annotation=None,
        lot_open=True,
        history=[],
        today=DAYS[199] + timedelta(days=7),
    )

    assert statuses(stale)["exit_signal"] == ("unknown", "price history is stale")
    assert statuses(fresh)["exit_signal"] == ("deviated", "held through signal")


def test_failed_review_marks_every_applicable_check_unknown() -> None:
    from app.agents.trade_review.checklist import failed_review

    open_buy = failed_review(buy(BUY_DAY), lot_open=True)
    sold = failed_review(sell(BUY_DAY, 1.0), lot_open=False)

    assert [c.kind for c in open_buy.checks] == [
        "valid_setup",
        "entry_location",
        "evidenced_stop",
        "exit_signal",
        "strategy_alignment",
        "data_completeness",
    ]
    assert [c.kind for c in sold.checks][:2] == ["exit_signal", "strategy_alignment"]
    for review in (open_buy, sold):
        assert all(c.status == "unknown" for c in review.checks)
        assert review.checks[0].note == "review failed for this trade"
