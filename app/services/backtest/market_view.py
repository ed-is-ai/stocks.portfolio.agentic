"""Concrete no-look-ahead ``MarketView`` bound to one simulated session (AD-3).

Implements the widened :class:`~app.services.backtest.strategy_protocol.
MarketViewV1` seam: a pandas-backed, bounds-checked read surface a future
Backtest Engine (Story 2.4) constructs once per simulated date ``D`` and
hands to a Strategy's ``entry_signals``/``exit_signals``/``position_size``.

``MarketView`` is deliberately *not* a Strategy-runtime module -- it is the
implementation those methods are handed an instance of, never something a
Strategy imports by name -- so, unlike ``strategy_protocol.py``, it is free
to import repositories directly (AD-10's import-boundary guard only walks a
Strategy's own module graph, see
``tests/backtest/test_strategy_runtime_import_boundary.py``).

Construction is deliberately narrow: a caller supplies exactly the
per-security price/action evidence revisions already pinned for this Run
(typically a ``RunInputManifestV1``'s ``securities`` tuple), never "whatever
is currently newest," plus the Run's selected security universe. The view is
scoped to that canonical universe: reading -- or acting on a signal for -- a
security outside it raises :class:`UnselectedSecurityError` rather than being
silently dropped. ``.price_history``/``.scan_result`` never reach past
``as_of_session`` and never silently substitute a different revision.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import date, timedelta
from calendar import monthrange
import logging
from types import MappingProxyType
from typing import Mapping, MutableMapping, Sequence

import pandas as pd

from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import (
    HistoricalEvidenceReadHandle,
    HistoricalPriceRepository,
)
from app.services.backtest.historical_scan_record import HistoricalScanRecordV1
from app.services.backtest.market_planes import (
    HistoricalMarketPlanes,
    MarketDataPolicyError,
)
from app.services.backtest.run_universe import canonical_run_universe
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    SecurityEvidenceCoverageV1,
)
from app.services.backtest.trading_calendar import TradingCalendar

#: Column order every ``MarketView.price_history`` DataFrame uses, whether
#: populated or empty -- a Strategy can rely on this shape regardless of
#: whether ``security_id`` has any evidence.
PRICE_HISTORY_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")

logger = logging.getLogger(__name__)

#: The zero-coverage answer for an unusable security id — evidence
#: coverage is a diagnostic, so it always answers with a value.
_NO_COVERAGE = SecurityEvidenceCoverageV1(security_id="unknown")


class MarketViewBoundError(LookupError):
    """A security's pinned evidence does not itself cover ``as_of_session``.

    Distinct from "no evidence pinned for this security at all" (which
    ``price_history`` answers with an empty DataFrame, not an error): this
    fires only when the view *does* track ``security_id`` but that
    security's pinned evidence interval ends before -- or starts after --
    the view's own ``as_of_session`` bound, so honoring the request would
    mean either silently truncating to less than the caller believes is
    current or reaching past the view's bound. Mirrors
    ``EvidenceMissingError``'s stable-``.code`` convention
    (``historical_price_repo.py``).
    """

    code = "bound_violation"

    def __init__(self, *, security_id: str, as_of_session: date) -> None:
        self.security_id = security_id
        self.as_of_session = as_of_session
        super().__init__(
            f"{security_id!r} has no pinned evidence covering "
            f"{as_of_session.isoformat()}"
        )


class UnselectedSecurityError(LookupError):
    """A security outside this Run's selected universe was acted on.

    Distinct from ``MarketViewBoundError`` (a security this Run *did*
    select whose pinned evidence does not cover ``as_of_session``) and
    from "selected but with no pinned price evidence" (which
    ``price_history`` answers with an empty DataFrame): this fires when a
    read -- or a Strategy signal -- names a security the Run never
    selected, so honoring it would silently widen the universe the Run's
    identity was sealed against. Mirrors ``MarketViewBoundError``'s
    stable-``.code`` convention.
    """

    code = "unselected_security"

    def __init__(self, *, security_id: str, selected_universe: tuple[str, ...]) -> None:
        self.security_id = security_id
        self.selected_universe = selected_universe
        super().__init__(
            f"{security_id!r} is not in this Run's selected universe "
            f"{selected_universe!r}"
        )


@dataclass(frozen=True)
class MarketView:
    """Pandas-backed, bounds-checked market view for one simulated session.

    ``selected_universe`` is the Run's selected security set, canonicalized
    on construction (sorted, deduplicated) so two selection orders of the
    same set build the identical view.

    ``security_price_revisions`` maps each *selected* security that has
    pinned Historical Price/Corporate Action evidence to its exact
    ``data_revision`` (``HistoricalPriceRepository``'s content-addressed
    evidence key). A selected ``security_id`` absent from this mapping has
    no pinned price evidence -- ``.price_history`` returns an empty
    DataFrame and ``.scan_result`` still resolves independently through
    ``backtest_repo`` (scan visibility is not gated on price evidence).
    """

    as_of_session: date
    profile_hash: str
    security_price_revisions: Mapping[str, str]
    selected_universe: tuple[str, ...]
    backtest_repo: BacktestRepository
    historical_price_repo: HistoricalPriceRepository
    prepared_planes: InitVar[Mapping[str, HistoricalMarketPlanes] | None] = None
    prepared_plane_cache: InitVar[
        MutableMapping[str, HistoricalMarketPlanes] | None
    ] = None
    price_accesses: InitVar[Mapping[str, HistoricalEvidenceReadHandle] | None] = None
    scan_cache: InitVar[
        MutableMapping[tuple[str, str], HistoricalScanRecordV1 | None] | None
    ] = None
    scan_cache_month: InitVar[MutableMapping[str, str] | None] = None
    _prepared_planes: Mapping[str, HistoricalMarketPlanes] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _prepared_plane_cache: MutableMapping[str, HistoricalMarketPlanes] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _scan_cache: (
        MutableMapping[tuple[str, str], HistoricalScanRecordV1 | None] | None
    ) = field(default=None, init=False, repr=False, compare=False)
    _scan_cache_month: MutableMapping[str, str] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _price_accesses: Mapping[str, HistoricalEvidenceReadHandle] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(
        self,
        prepared_planes: Mapping[str, HistoricalMarketPlanes] | None,
        prepared_plane_cache: MutableMapping[str, HistoricalMarketPlanes] | None,
        price_accesses: Mapping[str, HistoricalEvidenceReadHandle] | None,
        scan_cache: MutableMapping[tuple[str, str], HistoricalScanRecordV1 | None]
        | None,
        scan_cache_month: MutableMapping[str, str] | None,
    ) -> None:
        # Detach from a caller-supplied dict so later caller-side mutation
        # can never reach this view, matching PortfolioView's convention.
        object.__setattr__(
            self,
            "security_price_revisions",
            MappingProxyType(dict(self.security_price_revisions)),
        )
        object.__setattr__(
            self, "selected_universe", canonical_run_universe(self.selected_universe)
        )
        if (
            prepared_planes is not None
            and type(prepared_planes) is not MappingProxyType
        ):
            object.__setattr__(
                self,
                "_prepared_planes",
                MappingProxyType(dict(prepared_planes)),
            )
        else:
            object.__setattr__(self, "_prepared_planes", prepared_planes)
        object.__setattr__(self, "_prepared_plane_cache", prepared_plane_cache)
        if price_accesses is not None and type(price_accesses) is not MappingProxyType:
            object.__setattr__(
                self, "_price_accesses", MappingProxyType(dict(price_accesses))
            )
        else:
            object.__setattr__(self, "_price_accesses", price_accesses)
        object.__setattr__(self, "_scan_cache", scan_cache)
        object.__setattr__(self, "_scan_cache_month", scan_cache_month)

    def require_selected(self, security_id: str) -> None:
        """Reject ``security_id`` unless this Run selected it.

        The one universe-boundary check every read and every Strategy
        signal passes through before it can affect a Run: an unselected
        security fails loudly here rather than being silently dropped
        further down.
        """
        if security_id not in self.selected_universe:
            raise UnselectedSecurityError(
                security_id=security_id, selected_universe=self.selected_universe
            )

    def price_history(
        self,
        security_id: str,
        *,
        limit: int | None = None,
        columns: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        """Return ``security_id``'s split-continuous OHLCV history through
        ``as_of_session``, oldest first, indexed by session date. ``limit``
        selects the latest rows and ``columns`` selects a canonical subset;
        omitting both preserves the legacy full-history result.

        Uses AD-6's ``split_continuous_as_of_D`` plane -- the one plane a
        Strategy or detector may see: every split effective by
        ``as_of_session`` is already applied, and no future corporate
        action is ever exposed. Values are ``Decimal`` (object dtype),
        matching the deterministic-rounding policy every other AD-6
        consumer in this codebase uses; convert a column explicitly
        (``.astype(float)``) if vectorized numeric libraries are needed.

        Raises :class:`UnselectedSecurityError` if ``security_id`` is
        outside this Run's selected universe, and
        :class:`MarketViewBoundError` if ``security_id`` *is* tracked by
        this view but its pinned evidence interval does not itself cover
        ``as_of_session`` -- never silently truncates to an earlier,
        misleadingly-labeled "current" state.
        """
        self.require_selected(security_id)
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise MarketDataPolicyError(
                "invalid_price_history_request", "limit must be a positive integer."
            )
        try:
            requested_columns = (
                PRICE_HISTORY_COLUMNS if columns is None else tuple(columns)
            )
            valid_column_set = len(set(requested_columns)) == len(requested_columns)
        except (TypeError, ValueError):
            requested_columns = ()
            valid_column_set = False
        if (
            not requested_columns
            or not valid_column_set
            or any(column not in PRICE_HISTORY_COLUMNS for column in requested_columns)
        ):
            raise MarketDataPolicyError(
                "invalid_price_history_request",
                "columns must be a non-empty subset of the canonical price columns.",
            )
        requested_columns = tuple(
            column for column in PRICE_HISTORY_COLUMNS if column in requested_columns
        )
        revision = self.security_price_revisions.get(security_id)
        if revision is None:
            return pd.DataFrame(
                columns=requested_columns,
                index=pd.Index([], dtype=object, name="session"),
            )
        plane = None
        if self._prepared_planes is not None:
            plane = self._prepared_planes.get(security_id)
            if plane is not None and plane.data_revision != revision:
                raise MarketDataPolicyError(
                    "integrity_error",
                    f"Prepared plane for {security_id!r} does not match its pinned revision.",
                )
        if plane is None and self._prepared_plane_cache is not None:
            plane = self._prepared_plane_cache.get(security_id)
            if plane is not None and plane.data_revision != revision:
                raise MarketDataPolicyError(
                    "integrity_error",
                    f"Prepared plane for {security_id!r} does not match its pinned revision.",
                )
        access = (
            None
            if self._price_accesses is None
            else self._price_accesses.get(security_id)
        )
        if access is not None and access.data_revision != revision:
            raise MarketDataPolicyError(
                "integrity_error",
                f"Price access for {security_id!r} does not match its pinned revision.",
            )
        if plane is None and access is None:
            evidence = self.historical_price_repo.get(revision)
            plane = HistoricalMarketPlanes.from_evidence(evidence)
        if access is not None:
            bound_start = date.fromisoformat(access.metadata.start)
            bound_end = date.fromisoformat(access.metadata.end)
        else:
            assert plane is not None
            bound_start, bound_end = plane.start, plane.end
        if not (bound_start <= self.as_of_session < bound_end):
            raise MarketViewBoundError(
                security_id=security_id, as_of_session=self.as_of_session
            )
        if plane is None and access is not None:
            partial = access.bounded(through=access.end - timedelta(days=1))
            if not partial.rows:
                return pd.DataFrame(
                    columns=requested_columns,
                    index=pd.Index([], dtype=object, name="session"),
                )
            plane = HistoricalMarketPlanes.from_bounded_evidence(partial)
            if self._prepared_plane_cache is not None:
                self._prepared_plane_cache[security_id] = plane
        assert plane is not None
        rows = plane.split_continuous_window_as_of(self.as_of_session, limit=limit)
        if not rows:
            return pd.DataFrame(
                columns=requested_columns,
                index=pd.Index([], dtype=object, name="session"),
            )
        frame = pd.DataFrame(
            {
                column: [getattr(row, column) for row in rows]
                for column in requested_columns
            },
            # ``dtype=object`` keeps the index as plain ``datetime.date``
            # values -- pandas would otherwise infer a tz-naive
            # ``DatetimeIndex`` from a list of ``date`` objects, which
            # compares unreliably against plain ``date`` values callers
            # naturally hold (e.g. ``as_of_session`` itself).
            index=pd.Index([row.session for row in rows], dtype=object, name="session"),
            columns=requested_columns,
        )
        return frame

    def scan_result(self, security_id: str) -> HistoricalScanRecordV1 | None:
        """Return the latest committed monthly scan record visible at
        ``as_of_session``, or ``None`` if none is visible yet.

        Delegates entirely to
        ``BacktestRepository.latest_committed_scan_result`` -- the one
        query authority for monthly-scan visibility timing, so this view
        never re-implements the "own recorded month-end
        ``as_of_session_date`` onward, until superseded" rule itself.

        Raises :class:`UnselectedSecurityError` for a security outside
        this Run's selected universe.
        """
        self.require_selected(security_id)
        if self._scan_cache is not None and self._scan_cache_month is not None:
            visible_month = self.as_of_session.strftime("%Y-%m")
            if self._scan_cache_month.get("month") != visible_month:
                self._scan_cache.clear()
                self._scan_cache_month["month"] = visible_month
                self._scan_cache_month.pop("boundary", None)
            month_end = date(
                self.as_of_session.year,
                self.as_of_session.month,
                monthrange(self.as_of_session.year, self.as_of_session.month)[1],
            )
            month_boundaries: set[date] = set()
            if self._prepared_planes:
                calendar = TradingCalendar()
                for plane in self._prepared_planes.values():
                    mic = {
                        "America/New_York": "XNYS",
                        "Europe/London": "XLON",
                    }.get(plane.exchange_timezone)
                    if mic is not None:
                        month_boundaries.add(
                            calendar.last_session_of_month(mic, visible_month)
                        )
            if not month_boundaries:
                while month_end.weekday() >= 5:
                    month_end -= timedelta(days=1)
                month_boundaries.add(month_end)
            if (
                self.as_of_session in month_boundaries
                and self._scan_cache_month.get("boundary")
                != self.as_of_session.isoformat()
            ):
                self._scan_cache.clear()
                self._scan_cache_month["boundary"] = self.as_of_session.isoformat()
            cache_key = (self.profile_hash, security_id)
            if cache_key not in self._scan_cache:
                self._scan_cache[cache_key] = (
                    self.backtest_repo.latest_committed_scan_result(
                        profile_hash=self.profile_hash,
                        security_id=security_id,
                        as_of_session=self.as_of_session,
                    )
                )
            return self._scan_cache[cache_key]
        return self.backtest_repo.latest_committed_scan_result(
            profile_hash=self.profile_hash,
            security_id=security_id,
            as_of_session=self.as_of_session,
        )

    @property
    def evidence_capabilities(self) -> frozenset[EvidenceKind]:
        """Declare every evidence kind — the full pinned historical plane.

        A backtest Run pins both split-continuous price evidence and the
        monthly-scan detector fragments, so a Strategy requiring
        stage/VCP/technicals is fully supported here (#471). Structural
        capability only: per-security availability is answered by
        :meth:`evidence_coverage`.
        """
        return frozenset(EvidenceKind)

    def evidence_coverage(self, security_id: str) -> SecurityEvidenceCoverageV1:
        """Return ``security_id``'s coverage without ever raising (#471).

        Preflight is a read-only capability question, not a Strategy read:
        *any* failure — an unselected security, a bound violation, a
        repository or frame error — answers "no coverage" rather than
        propagating, so one security's problem stays a per-security
        diagnostic instead of failing the whole evaluation. The view's own
        bounds and pinned provenance are untouched — this method only
        observes what the existing bounded reads already return.
        """
        if not security_id:
            # ``SecurityEvidenceCoverageV1`` requires a non-empty id; a
            # method contracted never to raise must not raise here either.
            return _NO_COVERAGE
        try:
            frame = self.price_history(security_id)
        except Exception:
            logger.debug("No price coverage for %r", security_id, exc_info=True)
            return SecurityEvidenceCoverageV1(security_id=security_id)
        kinds: set[EvidenceKind] = set()
        sessions = int(len(frame.index))
        columns = tuple(str(column) for column in frame.columns)
        if sessions:
            kinds.add(EvidenceKind.PRICE_HISTORY)
        else:
            columns = ()
        try:
            record = self.scan_result(security_id)
        except Exception:
            logger.debug("No scan coverage for %r", security_id, exc_info=True)
            record = None
        if record is not None:
            if getattr(record, "stage", None) is not None:
                kinds.add(EvidenceKind.SCAN_STAGE)
            if getattr(record, "vcp", None) is not None:
                kinds.add(EvidenceKind.SCAN_VCP)
            if getattr(record, "technicals", None) is not None:
                kinds.add(EvidenceKind.SCAN_TECHNICALS)
        return SecurityEvidenceCoverageV1(
            security_id=security_id,
            kinds=frozenset(kinds),
            sessions=sessions,
            columns=columns,
        )


__all__ = [
    "MarketView",
    "MarketViewBoundError",
    "PRICE_HISTORY_COLUMNS",
    "UnselectedSecurityError",
]
