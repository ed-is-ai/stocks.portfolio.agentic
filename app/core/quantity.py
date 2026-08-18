"""Shared share-quantity rounding (Story 2.3).

Dependency-free (stdlib ``decimal`` only), mirroring ``app/core/money.py``'s
AD-2 shape: a small, reusable primitive that lives in ``app/core`` so no
layer above it needs a reverse dependency to share this policy. Share
quantities are a distinct typed fact from money amounts (AD-24) -- this
module intentionally does NOT extend or reuse ``Money``, and must never be
applied to monetary/price fields (``total_cost``, ``current_value``,
``unrealised_pnl``, ``current_price``, ``profit_target_20``/``_25``), which
stay 2dp and out of this module's scope.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

#: Decimal places every share quantity is rounded/compared at -- the one
#: shared precision policy FIFO matching, average-cost replay, and realised
#: P&L all apply identically (Story 2.3 AC1/AC2/AC3).
QUANTITY_PLACES = 8
_QUANTIZE_EXP = Decimal("1e-8")

#: Half of the smallest representable unit at ``QUANTITY_PLACES`` -- the
#: shared near-equality tolerance for "is this effectively zero/equal"
#: checks (lot-consumption exhaustion, FIFO-vs-avg-cost mismatch), replacing
#: each call site's previously independent, ad hoc epsilon (e.g. ``1e-6``).
QUANTITY_EPSILON = 5 * 10 ** -(QUANTITY_PLACES + 1)


def round_quantity(value: float) -> float:
    """Round a share quantity to 8dp using Decimal round-half-even.

    Constructs the ``Decimal`` from ``str(value)``, never ``Decimal(value)``
    directly on a float -- the latter reproduces the float's true binary
    imprecision (e.g. ``Decimal(0.1) != Decimal("0.1")``), defeating the
    purpose of rounding at all.

    Apply this everywhere a share quantity is rounded or compared for
    near-equality, so the same float rounds to the same result no matter
    which code path (FIFO matching, average-cost replay, realised P&L)
    performs it. Never apply it to a monetary/price field -- see the module
    docstring.
    """
    quantized = Decimal(str(value)).quantize(_QUANTIZE_EXP, rounding=ROUND_HALF_EVEN)
    return float(quantized)
