"""Unit tests for the weekly StockTwits email source (#137).

No real IMAP, HTTP, or Anthropic call is ever made — imaplib, requests, and the
Anthropic SDK are all mocked. Covers: image-URL extraction from the sanitised
sample email, media-type detection, the vision extraction happy path and its
graceful-skip failures, the enabled gate, and the end-to-end load() orchestration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from app.integrations import stocktwits_email
from app.integrations.stocktwits_email import (
    ImapConfig,
    StockTwitsEmailSource,
    _detect_media_type,
    _parse_internaldate,
    extract_list_image_urls,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_HTML = (FIXTURES / "stocktwits_weekly_sample.html").read_text(encoding="utf-8")


def _config() -> ImapConfig:
    return ImapConfig(
        host="imap.example.com",
        port=993,
        user="me@example.com",
        password="app-pw",
        sender="stocktwits.com",
        subject="Stocktwits Top 25",
    )


# --- image-URL extraction --------------------------------------------------


class TestExtractListImageUrls:
    def test_extracts_all_three_indices_from_sample(self) -> None:
        urls = extract_list_image_urls(SAMPLE_HTML)
        assert set(urls) == {"sp500", "nasdaq100", "russell2000"}
        assert "momentum-sp500-" in urls["sp500"]
        assert "momentum-nasdaq100-" in urls["nasdaq100"]
        assert "momentum-russell2000-" in urls["russell2000"]
        assert all(u.startswith("https://") for u in urls.values())

    def test_decodes_quoted_printable_body(self) -> None:
        # The raw sample is quoted-printable; extraction must decode it.
        assert "=3D" in SAMPLE_HTML  # sanity: fixture really is QP-encoded
        urls = extract_list_image_urls(SAMPLE_HTML)
        assert "=3D" not in urls["sp500"]

    def test_already_decoded_html_is_not_mangled(self) -> None:
        html = '<img src="https://cdn/x/momentum-sp500-2026-07-31.png?t=123">'
        urls = extract_list_image_urls(html)
        assert urls == {"sp500": "https://cdn/x/momentum-sp500-2026-07-31.png?t=123"}

    def test_returns_empty_when_no_list_images(self) -> None:
        assert extract_list_image_urls("<p>no lists here</p>") == {}

    def test_keeps_first_url_per_index(self) -> None:
        html = (
            '<img src="https://cdn/momentum-sp500-2026-07-31.png">'
            '<img src="https://cdn/momentum-sp500-2026-07-24.png">'
        )
        urls = extract_list_image_urls(html)
        assert urls["sp500"].endswith("2026-07-31.png")


# --- media-type detection --------------------------------------------------


class TestDetectMediaType:
    def test_png_signature(self) -> None:
        assert _detect_media_type(b"\x89PNG\r\n\x1a\n rest") == "image/png"

    def test_jpeg_signature(self) -> None:
        assert _detect_media_type(b"\xff\xd8\xff\xe0 rest") == "image/jpeg"

    def test_unknown_defaults_to_jpeg(self) -> None:
        assert _detect_media_type(b"not an image") == "image/jpeg"


# --- INTERNALDATE parsing --------------------------------------------------


class TestParseInternaldate:
    def test_parses_to_utc(self) -> None:
        envelope = b'1 (INTERNALDATE "01-Aug-2026 20:03:54 +0100" RFC822 {123}'
        parsed = _parse_internaldate(envelope)
        # 20:03:54 +0100 == 19:03:54 UTC
        assert parsed == datetime(2026, 8, 1, 19, 3, 54, tzinfo=timezone.utc)

    def test_unparseable_returns_none(self) -> None:
        assert _parse_internaldate(b"no date here") is None


# --- enabled gate ----------------------------------------------------------


class TestEnabled:
    def test_enabled_with_credentials_and_key(self) -> None:
        src = StockTwitsEmailSource(config=_config(), api_key="sk-test")
        assert src.enabled is True

    def test_disabled_without_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Empty key falls back to the env var; isolate from a real key in .env.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        src = StockTwitsEmailSource(config=_config(), api_key="")
        assert src.enabled is False

    def test_disabled_without_credentials(self) -> None:
        cfg = ImapConfig(
            host="h", port=993, user="", password="", sender="s", subject="j"
        )
        src = StockTwitsEmailSource(config=cfg, api_key="sk-test")
        assert src.enabled is False


# --- vision extraction (mocked Anthropic) ----------------------------------


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._response


class _FakeAnthropicClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, messages: _FakeMessages) -> None:
    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **_kw: _FakeAnthropicClient(messages)
    )


def _ok_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", content=[_FakeTextBlock(text)])


class TestExtractTickers:
    def test_happy_path_parses_and_normalises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        messages = _FakeMessages(_ok_response('{"tickers": ["sndk", " dell ", "mu"]}'))
        _patch_anthropic(monkeypatch, messages)
        src = StockTwitsEmailSource(config=_config(), api_key="sk-test")
        image = stocktwits_email._Image(data=b"\xff\xd8\xff", media_type="image/jpeg")

        tickers = src._extract_tickers(image, "sp500")

        assert tickers == ["SNDK", "DELL", "MU"]
        # image is base64-encoded into an image content block
        content = messages.last_kwargs["messages"][0]["content"]
        assert content[0]["type"] == "image"
        assert content[0]["source"]["media_type"] == "image/jpeg"

    def test_caps_at_twenty_five(self, monkeypatch: pytest.MonkeyPatch) -> None:
        many = ",".join(f'"T{i}"' for i in range(40))
        _patch_anthropic(
            monkeypatch, _FakeMessages(_ok_response(f'{{"tickers": [{many}]}}'))
        )
        src = StockTwitsEmailSource(config=_config(), api_key="sk-test")
        image = stocktwits_email._Image(data=b"x", media_type="image/png")
        assert len(src._extract_tickers(image, "sp500")) == 25

    def test_sdk_error_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_anthropic(monkeypatch, _FakeMessages(error=RuntimeError("boom")))
        src = StockTwitsEmailSource(config=_config(), api_key="sk-test")
        image = stocktwits_email._Image(data=b"x", media_type="image/png")
        assert src._extract_tickers(image, "sp500") == []

    def test_non_end_turn_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        refusal = SimpleNamespace(stop_reason="refusal", content=[])
        _patch_anthropic(monkeypatch, _FakeMessages(refusal))
        src = StockTwitsEmailSource(config=_config(), api_key="sk-test")
        image = stocktwits_email._Image(data=b"x", media_type="image/png")
        assert src._extract_tickers(image, "sp500") == []

    def test_malformed_json_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_anthropic(monkeypatch, _FakeMessages(_ok_response("not json")))
        src = StockTwitsEmailSource(config=_config(), api_key="sk-test")
        image = stocktwits_email._Image(data=b"x", media_type="image/png")
        assert src._extract_tickers(image, "sp500") == []


# --- load() orchestration --------------------------------------------------


def _make_source(tmp_path: Path, api_key: str = "sk-test") -> StockTwitsEmailSource:
    return StockTwitsEmailSource(
        config=_config(), api_key=api_key, watermark_path=tmp_path / "wm.json"
    )


_WHEN = datetime(2026, 8, 1, 19, 3, 54, tzinfo=timezone.utc)


class TestLoad:
    def test_disabled_returns_none_without_io(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("should not touch IMAP when disabled")

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(stocktwits_email.imaplib, "IMAP4_SSL", _boom)
        src = _make_source(tmp_path, api_key="")
        assert src.load() is None

    def test_end_to_end_with_sample_email(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        src = _make_source(tmp_path)
        # Stub the three IO boundaries: email fetch, image download, vision.
        monkeypatch.setattr(src, "_fetch_latest_email", lambda: (_WHEN, SAMPLE_HTML))
        monkeypatch.setattr(
            src,
            "_download_image",
            lambda url: stocktwits_email._Image(b"\xff\xd8\xff", "image/jpeg"),
        )
        monkeypatch.setattr(
            src,
            "_extract_tickers",
            lambda image, index_key: [f"{index_key.upper()}1", f"{index_key.upper()}2"],
        )

        result = src.load()

        assert result is not None
        assert set(result) == {"sp500", "nasdaq100", "russell2000"}
        assert result["sp500"] == ["SP5001", "SP5002"]
        # watermark advanced to the email's received time
        assert src._read_watermark() == _WHEN

    def test_returns_none_when_no_email(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        src = _make_source(tmp_path)
        monkeypatch.setattr(src, "_fetch_latest_email", lambda: None)
        assert src.load() is None

    def test_returns_none_when_all_images_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        src = _make_source(tmp_path)
        monkeypatch.setattr(src, "_fetch_latest_email", lambda: (_WHEN, SAMPLE_HTML))
        monkeypatch.setattr(src, "_download_image", lambda url: None)
        assert src.load() is None
        # no result -> watermark not advanced
        assert src._read_watermark() is None


# --- watermark gating ------------------------------------------------------


class TestWatermark:
    def _stub_pipeline(
        self, monkeypatch: pytest.MonkeyPatch, src: StockTwitsEmailSource
    ) -> list[str]:
        """Stub download + vision; return a list that records vision calls."""
        calls: list[str] = []

        def _fake_extract(image: Any, index_key: str) -> list[str]:
            calls.append(index_key)
            return [f"{index_key.upper()}1"]

        monkeypatch.setattr(
            src,
            "_download_image",
            lambda url: stocktwits_email._Image(b"\xff\xd8\xff", "image/jpeg"),
        )
        monkeypatch.setattr(src, "_extract_tickers", _fake_extract)
        return calls

    def test_skips_when_not_newer_than_watermark(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        src = _make_source(tmp_path)
        src._write_watermark(_WHEN)
        calls = self._stub_pipeline(monkeypatch, src)
        # Same received time as the watermark -> already seen.
        monkeypatch.setattr(src, "_fetch_latest_email", lambda: (_WHEN, SAMPLE_HTML))

        assert src.load() is None
        assert calls == []  # vision never invoked

    def test_processes_when_strictly_newer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        src = _make_source(tmp_path)
        src._write_watermark(_WHEN)
        calls = self._stub_pipeline(monkeypatch, src)
        newer = _WHEN.replace(day=8)
        monkeypatch.setattr(src, "_fetch_latest_email", lambda: (newer, SAMPLE_HTML))

        result = src.load()

        assert result is not None
        assert calls == list(stocktwits_email.INDEX_KEYS)
        assert src._read_watermark() == newer

    def test_first_run_without_watermark_processes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        src = _make_source(tmp_path)
        calls = self._stub_pipeline(monkeypatch, src)
        monkeypatch.setattr(src, "_fetch_latest_email", lambda: (_WHEN, SAMPLE_HTML))

        assert src.load() is not None
        assert calls == list(stocktwits_email.INDEX_KEYS)

    def test_missing_watermark_file_reads_none(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        assert src._read_watermark() is None

    def test_watermark_round_trip(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        src._write_watermark(_WHEN)
        assert src._read_watermark() == _WHEN


# --- ExtractionAgent integration (email-first, config fallback) ------------


class TestExtractionAgentSourceSelection:
    def test_uses_email_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.agents.extraction.extraction_agent import ExtractionAgent
        from app.schemas.source_health import SourceName, SourceState

        monkeypatch.setattr(
            ExtractionAgent,
            "_load_stocktwits_from_email",
            lambda _self: {"sp500": ["AAA", "BBB"]},
        )
        agent = ExtractionAgent(name="ExtractionAgent")
        result = agent._load_stocktwits_config()

        assert agent.stocktwits_source == "email"
        assert result == {"sp500": ["AAA", "BBB"]}
        health = agent._stocktwits_health(sum(len(v) for v in result.values()))
        assert health.state is SourceState.OK
        assert "weekly email" in health.display_message
        assert health.source is SourceName.STOCKTWITS

    def test_falls_back_to_config_when_email_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agents.extraction.extraction_agent import ExtractionAgent

        monkeypatch.setattr(
            ExtractionAgent, "_load_stocktwits_from_email", lambda _self: {}
        )
        monkeypatch.setattr(
            ExtractionAgent,
            "_load_stocktwits_from_config",
            lambda _self: {"sp500": ["ZZZ"]},
        )
        agent = ExtractionAgent(name="ExtractionAgent")
        result = agent._load_stocktwits_config()

        assert agent.stocktwits_source == "config"
        assert result == {"sp500": ["ZZZ"]}
        health = agent._stocktwits_health(1)
        assert "quarterly config" in health.display_message

    def test_email_exception_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agents.extraction.extraction_agent import ExtractionAgent

        def _boom(_self: Any) -> Any:
            raise RuntimeError("imap down")

        monkeypatch.setattr(stocktwits_email.StockTwitsEmailSource, "load", _boom)
        # Force enabled so load() is reached, then blows up and is caught.
        monkeypatch.setattr(
            stocktwits_email.StockTwitsEmailSource,
            "enabled",
            property(lambda _self: True),
        )
        agent = ExtractionAgent(name="ExtractionAgent")
        assert agent._load_stocktwits_from_email() == {}
