"""Golden replay digest for slot-sized strategies (#118).

The digest was recorded on unchanged ``main`` before target-weight sizing
landed. A slot-sized strategy (no ``Signal.target_weight``) must keep
replaying byte-identically: events, equity curve, candidate audits and
final cash.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

from app.services.backtest.backtest_engine import (
    InMemorySessionBatchSink,
    run_simulation,
)
from app.services.backtest.strategy_protocol import Signal, SignalSide
from tests.backtest.test_backtest_engine import (
    _ScriptedStrategy,
    _build_security,
    _manifest,
    _market_view_factory,
    _month_str,
    _sessions,
)

#: sha256 of the fixture's full output, recorded on main at 85f6403.
GOLDEN_REPLAY_DIGEST = (
    "4aae300115f95613b45acd59e9b731e783d8460733f8466784883d851762cb11"
)


def _signal(security_id: str, side: SignalSide, session: date, **extra) -> Signal:
    return Signal(
        security_id=security_id,
        side=side,
        session=session,
        rule_id=f"{side.value.lower()}-{security_id}",
        **extra,
    )


def _golden_output() -> tuple[str, int]:
    """Capped, ranked run with a swap, a split, a dividend and price moves."""
    start = date(2024, 3, 1)
    sessions = _sessions("XNYS", start, date(2024, 4, 1))
    d0, d3, d6, d9 = sessions[0], sessions[3], sessions[6], sessions[9]
    moves = {
        sessions[4]: (104.5, 106.25),
        sessions[7]: (97.0, 95.5),
        sessions[12]: (111.0, 112.75),
    }
    built = [
        _build_security(
            "sec-a",
            "XNYS",
            sessions,
            revision=hashlib.sha256(b"sec-a").hexdigest(),
            price_overrides=moves,
            split_by_session={sessions[5]: 2.0},
        ),
        _build_security(
            "sec-b",
            "XNYS",
            sessions,
            revision=hashlib.sha256(b"sec-b").hexdigest(),
            open_price=37.0,
            close_price=37.5,
            dividend_by_session={sessions[2]: 0.42},
        ),
        _build_security(
            "sec-c",
            "XNYS",
            sessions,
            revision=hashlib.sha256(b"sec-c").hexdigest(),
            open_price=251.0,
            close_price=249.0,
        ),
    ]
    entries = {
        d0: [
            _signal("sec-a", SignalSide.BUY, d0, priority=Decimal("2")),
            _signal("sec-b", SignalSide.BUY, d0, priority=Decimal("5")),
            _signal("sec-c", SignalSide.BUY, d0),
        ],
        d6: [_signal("sec-c", SignalSide.BUY, d6, priority=Decimal("1"))],
        d9: [_signal("sec-a", SignalSide.BUY, d9)],
    }
    exits = {
        d3: [_signal("sec-b", SignalSide.SELL, d3)],
        d6: [_signal("sec-a", SignalSide.SELL, d6)],
    }
    manifest = _manifest(
        securities=tuple(pinned for _, pinned in built),
        start_month=_month_str(start),
        end_month=_month_str(start),
        starting_capital=Decimal("25000"),
        parameters={"max_concurrent_positions": 2},
    ).model_copy(
        # The manifest is input, not engine behaviour: pin the identities
        # the golden was recorded under so its manifest_digest stays fixed
        # across ENGINE_VERSION/PROTOCOL_SCHEMA_VERSION bumps.
        update={
            "engine_version": "backtest_engine.v10",
            "protocol_schema_version": "strategy_protocol.v6",
        }
    )
    sink = InMemorySessionBatchSink()
    output = run_simulation(
        manifest=manifest,
        strategy=_ScriptedStrategy(entries=entries, exits=exits, default_size=-1),
        market_view_factory=_market_view_factory(),
        security_market_data=tuple(market for market, _ in built),
        sink=sink,
    )
    payload = {
        "output": output.model_dump(mode="json"),
        "audits": [audit.model_dump(mode="json") for audit in sink.candidate_audits],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest(), len(output.events)


def test_slot_sized_replay_matches_pre_target_weight_golden_digest() -> None:
    digest, event_count = _golden_output()
    assert event_count == 11
    assert digest == GOLDEN_REPLAY_DIGEST
