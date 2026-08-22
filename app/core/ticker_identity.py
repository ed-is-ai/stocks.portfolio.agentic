"""Ticker alias canonicalization -- the one identity FIFO matching,
average-cost replay, SIPP import, and repository-level ticker lookups
must all agree on.

Dependency-free (stdlib ``json`` only, plus ``app.core.config`` for the
alias-file path), following ``app/core/money.py``'s AD-2 precedent for a
shared type multiple layers need without a reverse dependency from
``app/core/`` into ``app/agents/``. ``TraderAgent`` (a lower layer) needs
to canonicalize a ticker at import/replay time, but ``PortfolioService`` --
which owns ``config/ticker_aliases.json`` today via
``load_ticker_aliases()`` -- sits *above* ``TraderAgent`` in the dependency
graph (``PortfolioService`` -> ``TraderService`` -> ``TraderAgent``), so
``TraderAgent`` importing ``PortfolioService`` to canonicalize would be
circular. This module is importable from every layer with no cycle;
``PortfolioService.load_ticker_aliases()`` delegates to ``load_aliases()``
here so there is a single source of truth for the alias data.
"""

from __future__ import annotations

import json
import logging

from app.core.config import TICKER_ALIASES_JSON

logger = logging.getLogger(__name__)


class AmbiguousTickerAliasError(ValueError):
    """Raised when a ticker's alias chain revisits a ticker before reaching
    a fixed point -- a genuine cycle, the only shape a flat
    ``dict[str, str]`` alias map can produce with no well-defined answer.

    A linear chain (a rename-of-a-rename, e.g. ``{"ABC.L": "ABC", "ABC":
    "ABC-NEW"}``) is *not* ambiguous -- ``canonical_ticker`` walks it in
    full and returns its terminal value (``"ABC-NEW"``) without raising.
    Only a chain that loops back on a ticker already visited while walking
    it (e.g. ``{"ABC.L": "ABC", "ABC": "ABC.L"}``) raises. The message
    includes the full cycle path walked, not just the starting ticker, so
    a ``failed_rows`` entry or warning log is actually debuggable against
    a real misconfigured file.
    """

    def __init__(self, cycle_path: list[str]) -> None:
        self.cycle_path = cycle_path
        path_text = " -> ".join(cycle_path)
        super().__init__(f"ambiguous ticker alias cycle: {path_text}")


def load_aliases() -> dict[str, str]:
    """Load the ticker-alias map, tolerating every failure mode a
    hand-edited config file can produce.

    Returns ``{}`` for a missing ``config/ticker_aliases.json``, an
    unreadable one (permission error, a directory in its place, or any
    other ``OSError``), non-UTF-8 bytes, invalid JSON, syntactically valid
    JSON that isn't a ``dict[str, str]`` (a list, or a dict with
    non-string keys/values), or an entry with an empty-string key or
    value -- a malformed alias file must degrade every caller to unaliased
    tickers, never crash import, replay, or currency lookup. Logs a
    warning (not silent) whenever it falls back to ``{}``, except for the
    ordinary "file doesn't exist yet" case.
    """
    try:
        raw_text = TICKER_ALIASES_JSON.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read %s: %s", TICKER_ALIASES_JSON, exc)
        return {}

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in %s: %s", TICKER_ALIASES_JSON, exc)
        return {}

    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k and v for k, v in data.items()
    ):
        logger.warning(
            "%s is not a dict[str, str] with non-empty keys/values -- ignoring",
            TICKER_ALIASES_JSON,
        )
        return {}

    return data


