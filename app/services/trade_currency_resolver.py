"""Resolve the currency a ticker's *trades* were priced in, from evidence (#553).

``ticker_currency_cache`` -- what ``TradesRepository._REPLAY_CURRENCY`` reads
(#549) -- stores the currency a ticker is *quoted* in. For a genuinely foreign
listing (9988 in HKD) that is also the currency its trades were priced in, and
converting a cost basis by it is right. For a sterling-priced holding whose
canonical ticker resolves to a foreign line (HSFWA, SGLN and AZN all cache as
``USD`` while their SIPP rows are in pounds) it is wrong, and dividing the cost
basis by the GBP/USD rate understates it by ~20%.

The test that separates the two is arithmetic, not metadata: compare a trade's
own price with the instrument's dated GBP close on that trade's date. If the
raw price is closer, the trade was already sterling; if ``price / rate`` is
closer, it was in the candidate currency. One trade could be a coincidence, so
a handful vote, each only when its winner beats the loser by a margin, and the
majority wins -- against production data every voting ticker scored 6/6 one way
or the other. A tie, or a date the margin cannot separate, is *no verdict*: the
candidate stands untouched and nothing is stored. The one exception is an
instrument with no dated close at all and no broker currency flag, which falls
back to sterling for the run without ever being recorded (see :meth:`resolve`),
so this can only correct a currency from evidence, never invent one from its
absence.
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.repositories.trade_currency_repo import TradeCurrencyRepository
from app.repositories.trades_repo import TradesRepository

logger = logging.getLogger(__name__)

#: How many dated trades vote. Six is what the production check used and is
#: ample for a unanimous signal; the cost of each is two cached lookups.
_MAX_VOTES = 6


class _PriceSource(Protocol):
    """The dated-evidence subset of ``snapshot_repair.HistoricalGbpPriceSource``."""

    def gbp_price(self, ticker: str, as_of: str) -> float | None: ...

    def gbp_rate(self, currency: str, as_of: str) -> float | None: ...


class TradeCurrencyResolver:
    """Decides, once per ticker, which currency its trade prices are in.

    ``repo`` makes a verdict durable so the vote is paid once per ticker
    across runs; omit it (tests, a throwaway pass) and the memo lives only
    for this instance's lifetime. A stored verdict is always trusted over a
    fresh vote -- re-deciding per run is what would let a ticker's whole
    history silently re-denominate on a day of thin evidence.
    """

    def __init__(
        self,
        source: _PriceSource,
        repo: TradeCurrencyRepository | None = None,
        trades: TradesRepository | None = None,
    ) -> None:
        self._source = source
        self._repo = repo
        # Only consulted when the vote finds no evidence at all; without it
        # an unverifiable candidate simply stands, as it did before #553.
        self._trades = trades
        self._verdicts: dict[str, str] | None = None
        # ``{ticker: quote currency}``, with ``""`` meaning "asked, nothing
        # foreign quoted" so a miss is never re-queried.
        self._quotes: dict[str, str] = {}

    def resolve(
        self, ticker: str, candidate: str, trades: list[tuple[str, float]]
    ) -> str:
        """Return the currency ``ticker``'s trades were priced in.

        ``candidate`` is today's answer (``_REPLAY_CURRENCY``, #549) and is
        what comes back whenever the evidence cannot better it -- including
        for a ``GBP`` candidate, which is never voted on at all: there is
        nothing to correct, and the vote would cost lookups on every
        sterling holding in the portfolio.

        ``trades`` are that ticker's ``(date, price)`` pairs, used only when
        no trades repository was supplied: with one, the sample comes from
        :meth:`TradesRepository.recent_trade_prices` instead, because the
        currency is a global fact about the listing and a caller's rows are
        one portfolio's replay -- and, for a ticker flagged ``USD`` on a
        single row and ``GBP`` on the rest (TSLA, T, PENN, TDOC, LULU), one
        row is the whole sample. Either way the most recent
        :data:`_MAX_VOTES` usable dates vote, because a stock split makes an
        adjusted historical close disagree with an unadjusted old fill.

        With *no dated close at all* the candidate's own provenance decides
        (:meth:`TradesRepository.has_foreign_currency_flag`): a stored
        non-GBP flag is the broker's own statement about the trade and
        stands, but a candidate that came only from the quote cache
        resolves to ``GBP`` -- that cache guesses ``USD`` for any
        unsuffixed symbol, and a SEDOL-only UK fund line (``B39RMM8``,
        ``0606196``) is not a Yahoo symbol at all. GBP is the SIPP's
        accounting currency and what the whole history assumed before #549,
        so it is the safe answer, not a new inference. That verdict is
        memoised but never *persisted*: it rests on the absence of evidence,
        and a cold price cache (or ``--no-historical-evidence``) must not be
        able to write a row that outranks the quote cache for good -- 9988
        is flagged ``GBP`` on all five of its rows and would come back
        carrying ~10x its market value. A tie, and a date whose close exists
        but whose rate is missing, are *not* this case: the instrument is
        priceable, so the candidate simply stands.
        """
        unit = candidate.strip().upper()
        stored = self._stored().get(ticker)
        if unit == "GBP":
            # A sterling candidate is not the same as a sterling answer: for
            # a ticker with no cache row and no broker flag -- ~60 of this
            # account's US holdings -- ``GBP`` is what the fallback chain
            # ran out of options and returned, not something anyone
            # observed. Put it to the same vote against whatever the
            # instrument is quoted in, and if nothing foreign is quoted,
            # sterling stands for free.
            if stored is not None:
                return stored
            hypothesis = self._hypothesis(ticker)
            if hypothesis is None:
                return "GBP"
            return self._decide(ticker, hypothesis, trades, default="GBP")
        if stored is not None:
            return stored
        return self._decide(ticker, unit, trades, default=unit)

    def _decide(
        self,
        ticker: str,
        rival: str,
        trades: list[tuple[str, float]],
        default: str,
    ) -> str:
        """Vote ``GBP`` against ``rival`` and return the answer (#553).

        ``default`` is what stands when the evidence cannot better it -- the
        candidate itself, so an inconclusive vote never moves a ticker. Only
        a verdict an actual vote reached is persisted; see :meth:`resolve`.
        """
        stored = self._stored().get(ticker)
        if stored is not None:
            return stored
        sample = (
            self._trades.recent_trade_prices(ticker)
            if self._trades is not None
            else trades
        )
        verdict, evidence, voted, any_close = self._vote(ticker, rival, sample)
        if verdict is None and (
            voted or any_close or not self._has_gbp_only_flags(ticker)
        ):
            logger.info(
                "trade currency for %s: no verdict (%s), keeping %s",
                ticker,
                evidence,
                default,
            )
            return default
        if verdict is None:
            verdict, evidence = "GBP", "no dated close; no broker currency flag"
        logger.info(
            "trade currency for %s resolved to %s (%s)", ticker, verdict, evidence
        )
        self._stored()[ticker] = verdict
        # Only an actual vote is durable. A no-evidence verdict is a
        # this-run assumption, and persisting it would freeze a cold cache's
        # guess into the highest-priority currency source there is.
        if self._repo is not None and voted:
            self._repo.upsert(ticker, verdict, evidence)
        return verdict

    def prime(self, tickers: list[str]) -> None:
        """Batch-load the rival hypotheses for a run's tickers (#553).

        One query instead of one per ticker. Optional: an unprimed resolver
        asks per ticker and memoises the answer, which is what a test or a
        single lookup wants.
        """
        if self._trades is None:
            return
        quotes = self._trades.quote_currencies(list(dict.fromkeys(tickers)))
        for ticker in tickers:
            self._quotes.setdefault(ticker, quotes.get(ticker, ""))

    def _hypothesis(self, ticker: str) -> str | None:
        """Return the non-GBP currency ``ticker`` is quoted in, or None."""
        if ticker not in self._quotes:
            self._quotes[ticker] = (
                ""
                if self._trades is None
                else self._trades.quote_currencies([ticker]).get(ticker, "")
            )
        return self._quotes[ticker] or None

    def without_persistence(self) -> "TradeCurrencyResolver":
        """Return a twin of this resolver that stores no verdict (#553).

        For ``SnapshotRepairService.repair(dry_run=True)``, whose contract is
        that nothing is written: the conversion runs before every
        ``if not dry_run`` guard, so the resolver's upsert was a write the
        dry run promised not to make. The twin starts from a *copy* of the
        memo -- it reuses what is already known, but its own verdicts do not
        leak back, so the real pass that follows still records them.
        """
        twin = TradeCurrencyResolver(self._source, None, self._trades)
        twin._verdicts = dict(self._stored())
        twin._quotes = dict(self._quotes)
        return twin

    def _stored(self) -> dict[str, str]:
        """Return the memo of verdicts, loading the stored ones once."""
        if self._verdicts is None:
            self._verdicts = self._repo.get_all() if self._repo is not None else {}
        return self._verdicts

    def _has_gbp_only_flags(self, ticker: str) -> bool:
        """True when no stored trade for ``ticker`` carries a non-GBP flag.

        False without a trades repository too: unable to check provenance is
        not the same as having checked it, and the candidate then stands.
        """
        return self._trades is not None and not self._trades.has_foreign_currency_flag(
            ticker
        )

    def _split_factor(self, ticker: str, day: str) -> float | None:
        """Return the split factor to restore ``day``'s share definition.

        ``1.0`` when the source cannot supply one at all -- a price source
        predating #555, or a test double -- so an unsplit instrument and an
        unaware source both compare exactly as they did before. ``None``
        only when the source *can* answer and says it does not know this
        symbol, which is a reason to skip the date rather than compare two
        different share definitions.
        """
        lookup = getattr(self._source, "split_factor_since", None)
        if lookup is None:
            return 1.0
        return lookup(ticker, day)

    def _vote(
        self, ticker: str, candidate: str, trades: list[tuple[str, float]]
    ) -> tuple[str | None, str, bool, bool]:
        """Return ``(verdict, evidence, voted, any_close)``.

        A None verdict means no answer. ``voted`` is True when at least one
        date was decided, which separates a genuine tie from no evidence at
        all; ``any_close`` is True when *some* date had a dated close, which
        separates "this instrument is not priceable at all" (the SEDOL-only
        fund line) from "the FX series has a hole".

        A trade only votes when its date carries *both* a positive dated GBP
        close and a dated rate -- with only one of the two there is nothing
        to compare -- and only when one side wins by a margin: the winner's
        error must be at most half the loser's. A bare nearest-match is
        decided by a ~10% gap between a fill and that day's close, and a
        split *after* the last trade puts every date on the wrong side (a
        4:1 split scores 52.8 raw against 38.8 converted, so "converted"
        would win and divide the cost basis by the rate for good). An
        ambiguous date abstains instead. Dates are deduplicated and walked
        newest first, so the answer cannot change with row order.
        """
        gbp = foreign = 0
        any_close = False
        splits_applied = False
        # ``sorted`` before the dict so a date holding two trades keeps one
        # of them by value, not by however the rows happened to arrive.
        by_day = dict(sorted(trades))
        for day, price in sorted(by_day.items(), reverse=True):
            if gbp + foreign >= _MAX_VOTES:
                break
            if price is None or price <= 0:
                continue
            close = self._source.gbp_price(ticker, day)
            if close is None or close <= 0:
                continue
            any_close = True
            rate = self._source.gbp_rate(candidate, day)
            if rate is None or rate <= 0:
                continue
            # The stored close is adjusted for every split since; the price
            # paid is not. Restore the day's own share definition before
            # comparing, or a holding that split afterwards has every date
            # abstain -- TSLA's 2021 fills sit near 690 against closes near
            # 171, because of a 3:1 split in 2022 (#555).
            factor = self._split_factor(ticker, day)
            if factor is None:
                continue
            close *= factor
            splits_applied = splits_applied or factor != 1.0
            raw_error = abs(price - close)
            converted_error = abs(price / rate - close)
            if raw_error * 2 <= converted_error:
                gbp += 1
            elif converted_error * 2 <= raw_error:
                foreign += 1
        note = ", split-adjusted" if splits_applied else ""
        if gbp == foreign:
            return (
                None,
                (f"tied {gbp}-{foreign}" if gbp else "no conclusive dated trade")
                + note,
                bool(gbp),
                any_close,
            )
        return (
            ("GBP" if gbp > foreign else candidate),
            f"{gbp} GBP vs {foreign} {candidate} of {gbp + foreign} dated trades{note}",
            True,
            any_close,
        )
