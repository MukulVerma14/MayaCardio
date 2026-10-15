"""Property tests proving offline (device) and online (cloud) parity.

In accordance with Section 3 of the specification:
"The offline (device) and online (cloud) paths must produce byte-identical
numbers for the same data. Write property tests proving it (random gaps,
random batch splits)."
"""

import json
import random
import pytest
from core.window_processor import WindowProcessor, SAMPLES_PER_SECOND
from tests.test_core_signal import generate_synthetic_ecg_signal


def test_device_cloud_parity_random_splits_and_gaps() -> None:
    random.seed(42)

    total_samples = 4000  # 10 windows
    ecg = generate_synthetic_ecg_signal(num_samples=total_samples, bpm=60.0)

    # Prepare complete ground truth session data
    s1_data = {
        i: [int(ecg[i] * 0.6), ecg[i], int(ecg[i] * 1.3)]
        for i in range(total_samples)
    }
    s2_data = {
        i: [400000 + (i % 100), 150000 + (i % 50)]
        for i in range(total_samples)
    }

    # Inject random gaps
    # Sensor 1 gap at 500..550
    # Sensor 2 gap at 1200..1280
    gaps = [
        (1, 500, 550),
        (2, 1200, 1280),
        (1, 2500, 2520),
    ]

    for sensor, g_start, g_end in gaps:
        target = s1_data if sensor == 1 else s2_data
        for idx in range(g_start, g_end + 1):
            target.pop(idx, None)

    # Initialize two processors: one offline, one online
    proc_offline = WindowProcessor(session_id="parity_test", mode="offline")
    proc_online = WindowProcessor(session_id="parity_test", mode="online")

    # Add loss ranges to both
    for sensor, g_start, g_end in gaps:
        proc_offline.add_loss_range(sensor, g_start, g_end)
        proc_online.add_loss_range(sensor, g_start, g_end)

    # Processor 1 (offline): Feed in small chunks of 17 to 89 samples
    idx = 0
    while idx < total_samples:
        chunk_size = random.randint(17, 89)
        chunk_end = min(total_samples, idx + chunk_size)
        for i in range(idx, chunk_end):
            if i in s1_data:
                proc_offline.add_sample(1, i, s1_data[i])
            if i in s2_data:
                proc_offline.add_sample(2, i, s2_data[i])
        proc_offline.finalize_available_windows()
        idx = chunk_end

    # Processor 2 (online): Feed in different chunk sizes of 150 to 500 samples
    idx = 0
    while idx < total_samples:
        chunk_size = random.randint(150, 500)
        chunk_end = min(total_samples, idx + chunk_size)
        for i in range(idx, chunk_end):
            if i in s1_data:
                proc_online.add_sample(1, i, s1_data[i])
            if i in s2_data:
                proc_online.add_sample(2, i, s2_data[i])
        proc_online.finalize_available_windows()
        idx = chunk_end

    # Finalize remaining windows
    proc_offline.finalize_available_windows()
    proc_online.finalize_available_windows()

    results_offline = proc_offline.finalized_results
    results_online = proc_online.finalized_results

    assert len(results_offline) > 0
    assert len(results_offline) == len(results_online)

    # Verify byte-identical numbers across all windows
    for r_off, r_on in zip(results_offline, results_online):
        assert r_off.window == r_on.window
        assert r_off.missing_1 == r_on.missing_1
        assert r_off.missing_2 == r_on.missing_2
        assert r_off.complete == r_on.complete
        assert r_off.mean == r_on.mean
        assert r_off.min == r_on.min
        assert r_off.max == r_on.max
        assert r_off.magnitude == r_on.magnitude
        assert r_off.heart_rate == r_on.heart_rate

        # Compare serialized numeric outputs (excluding mode and processed_at)
        dict_off = {k: v for k, v in r_off.to_dict().items() if k not in ("mode", "processed_at")}
        dict_on = {k: v for k, v in r_on.to_dict().items() if k not in ("mode", "processed_at")}

        json_off = json.dumps(dict_off, sort_keys=True)
        json_on = json.dumps(dict_on, sort_keys=True)

        assert json_off == json_on
