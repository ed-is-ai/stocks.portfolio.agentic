"""Tests for the weekly process summary and its interpretation (GH-17).

A tmp ``trades.db`` (never the real one) holds the ledger; evidence comes
from the checklist tests' ``FakeReader``. The Anthropic SDK is never called:
a fake client with ``.messages.create`` returns SimpleNamespace responses.
Covers weekly facts, the anonymised prompt's privacy, the interpretation
ok/fails rows, the service cache and the skill drift guard.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.agents.trade_review import weekly
from app.agents.trade_review.weekly import (
    TradeReviewClient,
    build_prompt,
    trade_label,
    weekly_facts,
)
from app.agents.trader.trader_agent import TraderAgent
from app.repositories import db
from app.repositories.portfolio_strategies_repo import PortfolioStrategiesRepository
from app.repositories.trade_annotations_repo import TradeAnnotationsRepository
from app.services.portfolio_service import PortfolioService
from app.services.realised_pnl_service import RealisedPnlService
from app.services import trade_review_service
from app.services.trade_review_service import TradeReviewService
from app.services.trader_service import TraderService
from tests.test_realised_pnl_service import _StubPortfolioService
from tests.test_trade_review_checklist import (
    DAYS,
    FakeReader,
    make_bars,
    rising,
    scan,
)

GOOD = {
    "summary": "Trade A followed most rules; Trade B was extended.",
    "patterns": ["entry.pivot_band deviated twice."],
}
TICKER = "ZETA"
PRICE = 123.45
SHARES = 4321
STOP = 117.28
INTENT = "secret intent: buying the breakout"


def build_stack(tmp_path: Path, reader: FakeReader | None = None) -> SimpleNamespace:
    """A review service over a tmp ledger: two BUYs in one week, one annotated."""
    # db_path at construction: TraderAgent initialises its database in
    # post-init, so a default-path instance would touch the real trades.db.
    agent = TraderAgent(name="TraderAgent", db_path=tmp_path / "trades.db")
    realised = RealisedPnlService(
        TraderService(agent), cast(PortfolioService, _StubPortfolioService())
    )
    pf = agent.create_portfolio("SIPP")
    first = agent.record_buy(
        TICKER, SHARES, PRICE, DAYS[260].isoformat(), portfolio_id=pf.id
    )
    second = agent.record_buy(
        TICKER, SHARES, 130.0, DAYS[261].isoformat(), portfolio_id=pf.id
    )
    connect = db.make_connect(lambda: agent.db_path)
    annotations = TradeAnnotationsRepository(connect)
    readers: list[FakeReader] = []

    def factory() -> FakeReader:
        fresh = reader or FakeReader(
            bars_=make_bars(rising(320)),
            scans=[replace(scan(DAYS[250], "110"), currency="GBP")],
            currency="GBP",
        )
        readers.append(fresh)
        return fresh

    # A fake store revision and clock: the real store files are never touched.
    clock = SimpleNamespace(store="rev-1", today=date(2025, 1, 6))
    service = TradeReviewService(
        TraderService(agent),
        realised,
        annotations,
        PortfolioStrategiesRepository(connect),
        reader_factory=factory,
        store_revision=lambda: clock.store,
        today=lambda: clock.today,
    )
    return SimpleNamespace(
        clock=clock,
        service=service,
        agent=agent,
        pid=pf.id,
        first=first,
        second=second,
        annotations=annotations,
        readers=readers,
    )


def _client(
    payload: Any, stop_reason: str = "end_turn", key: str = "test-key"
) -> tuple[TradeReviewClient, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if isinstance(payload, Exception):
            raise payload
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return SimpleNamespace(
            stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)]
        )

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    return TradeReviewClient(api_key=key, client=fake), calls


def test_weekly_facts_count_statuses_and_recurring_deviations(tmp_path: Path) -> None:
    stack = build_stack(tmp_path)

    view = stack.service.weekly()

    assert view.week == "2025-W01"
    assert view.previous is None and view.next is None
    assert view.facts is not None
    assert view.facts.counts["entry_location"]["deviated"] == 2
    assert view.facts.counts["valid_setup"]["followed"] == 2
    assert [r.rule_id for r in view.facts.recurring] == ["entry.pivot_band"]
    assert [view.labels[r.trade_id] for r in view.reviews] == ["Trade A", "Trade B"]


def test_trade_labels_continue_past_z() -> None:
    assert [trade_label(i) for i in (0, 25, 26, 27)] == [
        "Trade A",
        "Trade Z",
        "Trade AA",
        "Trade AB",
    ]


def test_interpretation_ok_and_request_is_anonymised(tmp_path: Path) -> None:
    stack = build_stack(tmp_path)
    stack.annotations.add(stack.pid, stack.first.id, INTENT, STOP)
    client, calls = _client(GOOD)

    result = stack.service.interpret(None, client)

    assert result is not None and result.summary == GOOD["summary"]
    call = calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["thinking"] == {"type": "disabled"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    request = json.dumps(call, default=str)
    for secret in (TICKER, str(PRICE), str(SHARES), str(STOP), "130.0", INTENT):
        assert secret not in request
    prompt = call["messages"][0]["content"]
    assert "Trade A (BUY):" in prompt
    assert "risk_pct=5.0" in prompt
    assert "entry_vs_pivot_pct=12.23" in prompt


@pytest.mark.parametrize(
    ("payload", "stop_reason", "key"),
    [
        (GOOD, "end_turn", ""),
        (GOOD, "refusal", "test-key"),
        (GOOD, "max_tokens", "test-key"),
        ("not json", "end_turn", "test-key"),
        ({"summary": ""}, "end_turn", "test-key"),
        ({**GOOD, "summary": "Worth £500 now."}, "end_turn", "test-key"),
        (RuntimeError("network"), "end_turn", "test-key"),
    ],
    ids=[
        "no-key",
        "refusal",
        "truncated",
        "bad-json",
        "wrong-shape",
        "states-amount",
        "sdk-error",
    ],
)
def test_any_interpretation_failure_is_none(
    payload: Any, stop_reason: str, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client, _ = _client(payload, stop_reason, key)

    assert client.interpret("Week 2025-W01: 1 trade(s) reviewed.") is None


def test_prompt_carries_only_whitelisted_observations(tmp_path: Path) -> None:
    stack = build_stack(tmp_path)
    view = stack.service.weekly()
    assert view.facts is not None

    prompt = build_prompt(view.facts, view.reviews)

    assert "pivot=" not in prompt and "price=" not in prompt
    assert "stop=" not in prompt
    assert weekly_facts("2025-W01", view.reviews) == view.facts


def test_reviews_are_cached_until_an_annotation_changes_them(tmp_path: Path) -> None:
    stack = build_stack(tmp_path)

    first = stack.service.reviews()
    again = stack.service.reviews()
    stack.annotations.add(stack.pid, stack.first.id, "intent", 95.0)
    after = stack.service.reviews()

    assert first == again and len(stack.readers) == 2
    check = after[stack.first.id].check("evidenced_stop")
    assert check is not None
    assert check.evidence[0].source.startswith("your annotation")


def test_a_failed_store_read_is_cached_until_the_store_changes(
    tmp_path: Path,
) -> None:
    stack = build_stack(tmp_path, FakeReader(failed=True))

    stack.service.reviews()
    stack.service.reviews()
    assert len(stack.readers) == 1
    stack.clock.store = "rev-2"
    stack.service.reviews()

    assert len(stack.readers) == 2


def _skill_prompt_text() -> str:
    """Extract the fenced prompt body from the trade-review skill reference."""
    from app.core.config import SKILLS_DIR

    ref = SKILLS_DIR / "rtly-trade-review" / "references" / "system_prompt.md"
    body = ref.read_text(encoding="utf-8")
    marker = "```text\n"
    start = body.index(marker) + len(marker)
    end = body.index("\n```", start)
    return body[start:end]


class TestSystemPromptDriftGuard:
    """The skill reference must mirror the live `_SYSTEM_PROMPT` verbatim."""

    def test_skill_reference_matches_live_prompt(self) -> None:
        assert _skill_prompt_text() == weekly._SYSTEM_PROMPT

    def test_skill_frontmatter_names_the_skill(self) -> None:
        from app.core.config import SKILLS_DIR

        skill = (SKILLS_DIR / "rtly-trade-review" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert skill.startswith("---\nname: rtly-trade-review\ndescription: ")


def test_reviews_refresh_when_the_store_or_the_day_changes(tmp_path: Path) -> None:
    stack = build_stack(tmp_path)

    stack.service.reviews()
    stack.service.reviews()
    assert len(stack.readers) == 1
    stack.clock.store = "rev-2"
    stack.service.reviews()
    assert len(stack.readers) == 2
    stack.clock.today = date(2025, 1, 7)
    stack.service.reviews()

    assert len(stack.readers) == 3


def test_concurrent_cold_loads_compute_once(tmp_path: Path) -> None:
    stack = build_stack(tmp_path)
    factory = stack.service._reader_factory

    def slow() -> FakeReader:
        time.sleep(0.05)
        return factory()

    stack.service._reader_factory = slow
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: stack.service.reviews(), range(4)))

    assert len(stack.readers) == 1
    assert all(result == results[0] for result in results)


def test_one_failing_trade_is_unknown_and_the_others_are_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stack = build_stack(tmp_path)
    real = trade_review_service.review_buy

    def flaky(trade: Any, *args: Any, **kwargs: Any) -> Any:
        if trade.id == stack.first.id:
            raise ZeroDivisionError("boom")
        return real(trade, *args, **kwargs)

    monkeypatch.setattr(trade_review_service, "review_buy", flaky)

    reviews = stack.service.reviews()

    failed = reviews[stack.first.id]
    assert {c.status for c in failed.checks} == {"unknown"}
    assert failed.checks[0].note == "review failed for this trade"
    other = reviews[stack.second.id].check("valid_setup")
    assert other is not None and other.status == "followed"
    assert "Trade review failed" in caplog.text


def test_annotations_survive_a_trade_correction(tmp_path: Path) -> None:
    stack = build_stack(tmp_path)
    stack.service.annotate(stack.first.id, INTENT, STOP)

    corrected = stack.agent.correct_trade(
        TICKER, SHARES, PRICE, date=DAYS[260].isoformat(), portfolio_id=stack.pid
    )

    assert corrected.id != stack.first.id
    notes = stack.service.annotations_for(corrected.id)
    assert [(n.trade_id, n.intent) for n in notes] == [(corrected.id, INTENT)]
    check = stack.service.reviews()[corrected.id].check("evidenced_stop")
    assert check is not None and check.observed["stop"] == STOP
    view = stack.service.weekly()
    assert [a.intent for a in view.annotations] == [INTENT]
    assert view.labels[view.annotations[0].trade_id] == "Trade A"


def test_migration_drops_the_cascade_and_keeps_every_note(tmp_path: Path) -> None:
    path = tmp_path / "trades.db"
    with closing(db.connect(path)) as conn:
        db.init_trades_db(conn)
        conn.execute("DROP TABLE trade_annotations")
        conn.execute(
            "CREATE TABLE trade_annotations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "portfolio_id INTEGER NOT NULL, trade_id INTEGER NOT NULL "
            "REFERENCES trades(id) ON DELETE CASCADE, intent TEXT NOT NULL, "
            "stated_stop REAL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO trades (id, ticker, action, shares, price, date, "
            "portfolio_id) VALUES (5, 'ZETA', 'BUY', 2, 10.5, '2025-01-02', 1)"
        )
        conn.execute(
            "INSERT INTO trade_annotations VALUES (1, 1, 5, 'why', 9.5, 'now')"
        )
        conn.commit()

        db.init_trades_db(conn)
        db.init_trades_db(conn)
        conn.execute("DELETE FROM trades WHERE id = 5")
        conn.commit()

        keys = conn.execute("PRAGMA foreign_key_list(trade_annotations)").fetchall()
        rows = conn.execute(
            "SELECT id, trade_id, intent, stated_stop, trade_fingerprint "
            "FROM trade_annotations"
        ).fetchall()
        index = conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE name = "
            "'idx_trade_annotations_trade'"
        ).fetchone()

    assert keys == []
    assert rows == [(1, 5, "why", 9.5, "1|ZETA|BUY|2025-01-02|2.0|10.5")]
    assert index == ("trade_annotations",)
