"""Composed clean-checkout journey test (Story 4.9).

One deterministic end-to-end test chains every supported Strategy Manager
activity through the real app engines/services -- schema creation, Bootstrap,
historical initialization, roster-backed universe selection, evidence
preparation, and the Backtest itself -- proving the whole journey documented
in ``docs/strategy-manager/onboarding.md`` still works from an empty database,
not just in isolated per-stage unit tests.

Building blocks are reused verbatim from two existing per-stage test files
rather than reinvented:

- ``tests/backtest/test_strategy_bootstrap_service.py`` (``_empty_repo``,
  ``_create_bootstrap_stage_job``, ``build_stage_walk_engine`` for Bootstrap,
  ``StrategyProviderBundleV1.fixture()``).
- ``tests/backtest/test_backtest_worker.py`` (``_repo``/schema pattern,
  ``build_backtest_engine``) -- but this file deliberately does *not* reuse
  that file's ``_patch_strategy_resolution`` monkeypatch shortcut: real
  Strategy discovery must run against the actual ``skills/`` directory, per
  the story's intent-contract.

Only ``StrategyProviderBundleV1.fixture()`` evidence is used for Bootstrap;
no production/network provider path is exercised.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from app.core import config
from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.fx_quote_repo import FxQuote, FxQuoteRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.historical_data_qualification import QualificationRunner
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.run_universe import (
    canonical_run_universe,
    run_universe_digest,
)
from app.services.backtest.skill_discovery import discover_strategies
from app.services.backtest.strategy_bootstrap_service import (
    StrategyProviderBundleV1,
    _production_probes,
    _qualification_fixture_path,
)
from app.services.backtest.strategy_job import (
    InitializationSubmissionV1,
    JobFailureCode,
    PreparationSubmissionV1,
    RunUniverseSelectionV1,
    StrategyJobStatus,
    StrategyJobType,
)
from app.services.backtest.strategy_job_service import StrategyJobService
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.worker import (
    build_backtest_engine,
    build_initialization_engine,
    build_stage_walk_engine,
)

NOW = datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)
INITIALIZATION_MONTH = "2026-06"
STRATEGY_ID = "rtly-backtest-buy-and-hold"
FX_PAIR = "GBPUSD=X"


def _empty_repo(path: Path) -> BacktestRepository:
    """A fresh Strategy Manager store created only via the app's own
    schema-creation path -- no hand-seeded rows (per the story's Boundary)."""
    repo = BacktestRepository(
        db.make_connect(lambda: str(path)),
        clock=lambda: NOW.date(),
        instant_clock=lambda: NOW,
    )
    repo.ensure_schema()
    return repo


def _create_bootstrap_stage_job(repo: BacktestRepository):
    """Same private test helper the existing Bootstrap-stage tests already
    use (see ``tests/backtest/test_strategy_bootstrap_service.py:413``)."""
    return repo._create_stage_job(StrategyJobType.BOOTSTRAP, None)


class _FakeTicker:
    """Deterministic offline stand-in for a yfinance ``Ticker`` (matches the
    pattern in ``tests/backtest/test_backtest_worker.py``'s ``_FakeTicker``)."""

    def __init__(
        self, frame: pd.DataFrame, symbol: str, currency: str, timezone_name: str
    ) -> None:
        self._frame = frame
        self._symbol = symbol
        self._currency = currency
        self._timezone_name = timezone_name

    def history(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame.copy()

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        return {
            "symbol": self._symbol,
            "currency": self._currency,
            "exchangeTimezoneName": self._timezone_name,
        }


def _flat_frame(sessions: tuple[date, ...], tz: str) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(session) for session in sessions], tz=tz)
    return pd.DataFrame(
        {
            "Open": [100.0] * len(sessions),
            "High": [101.0] * len(sessions),
            "Low": [99.0] * len(sessions),
            "Close": [100.5] * len(sessions),
            "Adj Close": [100.5] * len(sessions),
            "Volume": [1_000.0] * len(sessions),
            "Dividends": [0.0] * len(sessions),
            "Stock Splits": [0.0] * len(sessions),
        },
        index=index,
    )