def canonical_ticker(ticker: str, aliases: dict[str, str]) -> str:
    """Resolve ``ticker`` through ``aliases`` to its terminal value.

    Walks the chain (``aliases.get(t, t)``, repeated) to its fixed point --
    a ticker that maps to itself, or that isn't a key in ``aliases`` at
    all -- tracking every ticker visited along the way. An unconfigured
    ticker passes through unchanged (no error). A multi-hop chain (a
    rename-of-a-rename) is walked in full and returns its terminal value,
    never rejected. Only a genuine cycle -- a ticker revisited before
    reaching a fixed point -- raises ``AmbiguousTickerAliasError``.
    """
    visited: list[str] = [ticker]
    current = ticker
    while True:
        next_ticker = aliases.get(current, current)
        if next_ticker == current:
            return current
        if next_ticker in visited:
            raise AmbiguousTickerAliasError(visited + [next_ticker])
        visited.append(next_ticker)
        current = next_ticker


def matching_raw_tickers(canonical: str, aliases: dict[str, str]) -> set[str]:
    """Return every raw spelling whose forward resolution reaches
    ``canonical``.

    The reverse of ``canonical_ticker`` -- a canonical ticker isn't
    necessarily what's stored on a persisted trade row (rows are written
    under whatever a broker ``Symbol`` -- or an earlier, shorter alias
    chain -- resolved to at *import* time), so any operation that needs to
    find or delete rows *by* a canonical identity must translate that one
    value back into every raw spelling that could be stored under it.

    Always includes ``canonical`` itself (a raw, never-aliased ticker is
    its own match). First resolves its own ``canonical`` argument through
    ``canonical_ticker``, so this is correct even if a caller passes a
    non-canonical raw spelling instead of the true canonical value (if
    that resolution itself hits a cycle, ``canonical`` is used as-is
    rather than propagating the exception out of a reverse lookup).
    Computed by scanning the small in-memory ``aliases`` dict fresh on
    every call -- no persistent reverse index or cache.

    """
    try:
        resolved = canonical_ticker(canonical, aliases)
    except AmbiguousTickerAliasError as exc:
        logger.warning(
            "matching_raw_tickers: ambiguous ticker alias for %r -- "
            "falling back to raw ticker as its own identity: %s",
            canonical,
            exc,
        )
        resolved = canonical

    matches = {resolved}
    for raw in aliases:
        try:
            if canonical_ticker(raw, aliases) == resolved:
                matches.add(raw)
        except AmbiguousTickerAliasError as exc:
            # This raw spelling's own chain cycles -- we cannot tell
            # whether it belongs in the result, so it's excluded rather
            # than guessed. Logged (unlike a silent `continue`) so a
            # `delete_by_ticker`/`history` caller that appears to miss
            # rows has a diagnosable trail back to the misconfigured
            # alias file, rather than silently under-deleting.
            logger.warning(
                "matching_raw_tickers: ambiguous ticker alias for raw "
                "spelling %r -- excluded from the match set: %s",
                raw,
                exc,
            )
            continue
    return matches


def canonicalize_or_fallback(
    ticker: str,
    aliases: dict[str, str],
    *,
    logger: logging.Logger,
    context: str,
) -> str:
    """Resolve ``ticker`` for a read-time (non-import) call site.

    Shared by every replay/read call site (``TraderAgent._replay_trades``,
    ``RealisedPnlService._replay_fifo``, ``TradesRepository.held_tickers``/
    ``.history``, ``PortfolioService.ticker_currency``, and
    ``PortfolioService.fetch_all_prices``) so the
    "canonicalize, catch-and-degrade" shape lives once. Any ambiguous
    (cyclic) alias chain logs a warning via ``logger`` (naming ``context``
    and the raw ticker) and falls back to the raw ticker rather than
    raising -- these call sites replay already-persisted trades or already-
    displayed data with no per-row channel to surface an error to a user,
    unlike SIPP import, which rejects the row instead.

    Every ticker follows the same data-driven alias path; there are no
    reserved literals or identity-specific exceptions.
    """
    try:
        resolved = canonical_ticker(ticker, aliases)
    except AmbiguousTickerAliasError as exc:
        logger.warning(
            "%s: ambiguous ticker alias for %r -- falling back to raw ticker: %s",
            context,
            ticker,
            exc,
        )
        return ticker
    return resolved
