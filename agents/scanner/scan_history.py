"""Scan history — persists ticker + entry_zone per run to detect transitions.

File format  {date_str: {ticker: entry_zone_str}}
Old format   {date_str: [ticker, ...]}  — read-only backward compat, migrated on next save.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import StockRecord

_HISTORY_FILE = Path(__file__).parent / "scan_history.json"
_MAX_SNAPSHOTS = 30

# Type alias: one snapshot maps ticker → entry_zone (or "" if zone unknown)
Snapshot = dict[str, str]


def load_history() -> dict[str, Snapshot]:
    """Return {date_str: {ticker: zone}} for all past runs."""
    try:
        raw: dict[str, list[str] | Snapshot] = json.loads(
            _HISTORY_FILE.read_text(encoding="utf-8")
        )
    except OSError:
        return {}
    # Migrate old list-only format transparently
    result: dict[str, Snapshot] = {}
    for dt, val in raw.items():
        if isinstance(val, list):
            result[dt] = {t: "" for t in val}
        else:
            result[dt] = val
    return result


def get_new_tickers(current: list[str], history: dict[str, Snapshot]) -> set[str]:
    """Return tickers absent from the most recent run (all new if no history)."""
    if not history:
        return set(current)
    prev = history[max(history.keys())]
    return {t for t in current if t not in prev}


def get_fresh_breakouts(
    current_records: list[StockRecord],
    history: dict[str, Snapshot],
) -> set[str]:
    """Return tickers that just transitioned INTO 'broken_out' this run.

    A ticker qualifies if:
      - Its current entry_zone is 'broken_out', AND
      - In the previous run it was either absent or had a different zone.

    When there is no history at all, every currently-broken-out ticker is
    returned so the first run still produces a useful Breakouts sheet.
    """
    broken_out_now = {
        r.ticker
        for r in current_records
        if r.analysis and r.analysis.entry_zone == "broken_out"
    }
    if not history:
        return broken_out_now

    prev = history[max(history.keys())]
    return {t for t in broken_out_now if prev.get(t) != "broken_out"}


def save_history(
    current_records: list[StockRecord],
    history: dict[str, Snapshot],
) -> None:
    """Append today's {ticker: zone} snapshot and write to disk (max 30 entries)."""
    today = date.today().isoformat()
    history[today] = {
        r.ticker: (r.analysis.entry_zone if r.analysis else "")
        for r in current_records
    }
    if len(history) > _MAX_SNAPSHOTS:
        for k in sorted(history.keys())[:-_MAX_SNAPSHOTS]:
            del history[k]
    _HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
