"""Unit tests for core processing under Signal Mode (Heart Rate calculation)."""

import pytest
from core.window_processor import WindowProcessor, SAMPLES_PER_SECOND


def generate_synthetic_ecg_signal(
    num_samples: int,
    bpm: float,
    baseline: int = 200,
    peak_val: int = 1500,
) -> list[int]:
    """Generate a clean synthetic ECG-like signal with sharp peaks at a fixed BPM."""
    signal = [baseline] * num_samples
    gap_samples = int(round(SAMPLES_PER_SECOND * 60.0 / bpm))

    # Plant peaks at regular intervals
    current_idx = gap_samples
    while current_idx < num_samples:
        # Sharp QRS peak: rise, peak, fall
        if current_idx - 1 >= 0:
            signal[current_idx - 1] = int(baseline + (peak_val - baseline) * 0.4)
        signal[current_idx] = peak_val
        if current_idx + 1 < num_samples:
            signal[current_idx + 1] = int(baseline + (peak_val - baseline) * 0.3)
        current_idx += gap_samples

    return signal


def test_heart_rate_null_before_window_4() -> None:
    processor = WindowProcessor(session_id="test_hr_early", mode="online")
    ecg = generate_synthetic_ecg_signal(num_samples=1601, bpm=60.0)

    # Feed windows 0, 1, 2, 3 (each 400 samples)
    for i in range(1601):
        processor.add_sample(1, i, [int(ecg[i] * 0.6), ecg[i], int(ecg[i] * 1.3)])
        if i < 1600:
            processor.add_sample(2, i, [400000, 150000])

    results = processor.finalize_available_windows()
    assert len(results) == 4
    for r in results:
        # For w in 0, 1, 2, 3: heart_rate must be null
        assert r.heart_rate is None


def test_heart_rate_60_bpm() -> None:
    """Test 60 BPM signal produces exactly 60.0 bpm once window 4+ is finalized."""
    num_samples = 3000
    processor = WindowProcessor(session_id="test_hr_60", mode="online")
    ecg = generate_synthetic_ecg_signal(num_samples=num_samples, bpm=60.0)

    for i in range(num_samples):
        processor.add_sample(1, i, [int(ecg[i] * 0.6), ecg[i], int(ecg[i] * 1.3)])
        processor.add_sample(2, i, [400000, 150000])

    results = processor.finalize_available_windows()
    # Should finalize windows 0, 1, 2, 3, 4, 5, 6
    assert len(results) >= 5

    # Check window 4 (samples 1600..1999, lookahead sample 2000)
    w4 = results[4]
    assert w4.window == 4
    assert w4.complete is True
    # At 60 bpm with 400 samples/s, gap is exactly 400 samples (1.0 sec)
    assert w4.heart_rate == 60.0


def test_heart_rate_90_bpm() -> None:
    """Test 90 BPM signal produces ~90.0 bpm."""
    num_samples = 3000
    processor = WindowProcessor(session_id="test_hr_90", mode="online")
    ecg = generate_synthetic_ecg_signal(num_samples=num_samples, bpm=90.0)

    for i in range(num_samples):
        processor.add_sample(1, i, [int(ecg[i] * 0.6), ecg[i], int(ecg[i] * 1.3)])
        processor.add_sample(2, i, [400000, 150000])

    results = processor.finalize_available_windows()
    w4 = results[4]
    assert w4.window == 4
    # 90 bpm gap is 267 samples -> 24000 / 267 = 89.9 or 90.0
    assert w4.heart_rate in (89.9, 90.0)


def test_heart_rate_72_bpm() -> None:
    """Test 72 BPM signal produces 72.0 bpm."""
    num_samples = 3000
    processor = WindowProcessor(session_id="test_hr_72", mode="online")
    ecg = generate_synthetic_ecg_signal(num_samples=num_samples, bpm=72.0)

    for i in range(num_samples):
        processor.add_sample(1, i, [int(ecg[i] * 0.6), ecg[i], int(ecg[i] * 1.3)])
        processor.add_sample(2, i, [400000, 150000])

    results = processor.finalize_available_windows()
    w4 = results[4]
    assert w4.window == 4
    # 72 bpm gap is 333 samples -> 24000 / 333 = 72.1, or 333.33 -> 72.0
    assert abs(w4.heart_rate - 72.0) <= 0.2


def test_heart_rate_null_when_samples_missing_in_5s_window() -> None:
    """heart_rate is null if any sample in the 5-second window is missing."""
    num_samples = 3000
    processor = WindowProcessor(session_id="test_hr_missing", mode="online")
    ecg = generate_synthetic_ecg_signal(num_samples=num_samples, bpm=60.0)

    for i in range(num_samples):
        if i != 1000:
            processor.add_sample(1, i, [int(ecg[i] * 0.6), ecg[i], int(ecg[i] * 1.3)])
        processor.add_sample(2, i, [400000, 150000])

    processor.add_loss_range(1, 1000, 1000)

    results = processor.finalize_available_windows()
    assert len(results) >= 5
    w4 = results[4]
    # Sample 1000 missing in [0..1999] -> heart_rate MUST be None
    assert w4.heart_rate is None
