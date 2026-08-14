"""Tests for ``app.core.ticker_identity`` -- the shared ticker-canonicalization
seam used by SIPP import, FIFO replay, average-cost replay, and the
ticker-identity repository methods (Story 2.1).

Round 7's golden reference fixed two regressions introduced by round 6's
from-scratch rewrite: a self-mapping alias entry (``{"ABC": "ABC"}``)
spuriously raising ``AmbiguousTickerAliasError``, and ``matching_raw_tickers``/
``canonicalize_or_fallback`` only checking their *input* against the reserved
``"HSFWA"`` literal, never their *resolved output* -- letting an unrelated
ticker's chain silently merge into the reserved HSBC GLOB identity. Both are
covered here as explicit regression tests, alongside every edge case from
prior rounds.
"""

import json
import logging
from pathlib import Path

import pytest

from app.core import ticker_identity
from app.core.ticker_identity import (
    HSFWA_TICKER,
    AmbiguousTickerAliasError,
    canonical_ticker,
    canonicalize_or_fallback,
    load_aliases,
    matching_raw_tickers,
)


# --- canonical_ticker -------------------------------------------------------


def test_canonical_ticker_passthrough_when_unconfigured() -> None:
    assert canonical_ticker("XYZ", {}) == "XYZ"


def test_canonical_ticker_direct_alias() -> None:
    assert canonical_ticker("ABC.L", {"ABC.L": "ABC"}) == "ABC"


def test_canonical_ticker_chained_rename_resolves_to_terminal_value() -> None:
    aliases = {"ABC.L": "ABC", "ABC": "ABC-NEW"}
    assert canonical_ticker("ABC.L", aliases) == "ABC-NEW"


def test_canonical_ticker_true_cycle_raises_with_full_path() -> None:
    aliases = {"ABC.L": "ABC", "ABC": "ABC.L"}
    with pytest.raises(AmbiguousTickerAliasError) as exc_info:
        canonical_ticker("ABC.L", aliases)
    assert exc_info.value.cycle_path == ["ABC.L", "ABC", "ABC.L"]
    assert "ABC.L -> ABC -> ABC.L" in str(exc_info.value)


def test_canonical_ticker_self_mapping_regression() -> None:
    """Round 6 regression: a self-mapping alias entry (a plausible-if-unusual
    hand-edit) must resolve to itself, not be mistaken for a cycle."""
    assert canonical_ticker("ABC", {"ABC": "ABC"}) == "ABC"


# --- matching_raw_tickers ----------------------------------------------------


def test_matching_raw_tickers_reverse_resolution() -> None:
    aliases = {"ABC.L": "ABC"}
    assert matching_raw_tickers("ABC", aliases) == {"ABC", "ABC.L"}


def test_matching_raw_tickers_accepts_non_canonical_raw_spelling() -> None:
    """First resolves its own ``canonical`` argument, so passing a raw
    (non-canonical) spelling still produces the correct full match set."""
    aliases = {"ABC.L": "ABC"}
    assert matching_raw_tickers("ABC.L", aliases) == {"ABC", "ABC.L"}


def test_matching_raw_tickers_no_alias_configured() -> None:
    assert matching_raw_tickers("XYZ", {}) == {"XYZ"}


def test_matching_raw_tickers_skips_raw_key_whose_own_chain_is_a_cycle() -> None:
    aliases = {"ABC.L": "ABC", "CYCLE.A": "CYCLE.B", "CYCLE.B": "CYCLE.A"}
    assert matching_raw_tickers("ABC", aliases) == {"ABC", "ABC.L"}


def test_matching_raw_tickers_hsfwa_short_circuits_as_canonical_input() -> None:
    assert matching_raw_tickers(HSFWA_TICKER, {"XYZ": HSFWA_TICKER}) == {HSFWA_TICKER}


def test_matching_raw_tickers_hsfwa_leak_regression() -> None:
    """Round 7 regression: some *other* ticker's chain landing on the
    reserved ``"HSFWA"`` literal must never be discovered as HSFWA's own
    raw spelling, nor let HSFWA's own alias entry sweep into that other
    ticker's match set."""
    assert matching_raw_tickers("XYZ", {"XYZ": HSFWA_TICKER}) == {"XYZ"}


def test_matching_raw_tickers_skips_hsfwa_raw_key_during_scan() -> None:
    """A raw ``"HSFWA"`` entry is never swept into another ticker's match
    set even if HSFWA's own alias value happens to coincide with it."""
    aliases = {HSFWA_TICKER: "REAL.L", "OTHER": "REAL.L"}
    assert matching_raw_tickers("REAL.L", aliases) == {"REAL.L", "OTHER"}


