# System Resource Utilization Profile & Audit

This document details the measured and projected resource footprint (CPU, memory, storage I/O, network) of the MayaCardio bedside data pipeline.

---

## 1. Test Environment & Audit Disclosure

In strict accordance with reporting integrity:
- **Actual Measurement Host**: **Windows 10/11 x86_64 (AMD64), Python 3.12.3**
- **Target Assessment Board**: **Raspberry Pi 4 / CM4 (ARM64, max 4 cores, max 2 GB RAM)**
- **Actual Benchmark Duration**: **10 seconds** (`scripts/run_1hour_benchmark.py --duration 10`)
- **Full 1-Hour Run on Physical ARM64**: **Not run** (`[PROJECTED / UNVERIFIED ON PHYSICAL ARM64]`)

---

## 2. Resource Utilization Metrics

The table below distinguishes empirical figures measured during the 10-second streaming benchmark (Sensor 1 at 400 Hz, Sensor 2 at 400 Hz = 800 samples/s continuous load) from projected 1-hour figures:

| Metric | Measured Value (10s Run on x86_64) | Projected 1-Hour Value | Target Budget (ARM64 / 2GB / 4 Cores) | Status |
| :--- | :---: | :---: | :---: | :---: |
| **Average CPU** | **25.9% of 1 Core** `[MEASURED]` | ~25–35% of 1 Core `[PROJECTED]` | $\le 400\%$ (4 Cores) | **Within Budget** |
| **Peak CPU** | **65.6% of 1 Core** `[MEASURED]` | ~70–80% of 1 Core `[PROJECTED]` | $\le 400\%$ (4 Cores) | **Within Budget** |
| **Average Memory (RSS)** | **31.2 MB** `[MEASURED]` | ~32–40 MB `[PROJECTED]` | $\le 2,048$ MB (2.0 GB) | **Within Budget** |
| **Peak Memory (RSS)** | **31.85 MB** `[MEASURED]` | ~35–45 MB `[PROJECTED]` | $\le 2,048$ MB (2.0 GB) | **Within Budget** |
| **Disk Write Rate** | **~941 KB/s** `[MEASURED]` | ~900–950 KB/s `[PROJECTED]` | Sequential WAL append | **Within Budget** |
| **1-Hour Total Disk Write** | 9.19 MB in 10s `[MEASURED]` | ~3.3 GB per hour `[PROJECTED]` | eMMC / high-endurance SD | **Projected** |
| **Catch-Up Throughput** | **12,455.7 samples/s** `[MEASURED]` | N/A (burst mode) | $\ge 6,500$ samples/s | **Exceeds Target (1.9x)** |
| **Result Latency (P95)** | **~1.1 seconds** `[MEASURED]` | ~1.1–1.5 seconds `[PROJECTED]` | $\le 5.0$ seconds | **Within Budget** |

---

## 3. Resource Analysis & Architectural Safeguards

### 3.1 CPU Optimization
- **Sliding-Window Monotonic Queue**: The `PeakDetector` implements an $O(1)$ amortized sliding-window maximum tracker (`MonotonicMaxDeque`) over the 800-sample lookback window, avoiding $O(N)$ repeated iterations.
- **Raw Byte Slicing**: Verification of CRC-32 directly on the raw received bytes eliminates JSON parsing and serialization overhead during CRC checks.

### 3.2 Bounded Memory Model
- **History Pruning**: `WindowProcessor` retains only samples within the active 10-window (4,000 samples) horizon required for window calculation and heart-rate peak lookback. Older samples are pruned from memory after finalization.
- **Flat Memory Profile**: As observed in the 10-second test, RSS memory remained flat at ~31.2–31.85 MB without memory leaks.

### 3.3 Disk & Flash Endurance
- **Atomic Batch Commits**: Rather than writing each sample individually, the reader commits an entire 250 ms reply batch (~100 samples) in a single SQLite transaction with `fsync`, minimizing flash write amplification.
- **WAL Sequential Appends**: SQLite WAL mode replaces random sector writes with sequential append-only writes.

---

## 4. How to Execute the 1-Hour Benchmark on ARM64 Hardware

To run the complete 3,600-second benchmark on physical ARM64 hardware or an ARM64 container:

### On Physical Raspberry Pi (ARM64):
```bash
python scripts/run_1hour_benchmark.py --duration 3600 --mode signal
```

### In Docker on ARM64:
```bash
docker run --platform linux/arm64 --cpus 4 --memory 2g -v $(pwd):/app -w /app python:3.12-slim \
    bash -c "pip install . && python scripts/run_1hour_benchmark.py --duration 3600"
```
