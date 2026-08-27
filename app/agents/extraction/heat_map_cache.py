"""Durable single-entry cache for the WhaleWisdom heat-map payload (#345).

One JSON file holds the last fully-parsed, non-empty heat-map response keyed to
its period/quarter string, stamped with a UTC ``fetched_at``. An institutional
refresh reuses a fresh (< 7 day) entry with no network call and auto-refetches
once it expires. ``CACHE_PATH`` is a module-level seam read at call time so
tests can monkeypatch ``heat_map_cache.CACHE_PATH``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict
from uuid import uuid4

from app.core import config

CACHE_PATH = config.HEAT_MAP_CACHE_JSON

_FRESH_WINDOW = timedelta(days=7)


class CacheEntry(TypedDict):
    """Shape of a stored heat-map cache entry."""

    source_period: str
    fetched_at: str
    children: list[dict[str, Any]]
    heat_map_id: int


def store(period: str, children: list[dict[str, Any]], heat_map_id: int) -> None:
    """Write ``period`` + ``children`` to the cache, stamped with UTC now.

    Writes to a process-unique temp path then ``os.replace``s it into place so
    a concurrent reader never sees a partial file. A falsy ``period`` is a
    no-op (the caller also guards against it).
    """
    if not period:
        return
    entry: CacheEntry = {
        "source_period": str(period),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "children": list(children),
        "heat_map_id": int(heat_map_id),
    }
    tmp_path = CACHE_PATH.with_name(
        f"{CACHE_PATH.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    tmp_path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    try:
        os.replace(tmp_path, CACHE_PATH)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def load() -> CacheEntry | None:
    """Return the stored entry, or ``None`` when it is missing or unusable.

    A missing file, an unreadable file (``OSError``), or invalid content
    (``ValueError`` incl. JSON/unicode errors, non-dict root, missing keys,
    non-``str`` ``source_period``, non-``list`` ``children``, or any non-``dict``
    child) all read as empty.
    """
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None

    if not isinstance(raw, dict):
        return None
    if "source_period" not in raw or "fetched_at" not in raw or "children" not in raw:
        return None
    period = raw["source_period"]
    children = raw["children"]
    if not isinstance(period, str):
        return None
    if not isinstance(children, list):
        return None
    if any(not isinstance(child, dict) for child in children):
        return None
    try:
        heat_map_id = int(raw["heat_map_id"])
    except (KeyError, TypeError, ValueError):
        heat_map_id = -1
    return {
        "source_period": period,
        "fetched_at": str(raw["fetched_at"]),
        "children": children,
        "heat_map_id": heat_map_id,
    }


def entry_fetched_at(entry: CacheEntry) -> datetime | None:
    """Parse ``entry['fetched_at']`` to an aware UTC datetime, or ``None``."""
    try:
        parsed = datetime.fromisoformat(str(entry.get("fetched_at", "")))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_fresh(entry: CacheEntry, now: datetime) -> bool:
    """True when ``now - fetched_at`` is within ``[0, 7 days)``.

    A future or unparseable ``fetched_at`` is not fresh.
    """
    fetched_at = entry_fetched_at(entry)
    if fetched_at is None:
        return False
    age = now - fetched_at
    return timedelta(0) <= age < _FRESH_WINDOW