def _initialization_evidence_adapter() -> YFinanceHistoricalEvidenceAdapter:
    """Deterministic fake yfinance evidence for the Fixture roster's two
    securities (AAPL/XNAS/USD and ULVR.L/XLON/GBp) -- 300 flat trading
    sessions ending at each security's exchange month-end, enough lookback
    for a genuine (non-excluded) monthly scanner reconstruction rather than
    a legitimate-history-insufficient exclusion.
    """
    calendar = TradingCalendar()
    target_us = calendar.last_session_of_month("XNAS", INITIALIZATION_MONTH)
    target_uk = calendar.last_session_of_month("XLON", INITIALIZATION_MONTH)
    us_sessions = calendar.sessions_in_range(
        "XNAS", date(2020, 1, 1), target_us + timedelta(days=1)
    )[-300:]
    uk_sessions = calendar.sessions_in_range(
        "XLON", date(2020, 1, 1), target_uk + timedelta(days=1)
    )[-300:]
    frames: dict[str, tuple[pd.DataFrame, str, str]] = {
        "AAPL": (
            _flat_frame(us_sessions, "America/New_York"),
            "USD",
            "America/New_York",
        ),
        "ULVR.L": (_flat_frame(uk_sessions, "Europe/London"), "GBp", "Europe/London"),
    }

    def ticker_factory(symbol: str) -> _FakeTicker:
        frame, currency, tz = frames[symbol]
        return _FakeTicker(frame, symbol, currency, tz)

    return YFinanceHistoricalEvidenceAdapter(ticker_factory, clock=lambda: NOW)


def _commit_fx_evidence(prices: HistoricalPriceRepository) -> str:
    """Commit a deterministic GBPUSD=X close for every calendar day around
    the run month, so the real currency-conversion path (``currency.py``'s
    ``convert_to_base``) never sees a stale FX quote for any session."""
    sessions = tuple(date(2026, 5, 20) + timedelta(days=i) for i in range(60))
    frame = _flat_frame(sessions, "UTC")
    frame[["Open", "High", "Low", "Close", "Adj Close"]] = 1.27
    frame["Volume"] = 0.0

    class _FxTicker:
        def history(self, **_kwargs: object) -> pd.DataFrame:
            return frame.copy()

        def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
            return {
                "symbol": FX_PAIR,
                "currency": "USD",
                "exchangeTimezoneName": "UTC",
            }

    request = HistoricalEvidenceRequest(
        security_id="fx-gbpusd",
        alias_revision=None,
        symbol=FX_PAIR,
        start=sessions[0],
        end=sessions[-1] + timedelta(days=1),
        expected_currency="USD",
        expected_quote_unit="USD",
        expected_timezone="UTC",
        expected_sessions=sessions,
        allowed_observed_symbols=(FX_PAIR,),
    )
    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _symbol: _FxTicker(), clock=lambda: NOW
    ).fetch(request)
    return prices.commit(payload)


