"""Weekly StockTwits "Top 25 Momentum" email source (#137).

The newsletter renders its three index lists (S&P 500, Nasdaq 100, Russell
2000) as PNG *images*, not text — the tickers cannot be read from the HTML at
all. This module therefore:

1. fetches the most recent matching email over IMAP,
2. skips (no vision cost) unless its received time is newer than the stored
   watermark — so an already-seen weekly issue is never re-processed,
3. extracts the three ``momentum-<index>-<date>.png`` image URLs from the HTML,
4. downloads each image and asks Claude (vision) for the Top 25 tickers,
5. records the email's received time as the new watermark.

Every step degrades to ``None``/``{}`` on any failure so ``ExtractionAgent``
falls back to the quarterly-curated ``config/stocktwits_watchlist.json`` — the
pipeline never fails because email/IMAP/Claude is unset or unreachable.

Configuration (env vars; the IMAP credentials are the *same* Gmail app
password already used for sending in ``AlertAgent``):

- ``EMAIL_USER`` / ``EMAIL_PASSWORD`` — reused from the send config.
- ``EMAIL_IMAP_HOST`` (default ``imap.gmail.com``) / ``EMAIL_IMAP_PORT`` (993).
- ``STOCKTWITS_EMAIL_FROM`` (default ``stocktwits.com``) — IMAP FROM filter.
- ``STOCKTWITS_EMAIL_SUBJECT`` (default ``Stocktwits Top 25``) — SUBJECT filter.
- ``ANTHROPIC_API_KEY`` — for the vision extraction step.
"""

from __future__ import annotations

import base64
import email
import imaplib
import json
import os
import quopri
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import default as _DEFAULT_POLICY
from pathlib import Path
from typing import Any

import requests

from app.core.config import STOCKTWITS_EMAIL_WATERMARK_JSON

# Canonical index keys, matching config/stocktwits_watchlist.json and the shape
# ExtractionAgent already consumes.
INDEX_KEYS: tuple[str, ...] = ("sp500", "nasdaq100", "russell2000")
_INDEX_LABELS: dict[str, str] = {
    "sp500": "S&P 500",
    "nasdaq100": "Nasdaq 100",
    "russell2000": "Russell 2000",
}

# Full image URLs like
# https://media.beehiiv.com/.../momentum-sp500-2026-07-31.png?t=...
_IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>]*"
    r"momentum-(sp500|nasdaq100|russell2000)-\d{4}-\d{2}-\d{2}\.png"
    r"[^\s\"'<>]*",
    re.IGNORECASE,
)

_VISION_MODEL = "claude-sonnet-5"
_MAX_TOKENS = 512
_TICKERS_PER_LIST = 25

_IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

_VISION_SYSTEM_PROMPT = (
    "You extract stock ticker symbols from an image of a StockTwits "
    "'Top 25 Momentum' table. Read only the main ranked Top 25 table "
    "(rows 1 through 25) and return the symbol from its TICKER column, in "
    "rank order. Ignore any 'OUT / Fell off top 25' section, the header, "
    "prices, and every other column. Return only tickers you can actually "
    "read; never invent symbols."
)

_TICKERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"tickers": {"type": "array", "items": {"type": "string"}}},
    "required": ["tickers"],
    "additionalProperties": False,
}


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to ``default`` on unset/invalid."""
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class ImapConfig:
    """IMAP connection + message-selection settings for the StockTwits email."""

    host: str
    port: int
    user: str
    password: str
    sender: str
    subject: str

    @classmethod
    def from_env(cls) -> ImapConfig:
        """Build the config from environment variables (see module docstring)."""
        return cls(
            host=os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com"),
            port=_env_int("EMAIL_IMAP_PORT", 993),
            user=os.getenv("EMAIL_USER", ""),
            password=os.getenv("EMAIL_PASSWORD", ""),
            sender=os.getenv("STOCKTWITS_EMAIL_FROM", "stocktwits.com"),
            subject=os.getenv("STOCKTWITS_EMAIL_SUBJECT", "Stocktwits Top 25"),
        )


@dataclass(frozen=True)
class _Image:
    """A downloaded image with its detected media type."""

    data: bytes
    media_type: str


def _looks_quoted_printable(text: str) -> bool:
    """Return True when the text still carries quoted-printable escapes."""
    return "=3D" in text or "=\n" in text or "=\r\n" in text


def extract_list_image_urls(html: str) -> dict[str, str]:
    """Return ``{index_key: image_url}`` for the three Top 25 list images.

    Accepts either decoded HTML or the raw quoted-printable message body (as
    saved from a mail client); the latter is decoded first. Only the first URL
    seen per index is kept.
    """
    decoded = html
    if _looks_quoted_printable(html):
        decoded = quopri.decodestring(html.encode("utf-8", "replace")).decode(
            "utf-8", "replace"
        )

    urls: dict[str, str] = {}
    for match in _IMAGE_URL_RE.finditer(decoded):
        index_key = match.group(1).lower()
        urls.setdefault(index_key, match.group(0))
    return urls


def _parse_internaldate(envelope: Any) -> datetime | None:
    """Parse an IMAP fetch envelope's INTERNALDATE into an aware UTC datetime."""
    try:
        parsed = imaplib.Internaldate2tuple(envelope)
        if parsed is None:
            return None
        # Internaldate2tuple returns local time; mktime -> epoch -> UTC.
        return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
    except Exception:
        return None


