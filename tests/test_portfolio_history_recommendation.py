from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pandas as pd

from app.repositories import db
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.repositories.portfolio_strategies_repo import PortfolioStrategiesRepository
from app.schemas.analysis_artifact import build_analysis_payload
from app.schemas.portfolio_recommendation import RecommendationResultV1
from app.schemas.trade import Position
from app.services import portfolio_recommendation_service as service_module
from app.services import strategy_assignment_service as assignment_module
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.scan_view import read_portfolio_history
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
)
from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)
from app.services.portfolio_recommendation_service import (
    PortfolioRecommendationService,
)
from app.services.snapshot_price_backfill import PriceEvidenceBackfillService
from tests.test_portfolio_recommendation_service import _discovery_result

AS_OF = date(2026, 9, 11)
ALIASES = {"WCOG": "WCOG.L", "HSFWA": "0P00013P6I.L"}


class _Ticker:
    def __init__(
        self,
        symbol: str,
        currency: str,
        sessions: tuple[date, ...],
        falling: bool = False,
    ) -> None:
        self._symbol = symbol
        self._currency = currency
        self._sessions = sessions
        self._falling = falling

    def history(self, **_: object) -> pd.DataFrame:
        close = 1416.0 if self._currency == "GBp" else 10.0
        closes = [close - index if self._falling else close + index for index in range(len(self._sessions))]
        return pd.DataFrame(
            {
                "Open": closes,
                "High": [value + 2 for value in closes],
                "Low": [value - 1 for value in closes],
                "Close": closes,
                "Adj Close": closes,
                "Volume": [1000.0] * len(self._sessions),
                "Dividends": [0.0] * len(self._sessions),
                "Stock Splits": [0.0] * len(self._sessions),
            },
            index=pd.DatetimeIndex(self._sessions, tz="Europe/London"),
        )

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        del repair
        return {
            "symbol": self._symbol,
            "currency": self._currency,
            "exchangeTimezoneName": "Europe/London",
        }


def _repo(tmp_path: Path) -> HistoricalPriceRepository:
    repo = HistoricalPriceRepository(
        db.make_connect(lambda: tmp_path / "historical-prices.db")
    )
    repo.ensure_schema()
    return repo


def _commit_history(
    repo: HistoricalPriceRepository,
    symbol: str,
    sessions: tuple[date, ...],
    *,
    currency: str = "GBP",
    quote_unit: str = "GBP",
    falling: bool = False,
    end: date | None = None,
) -> str:
    request = HistoricalEvidenceRequest(
        security_id=f"portfolio:{symbol}",
        alias_revision="aliases-v1",
        symbol=symbol,
        start=sessions[0],
        end=end or sessions[-1] + timedelta(days=1),
        expected_currency=currency,
        expected_quote_unit=quote_unit,
        expected_timezone="Europe/London",
        expected_sessions=sessions,
        allowed_observed_symbols=(symbol,),
        canonical_exchange_sessions=True,
    )
    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _: _Ticker(
            symbol,
            "GBp" if quote_unit == "GBp" else currency,
            sessions,
            falling,
        ),
        provider_version="test",
    ).fetch(request)
    return repo.commit(payload)


def test_portfolio_reader_resolves_wcog_and_normalizes_gbpence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    revision = _commit_history(repo, "WCOG.L", (date(2026, 9, 10), AS_OF), quote_unit="GBp")

    result = read_portfolio_history(
        repo, "WCOG", ALIASES, through=AS_OF, minimum_sessions=2
    )

    assert result.available
    assert revision in result.evidence_details[0]
    assert result.history is not None
    assert list(result.history.index) == [date(2026, 9, 10), AS_OF]
    assert result.history["close"].tolist() == [
        Decimal("14.160"),
        Decimal("14.170"),
    ]
    assert "requested_symbol=WCOG.L" in result.evidence_details[0]
    assert "quote_unit=GBp" in result.evidence_details[0]
    assert "quote_unit_scale=0.01" in result.evidence_details[0]
    assert "provider_version=test" in result.evidence_details[0]
    assert "request_contract_version=" in result.evidence_details[0]


