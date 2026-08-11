from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.services.backtest.historical_data_qualification import (
    HistoricalQualificationPayload,
    MANDATORY_FIXTURE_IDS,
    MANDATORY_PROBE_IDS,
    ProbeDefinition,
    QualificationAvailabilityService,
    QualificationRunner,
)

FIXTURE = Path(__file__).parent / "fixtures" / "market_mechanics_v1.json"


def _probes() -> dict[str, ProbeDefinition]:
    return {
        name: ProbeDefinition(
            symbol={"us_active": "AAPL", "lse_active": "SHEL.L", "gbpusd": "GBPUSD=X"}[
                name
            ],
            start=date(2024, 1, 1),
            end=date(2024, 1, 4),
            expected_currency="USD",
            expected_quote_unit="USD",
            expected_timezone="UTC",
            expected_sessions=(date(2024, 1, 2), date(2024, 1, 3)),
            allowed_observed_symbols=(
                {"us_active": "AAPL", "lse_active": "SHEL.L", "gbpusd": "GBPUSD=X"}[
                    name
                ],
            ),
        )
        for name in MANDATORY_PROBE_IDS
    }


class FakeLiveAdapter:
    def fetch(self, definition: ProbeDefinition) -> HistoricalQualificationPayload:
        return HistoricalQualificationPayload(
            requested_symbol=definition.symbol,
            observed_symbol=definition.symbol,
            currency="USD",
            quote_unit="USD",
            quote_unit_scale="1",
            exchange_timezone="UTC",
            request_contract={},
            rows=(),
            response_metadata_digest="m" * 64,
            content_digest=(definition.symbol.encode().hex() + "0" * 64)[:64],
            acquired_at="2026-08-10T12:00:00+00:00",
        )


def test_provider_fixture_catalog_is_complete_and_has_pinned_digests() -> None:
    payload = json.loads(FIXTURE.read_text())
    cases = {case["id"]: case for case in payload["provider_cases"]}
    assert set(cases) == set(MANDATORY_FIXTURE_IDS)
    assert all(len(case["expected_content_digest"]) == 64 for case in cases.values())
    assert cases["renamed_alias"]["metadata"]["symbol"] == "META"
    assert cases["lse_gbpence"]["metadata"]["currency"] == "GBp"
    assert cases["ordinary_split"]["rows"][1]["Stock Splits"] == 4
    assert cases["ordinary_split"]["rows"][0]["Close"] == 25
    assert cases["reverse_split"]["rows"][1]["Stock Splits"] == 0.1
    assert cases["reverse_split"]["rows"][0]["Close"] == 100
    assert cases["dividend"]["rows"][0]["Dividends"] == 0.25
    assert cases["gbpusd_orientation"]["requested_symbol"] == "GBPUSD=X"


def test_runner_executes_all_fixtures_and_probes_then_enables_exact_contract(
    tmp_path,
) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    runner = QualificationRunner(
        repo,
        FIXTURE,
        _probes(),
        live_adapter=FakeLiveAdapter(),
        clock=lambda: datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
    )
    contract = runner.run()
    history = repo.qualification_history(contract.contract_digest)
    assert len(history) == 1 and history[0].passed
    assert QualificationAvailabilityService(repo).availability(contract).available
    assert runner.contract() == contract


def test_runner_rejects_incomplete_probe_definitions(tmp_path) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    probes = _probes()
    probes.pop("gbpusd")
    try:
        QualificationRunner(repo, FIXTURE, probes)
    except ValueError as exc:
        assert "incomplete" in str(exc)
    else:
        raise AssertionError("incomplete probes must fail")
