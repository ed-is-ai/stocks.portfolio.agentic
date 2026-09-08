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
from pathlib import Path

from app.core.config import PROVIDER_SYMBOL_ALIASES_JSON, TICKER_ALIASES_JSON

logger = logging.getLogger(__name__)

# Process-level last-good ticker-alias map. Plain module variable by design
# (GH-531): no persistent/on-disk cache. ``None`` until a ``load_aliases()``
# call first succeeds.
_last_good_aliases: dict[str, str] | None = None


class AliasFileUnreadableError(RuntimeError):
    """Raised by ``load_aliases()`` when ``config/ticker_aliases.json`` is
    present but unreadable/invalid and no last-good map has been cached yet.

    The cold-start backstop for GH-531: degrading to raw import spellings
    silently re-identifies every aliased holding and can value it off a
    stale ``price_cache`` row, so a first-ever load that cannot read a
    present file aborts loudly instead.
    """


class _AliasLoadError(RuntimeError):
    """Internal: a present alias file could not be read or parsed."""


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
    """Load the ticker-alias map, distinguishing "no aliases configured"
    from "the alias file broke".

    A missing ``config/ticker_aliases.json`` returns ``{}`` (a legitimate
    state). A file that is present but unreadable or invalid -- a
    permission error or any other ``OSError``, non-UTF-8 bytes, invalid
    JSON, JSON that isn't a ``dict[str, str]`` with non-empty keys/values
    -- does *not* degrade to ``{}`` (GH-531): silently re-identifying every
    aliased holding by its raw spelling can value it off a stale
    ``price_cache`` row. Instead it returns the process-cached last-good
    map if one exists (logged at WARNING), else raises
    ``AliasFileUnreadableError``. A successful load is cached as last-good.
    """
    global _last_good_aliases
    try:
        data = _load_alias_map_strict(TICKER_ALIASES_JSON)
    except FileNotFoundError:
        return {}
    except _AliasLoadError as exc:
        if _last_good_aliases is not None:
            logger.warning("Reusing last-good ticker aliases: %s", exc)
            return dict(_last_good_aliases)
        raise AliasFileUnreadableError(str(exc)) from exc
    _last_good_aliases = data
    return data


def load_provider_symbol_aliases() -> dict[str, str]:
    """Load the versioned source-to-provider symbol spelling map.

    Stays lenient: returns ``{}`` on any read/parse/shape failure.
    """
    try:
        return _load_alias_map_strict(PROVIDER_SYMBOL_ALIASES_JSON)
    except FileNotFoundError:
        return {}
    except _AliasLoadError as exc:
        logger.warning("Ignoring provider symbol aliases: %s", exc)
        return {}


def _load_alias_map_strict(path: Path) -> dict[str, str]:
    """Read and validate an alias file, raising on any failure.

    ``FileNotFoundError`` propagates (a legitimately absent file) and an
    empty/whitespace-only file is treated as "no aliases" (``{}``); every
    other read/parse/shape problem is raised as ``_AliasLoadError``.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise _AliasLoadError(f"could not read {path}: {exc}") from exc

    if not raw_text.strip():
        return {}

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise _AliasLoadError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k and v for k, v in data.items()
    ):
        raise _AliasLoadError(
            f"{path} is not a dict[str, str] with non-empty keys/values"
        )

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