def test_portfolio_reader_resolves_hsfwa_and_excludes_later_rows(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit_history(
        repo,
        "0P00013P6I.L",
        (date(2026, 9, 10), AS_OF, date(2026, 9, 12)),
        currency="USD",
        quote_unit="USD",
    )

    result = read_portfolio_history(
        repo, "HSFWA", ALIASES, through=AS_OF, minimum_sessions=2
    )

    assert result.available
    assert result.security_id == "0P00013P6I.L"
    assert result.history is not None
    assert list(result.history.index) == [date(2026, 9, 10), AS_OF]
    assert all("2026-09-12" not in detail for detail in result.evidence_details)


def test_portfolio_reader_rejects_crossed_identity_and_missing_history(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    crossed = read_portfolio_history(
        repo, "WCOG", ALIASES, through=AS_OF, minimum_sessions=2
    )

    assert not crossed.available
    assert crossed.error_code == "evidence_missing"
    assert crossed.evidence_details == (
        "portfolio_history: status=unavailable; reason=evidence_missing",
    )


def test_portfolio_reader_rejects_mismatched_revision_identity() -> None:
    handle = MagicMock(
        metadata=SimpleNamespace(
            security_id="portfolio:OTHER.L",
            requested_symbol="OTHER.L",
            observed_symbol="OTHER.L",
        )
    )
    repo = MagicMock()
    repo.covering_revision.return_value = "revision"
    repo.open_read.return_value = handle

    result = read_portfolio_history(
        repo, "WCOG", ALIASES, through=AS_OF, minimum_sessions=2
    )

    assert not result.available
    assert result.error_code == "integrity_error"
    handle.close.assert_called_once_with()


def test_portfolio_reader_returns_typed_failure_for_unrepresentable_bound(
    tmp_path: Path,
) -> None:
    result = read_portfolio_history(
        _repo(tmp_path), "WCOG.L", {}, through=date.max, minimum_sessions=2
    )

    assert not result.available
    assert result.error_code == "integrity_error"


class _HistoryStrategy:
    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        del parameters
        history = EvidenceRequirementV1(
            kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=2, columns=("close",)
        )
        return StrategyEvidenceRequirementsV1(entry=(history,), exit=(history,))

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        return [
            Signal(
                security_id=security_id,
                side=SignalSide.BUY,
                session=view.as_of_session,
                rule_id="entry",
            )
            for security_id in cast(list[str], parameters["selected_securities"])
            if view.price_history(security_id)["close"].iloc[-1]
            > view.price_history(security_id)["close"].iloc[0]
        ]

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        return [
            Signal(
                security_id=position.security_id,
                side=SignalSide.SELL,
                session=view.as_of_session,
                rule_id="exit",
            )
            for position in portfolio.positions
            if position.security_id in parameters["selected_securities"]
            and not view.price_history(position.security_id).empty
            and view.price_history(position.security_id)["close"].iloc[-1]
            < view.price_history(position.security_id)["close"].iloc[0]
        ]

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        del signal, view, portfolio, parameters
        return 1


class _VolumeHistoryStrategy(_HistoryStrategy):
    """History strategy whose exit explicitly requires volume evidence."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        del parameters
        history = EvidenceRequirementV1(
            kind=EvidenceKind.PRICE_HISTORY,
            minimum_sessions=2,
            columns=("close", "volume"),
        )
        return StrategyEvidenceRequirementsV1(entry=(history,), exit=(history,))


def _record(ticker: str, closes: tuple[float, ...]) -> dict[str, Any]:
    sessions = (date(2026, 9, 10), AS_OF)
    return {
        "ticker": ticker,
        "as_of": AS_OF.isoformat(),
        "price": closes[-1],
        "volume": 1000,
        "rel_volume": 1.0,
        "high_52w": 13.0,
        "low_52w": 9.0,
        "pct_from_52w_high": -1.0,
        "pct_change_week": 0.5,
        "ohlcv_history": [
            {
                "date": session.isoformat(),
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1000,
            }
            for session, close in zip(reversed(sessions), reversed(closes), strict=True)
        ],
    }


def _recommendation_service(
    tmp_path: Path,
    repo: HistoricalPriceRepository,
    positions: list[Position],
    monkeypatch: Any,
    strategy: Any | None = None,
    repair_factory: Any | None = None,
) -> PortfolioRecommendationService:
    analysis = tmp_path / "analysis.json"
    analysis.write_text(
        json.dumps(
            build_analysis_payload(
                [_record("AAA", (10.0, 12.0))],
                run_id="run-1",
                generated_at=datetime.now(UTC),
            )
        ),
        encoding="utf-8",
    )
    trades = tmp_path / "trades.db"
    with sqlite3.connect(trades) as connection:
        db.init_trades_db(connection)
        connection.execute(
            "INSERT INTO portfolios (id, name, created_at) VALUES (7, 'SIPP', 'now')"
        )
    monkeypatch.setattr(assignment_module, "discover_strategies", lambda root: _discovery_result())
    monkeypatch.setattr(service_module, "ANALYSIS_JSON", analysis)
    monkeypatch.setattr(service_module, "load_aliases", lambda: ALIASES)
    assignment = assignment_module.StrategyAssignmentService(
        PortfolioStrategiesRepository(db.make_connect(lambda: trades)),
        skills_root=tmp_path / "skills",
        analysis_path=analysis,
    )
    assignment.assign(7, "alpha")
    trader = MagicMock()
    trader.get_portfolio.return_value = positions
    trader.get_cash_balance.return_value = 1000.0
    return PortfolioRecommendationService(
        assignment_service=assignment,
        trader=trader,
        skills_root=tmp_path / "skills",
        loader=lambda path: strategy or _HistoryStrategy(),
        historical_price_repo=repo,
        repair_factory=repair_factory,
    )


def _position(ticker: str) -> Position:
    return Position(ticker=ticker, shares=1.0, avg_cost=10.0, total_cost=10.0)


def test_recommendation_uses_fallback_only_for_exit_and_keeps_friendly_symbol(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = _repo(tmp_path)
    _commit_history(
        repo,
        "WCOG.L",
        (date(2026, 9, 10), AS_OF),
        quote_unit="GBp",
        falling=True,
    )
    read_repo = MagicMock(wraps=repo)
    service = _recommendation_service(
        tmp_path, read_repo, [_position("WCOG")], monkeypatch
    )

    result = service.recommend(7)

    assert isinstance(result, RecommendationResultV1)
    assert result.parameters["selected_securities"] == ["AAA"]
    assert [(item.action, item.ticker, item.security_id) for item in result.recommendations] == [
        ("sell", "WCOG", "WCOG.L"),
        ("buy", "AAA", "AAA"),
    ]
    assert result.coverage.exit_state == "compatible"
    read_repo.commit.assert_not_called()
    read_repo.ensure_schema.assert_not_called()


def test_recommendation_uses_hsfwa_fallback_and_canonical_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = _repo(tmp_path)
    _commit_history(
        repo,
        "0P00013P6I.L",
        (date(2026, 9, 10), AS_OF),
        falling=True,
    )
    service = _recommendation_service(
        tmp_path, repo, [_position("HSFWA")], monkeypatch
    )

    result = service.recommend(7)

    assert isinstance(result, RecommendationResultV1)
    assert [(item.action, item.ticker, item.security_id) for item in result.recommendations] == [
        ("sell", "HSFWA", "0P00013P6I.L"),
        ("buy", "AAA", "AAA"),
    ]


def test_fallback_gap_is_hold_with_diagnostic_and_repeatable(tmp_path: Path, monkeypatch: Any) -> None:
    repo = _repo(tmp_path)
    _commit_history(repo, "WCOG.L", (date(2026, 9, 9), AS_OF), quote_unit="GBp")
    service = _recommendation_service(tmp_path, repo, [_position("WCOG")], monkeypatch)

    first = service.recommend(7)
    second = service.recommend(7)

    assert isinstance(first, RecommendationResultV1)
    assert isinstance(second, RecommendationResultV1)
    assert first.model_dump(exclude={"evaluated_at"}) == second.model_dump(
        exclude={"evaluated_at"}
    )
    assert first.recommendations[0].action == "hold"
    assert first.recommendations[0].rule_id == "evidence_incomplete"
    assert first.coverage.diagnostics[0].missing_session_ranges == ("2026-09-10",)


def test_evaluation_carries_a_five_session_trailing_gap_ephemerally(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = _repo(tmp_path)
    _commit_history(
        repo,
        "WCOG.L",
        (date(2026, 9, 8), date(2026, 9, 9)),
        quote_unit="GBp",
        falling=True,
        end=AS_OF + timedelta(days=1),
    )

    carried = read_portfolio_history(
        repo,
        "WCOG",
        ALIASES,
        through=AS_OF,
        minimum_sessions=2,
        carry_forward_sessions=5,
        repair_outcome="repair_incomplete",
    )

    assert carried.available
    assert carried.history is not None
    assert list(carried.history.index) == [
        date(2026, 9, 8),
        date(2026, 9, 9),
        date(2026, 9, 10),
        AS_OF,
    ]
    assert carried.history["close"].tolist()[-2:] == [
        Decimal("14.150"),
        Decimal("14.150"),
    ]
    assert carried.history["volume"].tolist()[-2:] == [None, None]
    assert any("carry_forward_policy=" in detail for detail in carried.evidence_details)
    assert any("repair_outcome=repair_incomplete" in detail for detail in carried.evidence_details)

    service = _recommendation_service(
        tmp_path, repo, [_position("WCOG")], monkeypatch
    )
    result = service.recommend_for_evaluation(7)

    assert isinstance(result, RecommendationResultV1)
    assert result.recommendations[0].action == "sell"
    assert result.recommendations[0].security_id == "WCOG.L"
    assert result.coverage.exit_state == "compatible"


def test_carried_ohlc_cannot_satisfy_volume_requirement(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = _repo(tmp_path)
    _commit_history(
        repo,
        "0P00013P6I.L",
        (date(2026, 9, 8), date(2026, 9, 9)),
        currency="USD",
        quote_unit="USD",
        falling=True,
        end=AS_OF + timedelta(days=1),
    )
    service = _recommendation_service(
        tmp_path,
        repo,
        [_position("HSFWA")],
        monkeypatch,
        strategy=_VolumeHistoryStrategy(),
    )

    result = service.recommend_for_evaluation(7)

    assert isinstance(result, RecommendationResultV1)
    held = next(item for item in result.recommendations if item.security_id == "0P00013P6I.L")
    assert held.action == "hold"
    assert held.rule_id == "evidence_incomplete"
    assert result.coverage.diagnostics[0].missing_columns == ("volume",)


def test_evaluation_repairs_hsfwa_before_reading_exit_history(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = _repo(tmp_path)
    expected = TradingCalendar().sessions_in_range(
        "XLON", AS_OF - timedelta(days=21), AS_OF + timedelta(days=1)
    )[-7:]
    repair = PriceEvidenceBackfillService(
        repo,
        YFinanceHistoricalEvidenceAdapter(
            lambda symbol: _Ticker(symbol, "USD", expected, falling=True),
            provider_version="repair-test",
        ),
        aliases=ALIASES,
    )
    service = _recommendation_service(
        tmp_path,
        repo,
        [_position("HSFWA")],
        monkeypatch,
        repair_factory=lambda: repair,
    )

    result = service.recommend_for_evaluation(7)

    assert isinstance(result, RecommendationResultV1)
    assert [(item.action, item.security_id) for item in result.recommendations] == [
        ("sell", "0P00013P6I.L"),
        ("buy", "AAA"),
    ]
    revision = repo.covering_revision(
        security_id="portfolio:0P00013P6I.L",
        requested_symbol="0P00013P6I.L",
        start=expected[0].isoformat(),
        end=(AS_OF + timedelta(days=1)).isoformat(),
    )
    assert revision is not None
    assert repo.get(revision).provider_version == "repair-test"


def test_six_exchange_session_gap_stays_hold(tmp_path: Path) -> None:
    expected = TradingCalendar().sessions_in_range(
        "XLON", AS_OF - timedelta(days=21), AS_OF + timedelta(days=1)
    )[-7:]
    repo = _repo(tmp_path)
    _commit_history(
        repo,
        "WCOG.L",
        (expected[0],),
        quote_unit="GBp",
        end=AS_OF + timedelta(days=1),
    )

    result = read_portfolio_history(
        repo,
        "WCOG",
        ALIASES,
        through=AS_OF,
        minimum_sessions=2,
        carry_forward_sessions=5,
        repair_outcome="repair_incomplete",
    )

    assert not result.available
    assert result.error_code == "missing_sessions"
    assert any(
        "repair_outcome=repair_incomplete" in detail
        for detail in result.evidence_details
    )