def test_clean_checkout_journey_completes_a_real_backtest_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chain Bootstrap -> historical initialization -> universe selection ->
    evidence preparation -> Backtest through the real app engines/services,
    starting from a completely empty Strategy Manager store, and assert the
    completed Result is readable and non-empty (Story 4.9 AC 1-3)."""
    price_cache = tmp_path / "historical_price_cache.db"
    trades_db = tmp_path / "trades.db"
    monkeypatch.setattr(config, "HISTORICAL_PRICE_CACHE", price_cache)
    monkeypatch.setattr(config, "TRADES_DB", trades_db)

    repo = _empty_repo(tmp_path / "backtest.db")
    jobs = StrategyJobService(repo)

    # --- Bootstrap: StrategyProviderBundleV1.fixture() through the real
    # BootstrapStageEngine, exactly the pattern in
    # tests/backtest/test_strategy_bootstrap_service.py:413.
    bundle = StrategyProviderBundleV1.fixture(repo)
    assert bundle.mode == "fixture"
    bootstrap_job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    bootstrap_engine = build_stage_walk_engine(
        bootstrap_job.id, repo, StrategyJobType.BOOTSTRAP, bootstrap_providers=bundle
    )
    bootstrap_result = bootstrap_engine.run(bootstrap_job.id, claim.claim_token)
    assert bootstrap_result.status is StrategyJobStatus.COMPLETE

    active = repo.active_snapshot_profile()
    assert active is not None
    profile = repo.snapshot_profile(active.profile_hash)
    assert profile is not None

    # --- Historical initialization: real HistoricalInitializationEngine,
    # deterministic offline evidence swapped in for the yfinance adapter.
    init_result = jobs.enqueue_initialization(
        InitializationSubmissionV1(
            profile_hash=active.profile_hash,
            requested_start=INITIALIZATION_MONTH,
            requested_end=INITIALIZATION_MONTH,
            calendar_dataset_version=profile.calendar_dataset_version,
        )
    )
    assert init_result.no_op is False
    assert init_result.job is not None
    init_claim = repo.claim_next_strategy_job()
    assert init_claim is not None
    init_engine = build_initialization_engine(
        init_claim.job.id, init_claim.claim_token, repo
    )
    init_engine._month_processor._evidence_adapter = (  # type: ignore[attr-defined]
        _initialization_evidence_adapter()
    )
    init_run_result = init_engine.run(init_claim.job.id, init_claim.claim_token)
    assert init_run_result.status is StrategyJobStatus.COMPLETE

    # --- Universe selection: the same validated canonicalization/selection
    # path the /strategy-manager/configuration route uses (Story 4.5) --
    # a non-empty, multi-security universe (both fixture roster members).
    roster_identities = repo.roster_member_identities(active.profile_hash)
    security_ids = [
        security_id for security_id, _symbol, _mic, _currency in roster_identities
    ]
    assert len(security_ids) >= 2
    canonical = canonical_run_universe(security_ids)

    strategies = discover_strategies(config.SKILLS_DIR)
    strategy = next(
        item for item in strategies.strategies if item.strategy_id == STRATEGY_ID
    )
    universe_binding = strategy.bind_universe(canonical)
    selection = RunUniverseSelectionV1(
        profile_hash=active.profile_hash,
        activation_seq=active.activation_seq,
        universe_schema=strategy.universe.schema_version,
        universe_mode=strategy.universe.mode,
        universe_parameter=strategy.universe.parameter,
        canonical_security_ids=canonical,
        run_universe_digest=run_universe_digest(
            canonical,
            universe_schema=strategy.universe.schema_version,
            mode=strategy.universe.mode,
            parameter=strategy.universe.parameter,
            profile_hash=active.profile_hash,
        ),
    )

    # --- FX evidence: the roster spans USD (AAPL) and GBP (ULVR), so the
    # base-currency mismatch requires pinned historical FX evidence (the
    # onboarding doc's documented "FX requirement"). Commit it through both
    # stores the real resolution path reads: HistoricalPriceRepository (the
    # runtime evidence the worker actually replays) and FxQuoteRepository
    # (what preparation's fx_pinning stage looks up), sharing one digest.
    prices = HistoricalPriceRepository(db.make_connect(lambda: str(price_cache)))
    prices.ensure_schema()
    fx_revision = _commit_fx_evidence(prices)
    with db.session(db.make_connect(lambda: str(trades_db))) as conn:
        db.init_trades_db(conn)
    fx_quote_repo = FxQuoteRepository(db.make_connect(lambda: str(trades_db)))
    fx_quote_repo.insert_or_get(
        FxQuote(
            pair=FX_PAIR,
            as_of=f"{INITIALIZATION_MONTH}-01",
            rate=Decimal("1.27"),
            digest=fx_revision,
        )
    )

    # --- Evidence preparation: build_stage_walk_engine sealing a V2
    # manifest (Story 4.6/4.6.3).
    parameters: dict[str, object] = {
        **universe_binding,
        "entry_on_or_after": "2026-06-01",
        "top_x": 1,
    }
    preparation_result = jobs.enqueue_preparation(
        PreparationSubmissionV1(
            selection=selection,
            strategy_id=strategy.strategy_id,
            strategy_api_version=strategy.api_version,
            strategy_source_digest=strategy.source_digest,
            parameters=parameters,
            start_month=INITIALIZATION_MONTH,
            end_month=INITIALIZATION_MONTH,
            base_currency="USD",
            starting_capital=Decimal("10000"),
            idempotency_key="clean-checkout-journey-preparation",
        )
    )
    preparation_claim = repo.claim_next_strategy_job()
    assert preparation_claim is not None
    preparation_engine = build_stage_walk_engine(
        preparation_claim.job.id, repo, StrategyJobType.PREPARATION
    )
    preparation_run_result = preparation_engine.run(
        preparation_claim.job.id, preparation_claim.claim_token
    )
    assert preparation_run_result.status is StrategyJobStatus.COMPLETE

    backtest_job_id = repo.preparation_child_backtest_id(preparation_result.job.id)
    assert backtest_job_id is not None
    backtest_run = repo.strategy_run(backtest_job_id)
    assert backtest_run.manifest_version == "run_input_manifest.v2"
    assert backtest_run.universe_selection == selection
    assert backtest_run.parameters["top_x"] == 1

    # --- Backtest: build_backtest_engine, real Strategy discovery against
    # the actual skills/ directory -- never the _patch_strategy_resolution
    # monkeypatch shortcut test_backtest_worker.py uses for narrower tests.
    backtest_claim = repo.claim_next_strategy_job()
    assert backtest_claim is not None
    assert backtest_claim.job.id == backtest_job_id
    backtest_engine = build_backtest_engine(
        backtest_claim.job.id, backtest_claim.claim_token, repo
    )
    backtest_result = backtest_engine.run(
        backtest_claim.job.id, backtest_claim.claim_token
    )
    assert backtest_result.status is StrategyJobStatus.COMPLETE

    # --- Result: readable, COMPLETE, and non-empty (Story 4.9 AC 3), via
    # the same repository read the /strategy-manager/results/{run_id} route
    # uses.
    completed_job = repo.strategy_job(backtest_job_id)
    assert completed_job.status is StrategyJobStatus.COMPLETE
    result = repo.backtest_result(backtest_job_id)
    assert len(result.events) > 0
    assert len(result.equity_curve) > 0
    assert result.parameters["top_x"] == 1
    assert result.initial_entry_selection is not None
    assert len(result.initial_entry_selection.signals) == 1
    assert len(result.initial_entry_selection.decisions) == len(canonical)
    assert not any(event.kind == "exit_fill" for event in result.events)
    assert any(event.kind == "open_position_mark" for event in result.events)

    # --- Restart: a separately prepared but otherwise identical Backtest
    # is cancelled before execution, then restarted through the durable
    # repository seam.  The restart must reuse (not rebuild) its canonical
    # manifest/parameters and reproduce the same persisted decision evidence
    # and final marks as the completed first run above.
    replay_preparation = jobs.enqueue_preparation(
        PreparationSubmissionV1(
            selection=selection,
            strategy_id=strategy.strategy_id,
            strategy_api_version=strategy.api_version,
            strategy_source_digest=strategy.source_digest,
            parameters=parameters,
            start_month=INITIALIZATION_MONTH,
            end_month=INITIALIZATION_MONTH,
            base_currency="USD",
            starting_capital=Decimal("10000"),
            idempotency_key="clean-checkout-journey-replay-preparation",
        )
    )
    replay_preparation_claim = repo.claim_next_strategy_job()
    assert replay_preparation_claim is not None
    replay_preparation_engine = build_stage_walk_engine(
        replay_preparation_claim.job.id, repo, StrategyJobType.PREPARATION
    )
    assert (
        replay_preparation_engine.run(
            replay_preparation_claim.job.id,
            replay_preparation_claim.claim_token,
        ).status
        is StrategyJobStatus.COMPLETE
    )
    cancelled_backtest_id = repo.preparation_child_backtest_id(
        replay_preparation.job.id
    )
    assert cancelled_backtest_id is not None
    cancelled = repo.request_strategy_job_cancellation(
        cancelled_backtest_id,
        expected_version=repo.strategy_job(cancelled_backtest_id).status_version,
    )
    restarted = repo.restart_backtest_job(
        cancelled_backtest_id,
        expected_version=cancelled.status_version,
        idempotency_key="clean-checkout-journey-restart",
    )
    cancelled_run = repo.strategy_run(cancelled_backtest_id)
    assert restarted.backtest.parameters["top_x"] == 1
    assert restarted.backtest.run_input_manifest_digest == (
        cancelled_run.run_input_manifest_digest
    )
    assert repo.run_input_manifest_json(
        restarted.backtest.run_input_manifest_digest
    ) == repo.run_input_manifest_json(cancelled_run.run_input_manifest_digest)

    restarted_claim = repo.claim_next_strategy_job()
    assert restarted_claim is not None and restarted_claim.job.id == restarted.job.id
    restarted_engine = build_backtest_engine(
        restarted.job.id, restarted_claim.claim_token, repo
    )
    assert (
        restarted_engine.run(restarted.job.id, restarted_claim.claim_token).status
        is StrategyJobStatus.COMPLETE
    )
    restarted_result = repo.backtest_result(restarted.job.id)
    assert restarted_result.parameters["top_x"] == 1
    assert restarted_result.initial_entry_selection == result.initial_entry_selection
    assert restarted_result.final_cash_base == result.final_cash_base
    assert restarted_result.events == result.events


def test_a_deliberately_failed_stage_surfaces_a_named_stable_failure(
    tmp_path: Path,
) -> None:
    """A stage failure inside the same composed journey must produce a
    named, stable failure code/reason -- never a silent pass (Story 4.9 AC,
    I/O matrix: 'Any required stage fails')."""

    class _UnavailableQualificationAdapter:
        def fetch(self, definition):
            raise ConnectionError(
                f"fixture provider unavailable for {definition.symbol}"
            )

    repo = _empty_repo(tmp_path / "backtest.db")
    bundle = StrategyProviderBundleV1.fixture(repo)
    broken_bundle = replace(
        bundle,
        qualification_runner=QualificationRunner(
            repo,
            _qualification_fixture_path(),
            _production_probes(),
            live_adapter=_UnavailableQualificationAdapter(),
            clock=lambda: NOW,
        ),
    )
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    engine = build_stage_walk_engine(
        job.id, repo, StrategyJobType.BOOTSTRAP, bootstrap_providers=broken_bundle
    )
    result = engine.run(job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.FAILED
    assert result.job_type is StrategyJobType.BOOTSTRAP
    assert result.failure_code is JobFailureCode.PROVIDER_UNAVAILABLE
    assert result.failure_detail == "Historical source unavailable"
    # No profile is ever activated off the back of a failed Bootstrap.
    assert repo.active_snapshot_profile() is None