# --- canonicalize_or_fallback ------------------------------------------------


def test_canonicalize_or_fallback_passthrough() -> None:
    logger = logging.getLogger("test")
    assert canonicalize_or_fallback("XYZ", {}, logger=logger, context="ctx") == "XYZ"


def test_canonicalize_or_fallback_resolves_chain() -> None:
    logger = logging.getLogger("test")
    aliases = {"ABC.L": "ABC", "ABC": "ABC-NEW"}
    assert (
        canonicalize_or_fallback("ABC.L", aliases, logger=logger, context="ctx")
        == "ABC-NEW"
    )


def test_canonicalize_or_fallback_ambiguous_cycle_degrades_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test")
    aliases = {"ABC.L": "ABC", "ABC": "ABC.L"}
    with caplog.at_level(logging.WARNING):
        result = canonicalize_or_fallback(
            "ABC.L", aliases, logger=logger, context="my_context"
        )
    assert result == "ABC.L"
    assert any("my_context" in r.message for r in caplog.records)


def test_canonicalize_or_fallback_protects_hsfwa_input_by_default() -> None:
    logger = logging.getLogger("test")
    aliases = {HSFWA_TICKER: "REAL.L"}
    assert (
        canonicalize_or_fallback(HSFWA_TICKER, aliases, logger=logger, context="ctx")
        == HSFWA_TICKER
    )


def test_canonicalize_or_fallback_hsfwa_leak_regression() -> None:
    """Round 7 regression: an unrelated ticker whose chain resolves to the
    reserved ``"HSFWA"`` literal must fall back to the raw input, never
    silently return ``"HSFWA"``, under the default (protected) mode."""
    logger = logging.getLogger("test")
    aliases = {"XYZ": HSFWA_TICKER}
    assert (
        canonicalize_or_fallback("XYZ", aliases, logger=logger, context="ctx") == "XYZ"
    )


def test_canonicalize_or_fallback_protect_hsfwa_false_resolves_through_alias() -> None:
    """Price/currency lookup opts out of HSFWA protection -- its own
    configured alias exists specifically to redirect its price fetch."""
    logger = logging.getLogger("test")
    aliases = {HSFWA_TICKER: "REAL.L"}
    assert (
        canonicalize_or_fallback(
            HSFWA_TICKER,
            aliases,
            logger=logger,
            context="ctx",
            protect_hsfwa=False,
        )
        == "REAL.L"
    )


# --- load_aliases ------------------------------------------------------------


def test_load_aliases_missing_file_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ticker_identity, "TICKER_ALIASES_JSON", tmp_path / "does_not_exist.json"
    )
    assert load_aliases() == {}


def test_load_aliases_valid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ticker_aliases.json"
    path.write_text(json.dumps({"ABC.L": "ABC"}), encoding="utf-8")
    monkeypatch.setattr(ticker_identity, "TICKER_ALIASES_JSON", path)
    assert load_aliases() == {"ABC.L": "ABC"}


def test_load_aliases_invalid_json_returns_empty_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "ticker_aliases.json"
    path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(ticker_identity, "TICKER_ALIASES_JSON", path)
    with caplog.at_level(logging.WARNING):
        assert load_aliases() == {}
    assert any("Invalid JSON" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "payload",
    [
        ["ABC.L", "ABC"],
        {"ABC.L": 1},
        {"": "ABC"},
        {"ABC.L": ""},
    ],
)
def test_load_aliases_wrong_shape_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    path = tmp_path / "ticker_aliases.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ticker_identity, "TICKER_ALIASES_JSON", path)
    assert load_aliases() == {}


def test_load_aliases_os_error_returns_empty_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _BoomPath:
        def read_text(self, encoding: str = "utf-8") -> str:
            raise PermissionError("denied")

    monkeypatch.setattr(ticker_identity, "TICKER_ALIASES_JSON", _BoomPath())
    with caplog.at_level(logging.WARNING):
        assert load_aliases() == {}
    assert any("Could not read" in r.message for r in caplog.records)


def test_load_aliases_unicode_decode_error_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoomPath:
        def read_text(self, encoding: str = "utf-8") -> str:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")

    monkeypatch.setattr(ticker_identity, "TICKER_ALIASES_JSON", _BoomPath())
    assert load_aliases() == {}
