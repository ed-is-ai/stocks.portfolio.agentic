"""Story 4.2 coverage: canonical selected-security universe identity.

``canonical_run_universe`` is the one authority turning a raw host
selection into the sorted, deduplicated tuple a Run is sealed against, and
``run_universe_digest`` must be a function of *that tuple only* -- these
tests pin both properties plus the stable rejection codes.
"""

from __future__ import annotations

import pytest

from app.services.backtest.run_universe import (
    RUN_UNIVERSE_VERSION,
    RunUniverseError,
    RunUniverseErrorCode,
    canonical_run_universe,
    run_universe_digest,
)


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def test_canonical_run_universe_sorts_and_deduplicates() -> None:
    assert canonical_run_universe(["sec-msft", "sec-aapl", "sec-msft", "sec-goog"]) == (
        "sec-aapl",
        "sec-goog",
        "sec-msft",
    )


def test_canonical_run_universe_is_an_immutable_tuple() -> None:
    canonical = canonical_run_universe(["sec-aapl"])

    assert isinstance(canonical, tuple)
    with pytest.raises(TypeError):
        canonical[0] = "sec-msft"  # type: ignore[index]


def test_canonical_run_universe_accepts_any_size_with_no_maximum() -> None:
    """No per-Run security-count maximum exists -- a large selection
    canonicalizes exactly like a small one."""
    selection = [f"sec-{index:04d}" for index in range(500)]

    assert canonical_run_universe(reversed(selection)) == tuple(selection)


def test_canonical_run_universe_rejects_an_empty_selection() -> None:
    with pytest.raises(RunUniverseError) as exc_info:
        canonical_run_universe([])

    assert exc_info.value.code is RunUniverseErrorCode.EMPTY_UNIVERSE


@pytest.mark.parametrize(
    "selection",
    (
        ["sec-aapl", ""],
        ["sec-aapl", " sec-msft"],
        ["sec-aapl", "sec-msft "],
        ["sec-aapl", None],
        ["sec-aapl", 7],
        [("sec-aapl",)],
    ),
)
def test_canonical_run_universe_rejects_a_malformed_security_id(
    selection: list[object],
) -> None:
    with pytest.raises(RunUniverseError) as exc_info:
        canonical_run_universe(selection)

    assert exc_info.value.code is RunUniverseErrorCode.INVALID_SECURITY_ID


# ---------------------------------------------------------------------------
# Digest identity
# ---------------------------------------------------------------------------


def test_run_universe_digest_is_identical_across_selection_orders() -> None:
    """The I/O matrix's "duplicate/unsorted universe" row: two UI selection
    orders of the same set, one repeating an ID, share one digest."""
    first = run_universe_digest(["sec-msft", "sec-aapl", "sec-goog"])
    second = run_universe_digest(["sec-goog", "sec-msft", "sec-aapl", "sec-msft"])

    assert first == second
    assert len(first) == 64


def test_run_universe_digest_changes_with_the_selected_set() -> None:
    assert run_universe_digest(["sec-aapl"]) != run_universe_digest(
        ["sec-aapl", "sec-msft"]
    )


def test_run_universe_digest_is_versioned() -> None:
    assert RUN_UNIVERSE_VERSION == "run_universe.v1"


def test_run_universe_digest_rejects_an_empty_selection() -> None:
    with pytest.raises(RunUniverseError) as exc_info:
        run_universe_digest([])

    assert exc_info.value.code is RunUniverseErrorCode.EMPTY_UNIVERSE
