# System Architecture & Design Specification: MayaCardio Pipeline

This document details the architectural principles, subsystem designs, mathematical invariants, resilience boundaries, and deployment specifications for the MayaCardio bedside biosignal pipeline.

---

## 1. System Context & Architecture Diagram

```mermaid
flowchart TD
    subgraph Edge ["Bedside Edge Device (Target: Raspberry Pi ARM64)"]
        subgraph Acquisition ["Acquisition & Ingestion Subsystem"]
            Mock["biosignal-mock Daemon\n(TCP 7100, 400 Hz)"]
            Reader["Reader Thread\n(Non-blocking framing, CRC-32)"]
            LossDetect["Gap & Buffer Overflow\nDetection Engine"]
            Mock -->|Stream & CRC| Reader
            Reader --> LossDetect
        end

        subgraph LocalStore ["Crash-Resilient Storage Subsystem"]
            DB[("SQLite 3 (WAL Mode)\nPRAGMA synchronous = FULL\nSingle-writer serial queue")]
            ReserveFile[(".emergency_reserve\n(512 KB ENOSPC reserve)")]
            Reader -->|Atomic Batch Commit| DB
            LossDetect -->|Loss Records Commit| DB
            ReserveFile -.->|Release on ENOSPC| DB
        end

        subgraph EdgeProc ["Edge Analytics & State Control"]
            ConnMon["Connectivity Monitor\n(Heartbeat Probe Loop)"]
            OfflineProc["Edge Window Processor\n(Shared Core Logic)"]
            DB -->|Read Unprocessed| OfflineProc
            OfflineProc -->|Store Offline Results| DB
            ConnMon -->|Persist Mode Changes| DB
        end

        subgraph Dispatch ["Outbound Synchronization"]
            Uploader["Dual-Channel Uploader\n(Live Batches + Backlog Drain)"]
            DB -->|Read Unacked Samples| Uploader
        end
    end

    subgraph RemoteCloud ["Free Cloud Backend (Render.com + Neon PostgreSQL, tested deployment)"]
        IngestAPI["FastAPI Ingestion Endpoint\n(/api/v1/ingest)"]
        CloudProc["Cloud Window Processor\n(Shared Core Logic)"]
        CloudDB[("Neon Serverless PostgreSQL\nPRIMARY KEY (session_id, sensor, idx)\nON CONFLICT DO NOTHING")]
        
        Uploader -->|HTTP/1.1 POST Gzip| IngestAPI
        ConnMon -->|2.0s Probe /api/v1/heartbeat| IngestAPI
        IngestAPI --> CloudProc
        CloudProc --> CloudDB
    end

    classDef edgeStyle fill:#e8f4f8,stroke:#2b6cb0,stroke-width:2px;
    classDef cloudStyle fill:#edf2f7,stroke:#4a5568,stroke-width:2px;
    class Edge edgeStyle;
    class RemoteCloud cloudStyle;
```

---

## 2. Core Subsystems & Mechanisms

### 2.1 Biosignal Reader & Protocol Parser ([`reader/`](reader/))
- **Framing & Protocol**: Handles newline-delimited ASCII lines (`"s1,idx,v1,v2,v3,crc"` / `"s2,idx,v1,v2,crc"`).
- **Integrity Verification**: Verifies 32-bit CRC against IEEE 802.3 standard on every incoming sample frame.
- **Session State & Gap Handling**:
  - The reader detects session ID transitions in the mock response stream.
  - When a new session is detected, all sensor cursors are reset immediately to 0.
  - If a sensor's initial read in a session starts with `first_index > 0`, the reader records `LossRecord(sensor, 0, first_index - 1, reason="not_read_before_pipeline_start")`.
  - Buffer overflow losses detected by the mock (`loss,from_idx,to_idx,reason`) are committed to SQLite.
- **Zero Cloud Coupling**: The reader never makes network calls to the cloud and cannot be blocked by cloud latency or network disconnects.

### 2.2 Durable Local Storage ([`storage/`](storage/))
- **Storage Engine**: SQLite in Write-Ahead Logging (WAL) mode with `PRAGMA synchronous = FULL`. Every reply batch (samples + loss records + cursor) is committed in a single atomic transaction with a filesystem sync.
- **Conflicting Duplicate Rejection**: Duplicate primary keys `(session_id, sensor, idx)` with matching values are ignored (idempotent); conflicting values are rejected and logged with `[ERROR]`.
- **Disk Full (ENOSPC) Allowance**: A 512 KB emergency reserve file (`.emergency_reserve`) is maintained on disk. When `sqlite3.OperationalError: database or disk is full` is encountered, the reserve file is released, allowing emergency loss records (`reason: 'disk_full'`) to be committed without crashing silently `[MEASURED via test_disk_full.py]`.
- **Session-Scoped Status Calculation**: Status metrics calculate exact physical row counts `WHERE session_id = ? AND sensor = ? AND idx <= last_acked_idx`, ensuring `samples_in_cloud <= samples_saved` and `backlog == samples_saved - samples_in_cloud` regardless of initial loss ranges or non-zero start indices.

