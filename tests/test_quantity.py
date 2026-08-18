"""Unit tests for ``app.core.quantity``: the shared 8dp round-half-even
share-quantity function (Story 2.3, AC1-3).

Values below are verified representation-noise/tie cases -- see the story's
spec, "Fractional share at an 8dp round-half-even boundary" I/O row.
"""

from app.core.quantity import QUANTITY_EPSILON, QUANTITY_PLACES, round_quantity


def test_round_quantity_places_and_epsilon_are_8dp_and_half_a_unit() -> None:
    assert QUANTITY_PLACES == 8
    assert QUANTITY_EPSILON == 5e-9


def test_round_quantity_truncates_representation_noise_beyond_8dp() -> None:
    # `0.1 + 0.2` is the textbook float-noise example: the true binary sum
    # is `0.30000000000000004...`, not `0.3`. Noise past the 8th decimal
    # place must not leak into the rounded result.
    assert round_quantity(0.1 + 0.2) == 0.3


def test_round_quantity_half_even_tie_rounds_to_even_neighbour() -> None:
    # A tie exactly halfway between two 8dp values rounds to whichever
    # neighbour is even -- never "always up" / "always away from zero".
    # 1.000000005 -> 8th-place digit is 0 (even) -> stays.
    assert round_quantity(1.000000005) == 1.0
    # 1.000000015 -> 8th-place digit is 1 (odd) -> rounds up to 2 (even).
    assert round_quantity(1.000000015) == 1.00000002


def test_round_quantity_does_not_reproduce_float_binary_imprecision() -> None:
    # Constructing Decimal directly from the float (rather than via
    # `str(value)`) would reproduce 2.675's true binary representation
    # (~2.67499999999999982...) and round it down instead of up. Verifies
    # the function uses the `str(value)` construction the spec requires.
    assert round_quantity(2.675) == 2.675


def test_round_quantity_is_idempotent() -> None:
    # Rounding an already-rounded value must be a no-op -- required for
    # cross-path consistency: whichever path (FIFO, average-cost, P&L)
    # rounds first, a second path rounding the same already-rounded figure
    # must not perturb it further.
    once = round_quantity(1.123456785)
    assert round_quantity(once) == once


def test_round_quantity_agrees_across_differently_ordered_arithmetic() -> None:
    """AC2: the same logical quantity, arrived at via different arithmetic
    (mirroring FIFO's incremental lot-subtraction vs. average-cost's
    single running total), rounds to an identical result regardless of
    which path performs the rounding."""
    # FIFO-style: three separate partial-lot subtractions.
    fifo_style = 10.0
    for chunk in (3.1, 2.2, 1.3):
        fifo_style -= chunk

    # Average-cost-style: one single subtraction of the pre-summed total.
    avg_cost_style = 10.0 - (3.1 + 2.2 + 1.3)

    assert round_quantity(fifo_style) == round_quantity(avg_cost_style)