def _detect_media_type(data: bytes) -> str:
    """Best-effort image media type from magic bytes (default JPEG)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class StockTwitsEmailSource:
    """Load the weekly StockTwits Top 25 lists from the newsletter email.

    ``load()`` returns ``{index_key: [tickers]}`` or ``None`` on any failure,
    mirroring ``AnthropicNarrativeClient``'s graceful-skip contract so the
    caller can always fall back to the static config.
    """

    def __init__(
        self,
        config: ImapConfig | None = None,
        api_key: str | None = None,
        watermark_path: Path | None = None,
    ) -> None:
        self.config = config or ImapConfig.from_env()
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.watermark_path = watermark_path or STOCKTWITS_EMAIL_WATERMARK_JSON

    @property
    def enabled(self) -> bool:
        """True when IMAP credentials and an Anthropic key are configured."""
        return bool(self.config.user and self.config.password and self.api_key)

    def load(self) -> dict[str, list[str]] | None:
        """Return per-index Top 25 tickers from a newer-than-watermark email.

        Returns ``None`` when the source is unconfigured, no matching email is
        found, or the newest email is not newer than the recorded watermark (so
        an already-seen weekly issue is never re-processed). On success the
        watermark is advanced to the email's received time.
        """
        if not self.enabled:
            return None
        fetched = self._fetch_latest_email()
        if fetched is None:
            return None
        received_at, html = fetched

        watermark = self._read_watermark()
        if received_at is not None and watermark is not None:
            if received_at <= watermark:
                return None  # nothing newer than what we've already processed

        urls = extract_list_image_urls(html)
        if not urls:
            return None

        result: dict[str, list[str]] = {}
        for index_key in INDEX_KEYS:
            url = urls.get(index_key)
            if not url:
                continue
            image = self._download_image(url)
            if image is None:
                continue
            tickers = self._extract_tickers(image, index_key)
            if tickers:
                result[index_key] = tickers

        if result and received_at is not None:
            self._write_watermark(received_at)
        return result or None

    def _fetch_latest_email(self) -> tuple[datetime | None, str] | None:
        """Return ``(received_at, html)`` for the newest matching email, or None.

        ``received_at`` is the server's IMAP ``INTERNALDATE`` (received time) as
        an aware UTC datetime, or ``None`` if it can't be parsed.
        """
        try:
            with imaplib.IMAP4_SSL(self.config.host, self.config.port) as imap:
                imap.login(self.config.user, self.config.password)
                imap.select("INBOX")
                typ, data = imap.search(
                    None,
                    "FROM",
                    self.config.sender,
                    "SUBJECT",
                    self.config.subject,
                )
                if typ != "OK" or not data or not data[0]:
                    return None
                latest_id = data[0].split()[-1]
                typ, msg_data = imap.fetch(latest_id, "(INTERNALDATE RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    return None
                envelope, raw = msg_data[0][0], msg_data[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    return None
                message = email.message_from_bytes(raw, policy=_DEFAULT_POLICY)
                html = _html_body(message)
                if html is None:
                    return None
                return _parse_internaldate(envelope), html
        except Exception:
            return None

    def _read_watermark(self) -> datetime | None:
        """Return the last-processed email's received time, or None."""
        try:
            raw = json.loads(self.watermark_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        stamp = raw.get("last_seen") if isinstance(raw, dict) else None
        if not stamp:
            return None
        try:
            return datetime.fromisoformat(str(stamp))
        except ValueError:
            return None

    def _write_watermark(self, received_at: datetime) -> None:
        """Persist ``received_at`` as the new watermark (best effort)."""
        try:
            self.watermark_path.write_text(
                json.dumps({"last_seen": received_at.isoformat()}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _download_image(self, url: str) -> _Image | None:
        """Download an image URL, or None on any request/HTTP failure."""
        try:
            resp = requests.get(url, headers=_IMG_HEADERS, timeout=20)
            resp.raise_for_status()
            return _Image(
                data=resp.content, media_type=_detect_media_type(resp.content)
            )
        except Exception:
            return None

    def _extract_tickers(self, image: _Image, index_key: str) -> list[str]:
        """Ask Claude (vision) for the Top 25 tickers, or [] on any failure."""
        try:
            import anthropic
        except ImportError:
            return []
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.media_type,
                    "data": base64.standard_b64encode(image.data).decode("ascii"),
                },
            },
            {"type": "text", "text": _vision_user_prompt(index_key)},
        ]
        messages: list[Any] = [{"role": "user", "content": content}]
        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model=_VISION_MODEL,
                max_tokens=_MAX_TOKENS,
                system=_VISION_SYSTEM_PROMPT,
                messages=messages,
                output_config={
                    "format": {"type": "json_schema", "schema": _TICKERS_SCHEMA}
                },
            )
        except Exception:
            return []

        if response.stop_reason != "end_turn":
            return []
        try:
            text = next(b.text for b in response.content if b.type == "text")
            payload = json.loads(text)
            tickers = payload.get("tickers", [])
            cleaned = [str(t).strip().upper() for t in tickers if str(t).strip()]
            return cleaned[:_TICKERS_PER_LIST]
        except Exception:
            return []


def _html_body(message: EmailMessage) -> str | None:
    """Return the ``text/html`` part's decoded content, or None."""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/html":
                return str(part.get_content())
        return None
    if message.get_content_type() == "text/html":
        return str(message.get_content())
    return None


def _vision_user_prompt(index_key: str) -> str:
    """Build the per-index user prompt for the vision extraction call."""
    label = _INDEX_LABELS.get(index_key, index_key)
    return (
        f"This is the StockTwits Top 25 Momentum table for the {label}. "
        "Extract the 25 ticker symbols from the main table's TICKER column, "
        "in rank order (1 to 25)."
    )
