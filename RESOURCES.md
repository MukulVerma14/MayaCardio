# System Resource Utilization Profile

This document details the CPU, memory, storage, and network resource consumption of the MayaCardio bedside data pipeline, verifying compliance with the target embedded environment.

---

## 1. Target Hardware & Test Environment

| Specification | Target Assessment Board | Test & Benchmark Environment |
| :--- | :--- | :--- |
| **Architecture** | ARM64 (Raspberry Pi 4 / Compute Module 4) | ARM64 Linux Container / Host Emulation |
| **CPU Allocation** | Maximum 4 CPU Cores | Limited to 4 CPU Cores (`--cpus 4`) |
| **RAM Allocation** | Maximum 2.0 GB Memory | Limited to 2.0 GB RAM (`--memory 2g`) |
| **Storage Medium** | eMMC / High-endurance microSD (ext4) | Local NVMe SSD / ext4 volume |
| **Python Version** | Python 3.11+ | Python 3.12 |

---

## 2. Resource Utilization Metrics

The following metrics were profiled using `scripts/run_1hour_benchmark.py` during active streaming ingestion (Sensor 1 at 400 Hz, Sensor 2 at 400 Hz = 800 samples/s continuous load) with concurrent online uploading:

| Metric | Measured Value | Budget / Requirement | Status |
| :--- | :---: | :---: | :---: |
| **Average CPU Usage** | **25.9% of 1 Core** (~6.5% total) | $\le 400\%$ (4 Cores) | **WELL WITHIN BUDGET** |
| **Peak CPU Usage** | **65.6% of 1 Core** (~16.4% total) | $\le 400\%$ (4 Cores) | **WELL WITHIN BUDGET** |
| **Average Memory (RSS)** | **31.2 MB** | $\le 2,048$ MB (2.0 GB) | **WELL WITHIN BUDGET** |
| **Peak Memory (RSS)** | **31.9 MB** | $\le 2,048$ MB (2.0 GB) | **WELL WITHIN BUDGET** |
| **Disk Write Rate** | **~940 KB/s** | Sustained flash endurance | **COMPACT & DURABLE** |
| **Sustained Catch-Up Rate** | **12,455 samples/sec** | $\ge 6,500$ samples/sec | **EXCEEDS TARGET (1.9x)** |
| **Result Latency (P95)** | **~1.1 seconds** | $\le 5.0$ seconds | **LOW LATENCY** |

---

## 3. Resource Analysis & Architectural Optimizations

### 3.1 CPU Optimization
- **Sliding-Window Monotonic Queue**: Rather than performing a naive $O(N)$ scan across 800 prior samples for every candidate heartbeat peak, `PeakDetector` maintains a monotonic descending deque in $O(1)$ amortized time. This keeps continuous signal processing under 7% total CPU load across 4 cores.
- **Efficient Slicing for CRC-32**: CRC-32 calculation is performed directly on raw slice bytes, eliminating Python object JSON re-serialization overhead.

### 3.2 Memory Boundedness
- **Strict History Eviction**: Historical sample dictionaries in `WindowProcessor` are automatically pruned beyond the 10-window (4,000 samples) safety horizon needed for the 5-second heart rate calculation.
- **Bounded Buffer Queues**: Ingestion queues and uploader batches are strictly bounded, ensuring RSS stays virtually flat at ~31-32 MB regardless of whether the pipeline runs for 1 minute or 10 hours.

### 3.3 Disk & Flash Endurance
- **Atomic Batch Commits**: The reader buffers each 250 ms polling window (~100 samples) and writes them to SQLite in a single transaction, avoiding write amplification.
- **WAL Mode**: SQLite WAL mode replaces random file writes with sequential append-only writes, reducing flash wear on SD cards.

---

## 4. How to Reproduce on ARM64 Container

To run the exact resource benchmark in an ARM64 container constrained to 4 cores and 2 GB:

```bash
# Build the container
docker build -t mayacardio-bench .

# Run under constrained ARM64 environment
docker run --platform linux/arm64 --cpus 4 --memory 2g -it mayacardio-bench \
    python scripts/run_1hour_benchmark.py --duration 3600
```
