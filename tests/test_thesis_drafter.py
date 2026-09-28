"""Unit tests for the AI position-thesis drafter (GH-14).

The Anthropic SDK is never called: a fake client with ``.messages.create``
returns SimpleNamespace responses. Covers the draft/unavailable rows of the
I/O matrix, prompt privacy and the skill drift guard.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.research.evidence import CURRENCY_AMOUNT, LABEL
from app.agents.thesis import drafter
from app.agents.thesis.drafter import ThesisDraftClient, draft_thesis
from app.schemas.position_thesis import CloseBelowSmaRule, Stage2LostRule
from app.services.freshness_service import calculate_freshness
from tests.test_research_copilot import ENTRY, NOW, PRICE, STOP, _meta, _record

GOOD = {
    "rationale": f"{LABEL} is a Stage 2 leader.",
    "expected_setup": f"{LABEL} should hold its 50-day SMA.",
    "rules": [
        {"kind": "close_below_sma", "period": 50, "min_rel_volume": None},
        {"kind": "stage_2_lost", "period": None},
        {"kind": "close_below_sma", "period": 20},
    ],
}


def _client(
    payload: Any, stop_reason: str = "end_turn", key: str = "test-key"
) -> tuple[ThesisDraftClient, list[dict[str, Any]]]:
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
    return ThesisDraftClient(api_key=key, client=fake), calls


def _draft(client: ThesisDraftClient, record: Any = None) -> Any:
    return draft_thesis(
        _record() if record is None else record,
        client=client,
        meta=_meta(),
        freshness=calculate_freshness(NOW, now=NOW),
        source_health={},
    )


def test_valid_rules_are_kept_and_bad_ones_dropped() -> None:
    client, calls = _client(GOOD)

    draft = _draft(client)

    assert draft is not None
    assert draft.rules == (CloseBelowSmaRule(period=50), Stage2LostRule())
    # The label is mapped back to the ticker for local display only.
    assert draft.rationale == "ZETA.L is a Stage 2 leader."
    call = calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["thinking"] == {"type": "disabled"}
    schema = call["output_config"]["format"]["schema"]
    kinds = schema["properties"]["rules"]["items"]["properties"]["kind"]["enum"]
    assert kinds == [
        "close_below_stop",
        "close_below_sma",
        "stage_2_lost",
        "score_below",
    ]


def test_prompt_contains_no_ticker_currency_amount_or_price() -> None:
    client, calls = _client(GOOD)
    _draft(client)

    prompt = calls[0]["messages"][0]["content"]

    assert "ZETA" not in prompt
    assert CURRENCY_AMOUNT.search(prompt) is None
    for level in (PRICE, STOP, ENTRY, 176.55, 189.91, 165.11, 199.99):
        assert str(level) not in prompt
    assert "123456" not in prompt
    assert "123.45" not in prompt
    assert f"{LABEL} is currently held in a portfolio." in prompt
    assert "ZETA" not in calls[0]["system"]


@pytest.mark.parametrize(
    ("payload", "stop_reason", "key"),
    [
        (GOOD, "end_turn", ""),
        (GOOD, "refusal", "test-key"),
        (GOOD, "max_tokens", "test-key"),
        ("not json", "end_turn", "test-key"),
        ({"rationale": "x"}, "end_turn", "test-key"),
        (
            {**GOOD, "rules": [{"kind": "moon_phase"}, {"kind": "score_below"}]},
            "end_turn",
            "test-key",
        ),
        ({**GOOD, "rationale": ""}, "end_turn", "test-key"),
        (RuntimeError("network"), "end_turn", "test-key"),
    ],
    ids=[
        "no-key",
        "refusal",
        "truncated",
        "bad-json",
        "wrong-shape",
        "zero-valid-rules",
        "empty-wording",
        "sdk-error",
    ],
)
def test_any_failure_is_unavailable(
    payload: Any, stop_reason: str, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client, _ = _client(payload, stop_reason, key)

    assert _draft(client) is None


def test_missing_record_makes_no_call() -> None:
    client, calls = _client(GOOD)

    assert (
        draft_thesis(
            None,
            client=client,
            meta=_meta(),
            freshness=calculate_freshness(NOW, now=NOW),
            source_health={},
        )
        is None
    )
    assert calls == []


def test_client_is_disabled_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert ThesisDraftClient().enabled is False


def _skill_prompt_text() -> str:
    """Extract the fenced prompt body from the position-thesis skill reference."""
    from app.core.config import SKILLS_DIR

    ref = SKILLS_DIR / "rtly-position-thesis" / "references" / "system_prompt.md"
    body = ref.read_text(encoding="utf-8")
    marker = "```text\n"
    start = body.index(marker) + len(marker)
    end = body.index("\n```", start)
    return body[start:end]


class TestSystemPromptDriftGuard:
    """The skill reference must mirror the live `_SYSTEM_PROMPT` verbatim."""

    def test_skill_reference_matches_live_prompt(self) -> None:
        assert _skill_prompt_text() == drafter._SYSTEM_PROMPT

    def test_skill_frontmatter_names_the_skill(self) -> None:
        from app.core.config import SKILLS_DIR

        skill = (SKILLS_DIR / "rtly-position-thesis" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert skill.startswith("---\nname: rtly-position-thesis\ndescription: ")


def test_stray_parameters_are_dropped_not_the_rule() -> None:
    payload = {**GOOD, "rules": [{"kind": "stage_2_lost", "period": 50}]}
    client, _ = _client(payload)

    draft = _draft(client)

    assert draft is not None and draft.rules == (Stage2LostRule(),)


def test_only_the_first_six_valid_rules_are_kept() -> None:
    rules = [{"kind": "moon"}] + [
        {"kind": "score_below", "min_score": n} for n in range(1, 9)
    ]
    client, _ = _client({**GOOD, "rules": rules})

    draft = _draft(client)

    assert draft is not None
    assert [rule.min_score for rule in draft.rules] == [1, 2, 3, 4, 5, 6]


def test_label_is_revealed_as_the_imported_display_symbol() -> None:
    client, _ = _client(GOOD)

    draft = draft_thesis(
        _record(),
        client=client,
        meta=_meta(),
        freshness=calculate_freshness(NOW, now=NOW),
        source_health={},
        display_symbol="ZETA",
    )

    assert draft is not None and draft.rationale == "ZETA is a Stage 2 leader."