### 2.3 Shared Core Processing ([`core/`](core/))
- **Single Shared Module**: Exactly identical code is executed by the device (for offline processing) and the cloud service (for online streaming ingestion).
- **Peak Detection**: Stateful across the entire session stream using a monotonic maximum tracker over the preceding 800 samples ($O(1)$ amortized time).
- **Lookahead Finalization**: Window $w$ ($400w \dots 400w+399$) requires lookahead sample $400(w+1)$ of Sensor 1 to evaluate whether the final sample of the window is a peak.
- **Deterministic Rounding**: All decimal outputs use `Decimal` with `ROUND_HALF_UP` quantized to `Decimal('0.1')`.

### 2.4 Connectivity Monitor & Mode Switching ([`connectivity/`](connectivity/))
- Probes `/api/v1/heartbeat` every 2.0s using monotonic clock intervals with a 1.5s timeout.
- **Hysteresis**: 3 consecutive failed probes switch mode to `offline` (measured: **6.06s** in local-simulated test, theoretical limit $\le 6.0\text{s} + \text{timeout}$); 2 consecutive successes switch mode to `online` (measured: **4.55s** in local-simulated test, theoretical limit $\le 4.0\text{s}$). Both are well within the $\le 10\text{s}$ requirement. Mode state is persisted to SQLite.

### 2.5 Dual-Channel Uploader & Catch-Up ([`uploader/`](uploader/))
- **Prioritization**: When online, live batches are transmitted every 0.5s to ensure results appear in the cloud within 5s (measured P95 latency: **~1.1s** on localhost).
- **Throughput & Backlog Drain**: Backlog is uploaded in batches of up to 2,500 samples with Gzip compression.
  - **Localhost**: Sustained throughput of **12,455.7 samples/s** `[MEASURED]`, allowing a 2-hour outage (5.76M samples) to drain in **~7.7 minutes** (requirement met on localhost).
  - **Deployed Render (Public Internet)**: Upload throughput measured at **1,688.6 samples/s** (sequential, 1 worker) to **2,533.7 samples/s** (parallel, 4 workers) `[MEASURED]`. Catching up a 2-hour backlog over the public internet requires projected **~37.9 to 56.9 minutes**, which does **NOT** meet the 15-minute requirement on this deployment.
- **Ack-Based Advancing**: Device cursors advance only upon receiving explicit acknowledgement from the cloud ingestion endpoint.

### 2.6 Free Cloud Tier Architecture (Render + Neon)
- **Platform**: Render.com (Free Web Service) + Neon.tech (Free Serverless PostgreSQL). Both provide free tiers (per provider documentation, verify at signup).
- **Schema & SQL Portability**: Double-quoted identifier `"window"` ensures full compatibility with PostgreSQL reserved keywords while preserving SQLite compatibility.
- **Duplicate-Freeness Guarantee**: Guaranteed by `PRIMARY KEY (session_id, sensor, idx)` with `ON CONFLICT (session_id, sensor, idx) DO NOTHING`.
- **Cold-Start Mitigation & Keepalive (design intent, not tested)**: Render free web services spin down after 15 minutes of inactivity (cold start ~50s). In this architecture, the bedside device transmits a lightweight `/api/v1/heartbeat` probe every **2.0 seconds** while online. Continuous multi-hour keepalive effectiveness was not empirically measured.

---

## 3. What is Guaranteed and What is NOT

### Guaranteed:
1. **Zero Silent Loss of Received Data**: Every sample received by the device is safely committed to local disk before acknowledging the read cursor `[MEASURED]`.
2. **Honest Accounting**: Every gap caused by buffer overflow, daemon termination, network delay, mock restart, or initial unread data is explicitly logged with exact index ranges and reasons `[MEASURED]`.
3. **Exact Mathematical Parity**: Offline results computed on the device and online results computed in the cloud are byte-identical for identical inputs `[MEASURED via test_parity_property.py]`.
4. **Crash & Kill Resumption**: Hard process termination (`kill -9`) causes zero duplicate entries and zero DB corruption; the daemon resumes from stored cursors `[MEASURED via test_kill_daemon.py]`.
5. **Deterministic Mode Switching**: Network cuts switch to offline in $\le 6.1$ s; restores switch to online in $\le 4.6$ s `[MEASURED via local-simulated test_network_switch.py]`.
6. **Clock Jump Immunity**: All timers, polling loops, and window indices use `time.monotonic()` and integer sample index arithmetic, immune to system clock jumps `[MEASURED via test_clock_jump.py]`.
7. **Cloud Data Integrity**: PostgreSQL primary key `(session_id, sensor, idx)` guarantees zero duplicates in cloud storage `[MEASURED]`.

