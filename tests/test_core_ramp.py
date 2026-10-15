"""Unit tests for core processing under Ramp Mode."""

import math
from core.rounding import round_metric
from core.window_processor import WindowProcessor, SAMPLES_PER_SECOND

MOD = 8388608


def generate_ramp_sample(sensor: int, idx: int) -> list[int]:
    if sensor == 1:
        return [(3 * idx + c) % MOD for c in range(3)]
    else:
        return [(2 * idx + c) % MOD for c in range(2)]


def test_core_ramp_window_0() -> None:
    processor = WindowProcessor(session_id="test_ramp_0", mode="online")

    # Feed window 0 (samples 0..399) + lookahead sample 400 for sensor 1
    for i in range(401):
        processor.add_sample(1, i, generate_ramp_sample(1, i))
    for i in range(400):
        processor.add_sample(2, i, generate_ramp_sample(2, i))

    results = processor.finalize_available_windows()
    assert len(results) == 1
    res = results[0]

    assert res.window == 0
    assert res.complete is True
    assert res.missing_1 == 0
    assert res.missing_2 == 0
    # Window 0 has w < 4, so heart_rate must be None
    assert res.heart_rate is None

    # Analytical verification for Sensor 1 Channel 0: (3i + 0) for i in 0..399
    s1c1_vals = [3 * i for i in range(400)]
    assert res.min["s1c1"] == float(min(s1c1_vals))
    assert res.max["s1c1"] == float(max(s1c1_vals))
    assert res.mean["s1c1"] == round_metric(sum(s1c1_vals) / len(s1c1_vals))

    # Analytical verification for Sensor 2 Channel 0 and 1
    s2c1_vals = [2 * i for i in range(400)]
    s2c2_vals = [2 * i + 1 for i in range(400)]
    assert res.min["s2c1"] == float(min(s2c1_vals))
    assert res.max["s2c1"] == float(max(s2c1_vals))
    assert res.mean["s2c1"] == round_metric(sum(s2c1_vals) / len(s2c1_vals))

    mags = [math.sqrt(s2c1_vals[i] ** 2 + s2c2_vals[i] ** 2) for i in range(400)]
    expected_mag = round_metric(sum(mags) / len(mags))
    assert res.magnitude == expected_mag


def test_core_ramp_window_with_missing_samples() -> None:
    processor = WindowProcessor(session_id="test_ramp_missing", mode="offline")

    # Omit samples 100..109 for sensor 2 (10 samples missing)
    for i in range(401):
        processor.add_sample(1, i, generate_ramp_sample(1, i))

    for i in range(400):
        if 100 <= i < 110:
            continue
        processor.add_sample(2, i, generate_ramp_sample(2, i))

    # Mark 100..109 as lost for sensor 2
    processor.add_loss_range(2, 100, 109)

    results = processor.finalize_available_windows()
    assert len(results) == 1
    res = results[0]

    assert res.complete is False
    assert res.missing_1 == 0
    assert res.missing_2 == 10
    assert res.mode == "offline"

    # Sensor 2 means should be computed over the 390 available samples
    available_s2c1 = [2 * i for i in range(400) if not (100 <= i < 110)]
    assert len(available_s2c1) == 390
    assert res.mean["s2c1"] == round_metric(sum(available_s2c1) / 390)
