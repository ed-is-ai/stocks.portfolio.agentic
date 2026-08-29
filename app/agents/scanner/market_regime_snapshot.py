"""Persist / load the scan's broad-market regime reading for the UI banner.

Mirrors the save/load pair in ``app.agents.scanner.market_narrative``: an
atomic temp-sibling + ``os.replace`` write, and a ``load`` that returns
``None`` on any missing/corrupt file. Additionally, ``load_market_regime``
rejects a snapshot whose ``return_52w_pct`` is non-finite (NaN/inf) — both
``json.loads`` and pydantic accept those, so the guard is explicit.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from uuid import uuid4

from app.core.config import MARKET_REGIME_JSON
from app.core.market_regime import MarketRegimeReadingV1
from app.schemas.market_regime import MarketRegimeSnapshotV1


def save_market_regime(reading: MarketRegimeReadingV1) -> None:
    """Persist *reading* atomically so re-renders reuse the same snapshot.

    Writes a process-unique temp sibling then ``os.replace``s it into place,
    wrapped in ``try/except BaseException`` so an interrupt or ENOSPC cannot
    orphan a ``.tmp`` file in the source tree. Mirrors
    ``app.agents.scanner.market_narrative.save_market_narrative``.
    """
    snapshot = MarketRegimeSnapshotV1.from_reading(
        reading, datetime.now(timezone.utc).isoformat()
    )
    MARKET_REGIME_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = MARKET_REGIME_JSON.with_name(
        f"{MARKET_REGIME_JSON.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    try:
        tmp_path.write_text(
            json.dumps(snapshot.model_dump(mode="json"), indent=2, allow_nan=False),
            encoding="utf-8",
        )
        os.replace(tmp_path, MARKET_REGIME_JSON)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def load_market_regime() -> MarketRegimeSnapshotV1 | None:
    """Return the persisted regime snapshot from the latest run, or None.

    ``None`` is returned for a missing or corrupt file, when the stored
    ``return_52w_pct`` is non-finite (NaN/inf) — which would otherwise render
    as ``+nan%`` — and when the snapshot is flagged ``is_degraded`` so a
    fail-safe reading never drives the banner (the scanner does not persist
    degraded readings; this guards a hand-written or corrupted file).
    """
    try:
        payload = json.loads(MARKET_REGIME_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        snapshot = MarketRegimeSnapshotV1.model_validate(payload)
    except Exception:
        return None
    if snapshot.is_degraded or not math.isfinite(snapshot.return_52w_pct):
        return None
    return snapshot
