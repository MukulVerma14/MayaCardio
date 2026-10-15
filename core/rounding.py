"""Deterministic rounding helper using Decimal and ROUND_HALF_UP.

In accordance with Section 3 of the specification:
"Round decimals to 1 decimal place (use one deterministic rounding helper
everywhere; document which rounding mode, and prefer round-half-up via Decimal
over Python's banker's rounding)."
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Union

ONE_TENTH = Decimal("0.1")


def round_metric(value: Optional[Union[float, int, Decimal, str]]) -> Optional[float]:
    """Round a number to 1 decimal place using deterministic ROUND_HALF_UP.

    Examples:
        round_metric(1.24) -> 1.2
        round_metric(1.25) -> 1.3
        round_metric(1.26) -> 1.3
        round_metric(-1.25) -> -1.3
        round_metric(None) -> None

    Returns:
        Rounded float or None if input was None.
    """
    if value is None:
        return None

    if isinstance(value, (int, str)):
        d = Decimal(str(value))
    elif isinstance(value, Decimal):
        d = value
    elif isinstance(value, float):
        # Using string conversion produces shortest accurate decimal representation in Python 3.11+
        # If float is nan or inf, return None or standard float
        if value != value:  # NaN check
            return None
        d = Decimal(str(value))
    else:
        d = Decimal(str(value))

    quantized = d.quantize(ONE_TENTH, rounding=ROUND_HALF_UP)
    result = float(quantized)
    # Prevent negative zero (-0.0) from appearing
    return 0.0 if result == 0.0 else result