### NOT Guaranteed:
1. **Data Discarded by the Mock Prior to Read**: If the pipeline is stopped longer than the mock's 10-second internal ring buffer (4,000 samples), data purged by the mock cannot be recovered; it is accurately recorded as `lost` with `buffer_overflow` or `gap_detected_unread`.
2. **Conflicting Samples from External Sources**: If an external caller injects conflicting values for an already recorded `(session_id, sensor, idx)`, the conflicting sample is rejected and discarded to preserve data integrity `[MEASURED via test_fault_proxy.py]`.
3. **15-Minute 2-Hour Catch-Up on Free Cloud Deployments**: While localhost achieves 12,455 samples/s (~7.7 min), the public internet connection to Render free tier achieves ~1,689 to 2,534 samples/s (~38 to 57 minutes), exceeding the 15-minute target.

---

## 4. Power Cut Analysis & Data Loss Boundary

- **Tested Power-Cut Simulation**: Hard process kill (`proc.kill()` with `taskkill /F` / `kill -9`) mid-write `[MEASURED]`. SQLite WAL with `synchronous=FULL` passed `PRAGMA integrity_check` with 0 duplicate samples and 0 database corruption.
- **Physical AC Power Pull**: `[NOT RUN]` (requires pulling physical power cable on real Raspberry Pi hardware; instructions in [`HUMAN_TODO.md`](HUMAN_TODO.md)).
- **Theoretical Maximum Data Loss on Power Cut**:
  1. The pipeline reader polls each sensor every $250$ ms.
  2. Each batch of up to 100 samples (250 ms) is committed to disk via SQLite `PRAGMA synchronous = FULL` in an atomic transaction before the read cursor advances.
  3. Under `synchronous = FULL`, SQLite issues an explicit `fsync()` on the WAL file upon transaction commit, ensuring data is written to physical storage media.
  4. If power is abruptly cut mid-execution, any transaction that finished committing is completely durable (**0 seconds of acknowledged data lost**). Data currently sitting in the mock's TCP socket buffer that has not yet been polled or committed amounts to at most 1 polling interval (**$\le 0.25$ seconds of unread mock data**).

---

## 5. Assumptions & Ambiguities Resolved

1. **Dual Results for a Window**: Where both an offline result (computed locally during network outage) and an online result (computed in the cloud after catch-up) exist for a window, both are preserved with their respective `mode` (`"offline"` vs `"online"`). `pipeline results --session ID` yields both lines.
2. **Sensor 1 Channel Mapping in Ramp Mode**: Ramp mode specification `(3i + c) mod 8388608` is mapped using 0-indexed channels $c \in \{0, 1, 2\}$ corresponding to channels `s1c1`, `s1c2`, `s1c3`.
3. **Sensor 2 Magnitude**: Defined as the mean of $\sqrt{ch1^2 + ch2^2}$ across all available Sensor 2 samples in the window. If Sensor 2 has 0 available samples, `magnitude` is `null`.
4. **Heart Rate Window Lookahead**: Because sample $i$ requires sample $i+1$ to be evaluated as a peak, the final sample of window $w$ (index $400w+399$) requires sample $400(w+1)$ of Sensor 1 to be available or marked lost before window $w$ can be finalized.
5. **Deterministic Rounding**: In accordance with the prompt guidance, Python's default bankers rounding (`round()`) is avoided; deterministic `ROUND_HALF_UP` via `decimal.Decimal` quantized to `Decimal('0.1')` is used across all metrics.
6. **Benchmark Test Sessions**: Sessions `bench_test_*` and `bench_throughput_*` in the cloud database represent synthetic test datasets created during concurrency, throughput, and resilience benchmarks.

---

## 6. Known Limitations

1. **Periodic Online/Offline Mode Flapping**: Observed for about 22 minutes on 17 Oct; 5-minute separate probe test had 0 timeouts; cause not established. Why 3 consecutive probe failures occurred about once per minute remains unexplained (hypotheses include Render free-tier container CPU starvation or reverse-proxy queue contention, but latency jitter was not confirmed as the cause).
2. **Render Catch-Up Throughput Limitation**: Public internet upload throughput to free-tier Render is ~1,689 samples/s (sequential) to ~2,534 samples/s (parallel). Catching up a 2-hour backlog (5.76M samples) over this deployment requires ~38 to 57 minutes, failing the 15-minute SLA that localhost satisfies (~7.7 min).
3. **Render Free-Tier Cold Starts**: Free instances on Render spin down after 15 minutes of inactivity; waking up an instance requires ~50 seconds, triggering temporary offline mode switching until the cloud service responds.
4. **Untested on Physical ARM64 Hardware**: All tests and runs were performed on Windows 10/11 x86_64; physical execution on a Raspberry Pi ARM64 board, systemd auto-start, and physical power cable pulls remain `[NOT RUN]`.
5. **Full 2-Hour Continuous Catch-Up**: Sustained throughput of 12,455.7 samples/s was empirically verified on a 130,000-sample backlog; full 2-hour accumulation (5.76M samples) was `[NOT RUN]`.
6. **1-Hour Continuous Resource Run**: Resource profiling ran for 10 seconds on x86_64; a full 3600-second continuous benchmark on ARM64 was `[NOT RUN]`.
