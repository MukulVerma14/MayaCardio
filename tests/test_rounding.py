"""Unit tests for deterministic rounding helper."""

from decimal import Decimal
import pytest
from core.rounding import round_metric


def test_round_metric_half_up() -> None:
    # Round half up: .x5 rounds away from zero
    assert round_metric(1.25) == 1.3
    assert round_metric(1.35) == 1.4
    assert round_metric(1.45) == 1.5
    assert round_metric(2.55) == 2.6
    assert round_metric(1.24) == 1.2
    assert round_metric(1.26) == 1.3


def test_round_metric_negatives() -> None:
    assert round_metric(-1.25) == -1.3
    assert round_metric(-1.24) == -1.2
    assert round_metric(-1.35) == -1.4


def test_round_metric_none_and_zero() -> None:
    assert round_metric(None) is None
    assert round_metric(0.0) == 0.0
    assert round_metric(0) == 0.0
    assert round_metric(-0.0) == 0.0


def test_round_metric_types() -> None:
    assert round_metric(10) == 10.0
    assert round_metric("12.34") == 12.3
    assert round_metric(Decimal("12.35")) == 12.4
