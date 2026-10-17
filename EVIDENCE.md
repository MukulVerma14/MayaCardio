# Empirical Test Evidence & Verification Logs: MayaCardio Pipeline

This document compiles the empirical execution logs, verbatim test results, failure recovery benchmarks, and live cloud deployment audits for the MayaCardio data pipeline.

---

## 1. Automated Failure & Resilience Test Suite

Executed on **Windows 10/11 x86_64, Python 3.12.3**.

| Test Script | Target Scenario | Verification Result | Measured Metrics | Audit Classification |
| :--- | :--- | :---: | :--- | :---: |
| [`test_kill_daemon.py`](scripts/test_kill_daemon.py) | Abrupt process termination (`kill -9`) | **PASS** (17.7s) | 0 duplicate samples, 0 missing samples; `PRAGMA integrity_check` clean | `[MEASURED]` |
| [`test_network_switch.py`](scripts/test_network_switch.py) | Local-simulated network cut & restore | **PASS** (23.7s) | Offline switch: **6.06s**; Online restore: **4.55s**; 0 loss/duplicates | `[MEASURED (local-simulated)]` |
| [`test_catchup_rate.py`](scripts/test_catchup_rate.py) | Backlog catch-up throughput | **PASS** (10.4s) | **12,455.7 samples/s** (localhost only) over 130,000 backlog | `[MEASURED]` |
| [`test_fault_proxy.py`](scripts/test_fault_proxy.py) | Corrupt CRC, stalls, conflicting duplicates | **PASS** (15.5s) | Bad CRC rejected; conflicting duplicates discarded; 0 corruption | `[MEASURED]` |
| [`test_disk_full.py`](scripts/test_disk_full.py) | Storage disk full (ENOSPC) | **PASS** (11.0s) | Reserve file released; emergency loss record written; 0 silent crash | `[MEASURED]` |
| [`test_clock_jump.py`](scripts/test_clock_jump.py) | Wall-clock jumps (+/- 2 hours) | **PASS** (8.4s) | Monotonic window alignment unaffected; 0 window calculation skew | `[MEASURED]` |
| [`test_ground_truth.py`](scripts/test_ground_truth.py) | Bit-level sample & CRC ground truth | **PASS** (10.9s) | 100% value parity against simulated signals | `[MEASURED]` |
| [`test_cloud_restart.py`](scripts/test_cloud_restart.py) | Cloud process kill & restart persistence | **PASS** (13.7s) | Pre-kill + post-kill samples persist with 0 loss and 0 duplicates | `[MEASURED]` |

---

## 2. Unit & Property Verification (`pytest`)

```text
============================= test session starts =============================
platform win32 -- Python 3.12.3, pytest-9.0.2, pluggy-1.6.0
rootdir: D:\IdeaProjects\MayaCardio
configfile: pyproject.toml
testpaths: tests
plugins: anyio-3.7.1, langsmith-0.7.22
collected 23 items

tests\test_core_ramp.py ..                                               [  8%]
tests\test_core_signal.py .....                                          [ 30%]
tests\test_faults.py ..                                                  [ 39%]
tests\test_parity_property.py .                                          [ 43%]
tests\test_reader_protocol.py ....                                       [ 60%]
tests\test_rounding.py ....                                              [ 78%]
tests\test_storage.py ....                                               [ 95%]
tests\test_uploader.py .                                                 [100%]

============================= 23 passed in 6.27s ==============================
```

---

## 3. Real Cloud Deployment Verification (Render + Neon Postgres)

- **Date Tested**: 17 October 2026
- **Device Platform**: Windows 10/11 x86_64 (Python 3.12.3)
- **Cloud Service**: Deployed on Render free tier (`https://mayacardio-z481.onrender.com`)
- **Cloud Database**: Neon serverless PostgreSQL (`postgresql+psycopg://...neon.tech/neondb?sslmode=require`)
- **Evaluated Sessions**: `2ae483f8` and `167e65b1`

### 3.1 Live Deployment Audit: Session `2ae483f8`
Exported session `2ae483f8` from device and cloud via `pipeline export`:
- **Total Device Sample Lines**: `350,905`
- **Unique (sensor, index) Pairs**: `350,905`
- **Device Duplicates**: `0`
- **Loss Records**: `4` records (all `buffer_overflow`):
  - Sensor 1: `[0, 66199]` (66,200 samples lost)
  - Sensor 2: `[0, 66211]` (66,212 samples lost)
  - Sensor 1: `[105424, 137105]` (31,682 samples lost)
  - Sensor 2: `[105431, 137167]` (31,737 samples lost)
  - Total lost indices logged: `195,831`
- **Sensor Index Ranges**:
  - Sensor 1: min `66,200`, max `273,361` (175,480 samples saved)
  - Sensor 2: min `66,212`, max `273,373` (175,425 samples saved)
- **Cloud Duplicates**: `0` (enforced by `PRIMARY KEY (session_id, sensor, idx)` with `ON CONFLICT DO NOTHING`).
- **Missing Samples**: `0` missing in cloud for acknowledged index range, `0` missing on device.

### 3.2 Connectivity Monitor Behavior & Flapping Investigation

- **Observed Behavior**: The connectivity monitor was observed switching `online -> offline -> online` roughly once per minute for about 22 minutes on 17 Oct (18:37–18:59), with offline blips lasting only ~2.5 seconds.
- **Separate Probe Test**: A 5-minute separate probe test (150 probes at 2.0s intervals) had **0 timeouts** and 100% success (P50: 328ms, P90: 437ms, P99: 1,234ms, Max: 1,312ms).
- **Cause Status**: **Cause not established**. Prior hypotheses asserting trans-continental public internet latency jitter as the confirmed cause have been removed.
- **Why 3 Consecutive Probe Failures Occur (~1/min)**:
  - From `connectivity/monitor.py`, switching to offline requires 3 consecutive probe timeouts/failures ($\approx 4.5\text{s} \dots 6.0\text{s}$). Returning online requires 2 consecutive successes ($1 \text{ interval} + \text{RTT} \approx 2.3\text{--}2.5\text{s}$), which explains why offline blips resolved in ~2.5s.
  - Why 3 consecutive probes failed once per minute remains **unexplained**. Leading candidate mechanisms include:
    1. Render free-tier container CPU quota throttling: On shared free-tier infrastructure, CPU starvation or pause events lasting 5–6 seconds occurring periodically would stall inbound HTTP requests across 3 probe cycles.
    2. Transient reverse-proxy connection queuing or TLS socket reset on Render.
  - In a concurrent upload test (sending 10 back-to-back 2,500-sample batches during probing), heartbeats completed with 0 timeouts (RTT 265–1,438ms). Thus, ingest processing itself did not block heartbeats.

### 3.3 Verification of Session `167e65b1` & Silent Gap Investigation

#### 1. Device Export Metrics (`export_device_167e65b1.jsonl`, 114,690,665 bytes):
- **Sample Lines**: `1,911,275`
- **Unique (sensor, index) Pairs**: `1,911,275`
- **Device Duplicates**: `0`
- **Loss Records**: `4` records:
  - Sensor 1: `[986818, 1091461]` (`buffer_overflow`, 104,644 samples lost)
  - Sensor 2: `[986824, 1091567]` (`buffer_overflow`, 104,744 samples lost)
  - Sensor 1: `[1440000, null]` (`mock_restart_session_change`)
  - Sensor 2: `[1440000, null]` (`mock_restart_session_change`)
- **Sensor Index Ranges**:
  - Sensor 1: min `759,337`, max `1,439,999` (576,019 samples saved). Indices `0..759,336` (759,337 indices) were missing with no loss record.
  - Sensor 2: min `0`, max `1,439,999` (1,335,256 samples saved).

#### 2. Root Cause of Sensor 1 Silent Gap (`0..759,336`):
- `pipeline.db` history reveals that session `2ae483f8` was active immediately prior and stopped at index 759,336 (sensor 1) and 759,342 (sensor 2).
- When the daemon restarted, `_init_cursors_from_db()` initialized `expected_indices[1] = 759,337` and `expected_indices[2] = 759,343` from the database.
- The mock restarted concurrently with a new session `167e65b1`.
- When Sensor 2's first reply arrived, it detected the session ID change and reset `expected_indices[s] = 0`. Sensor 2 therefore read from index 0.
- However, Sensor 1's request had ALREADY been dispatched with `from_index: 759,337` before the session change was handled, so Sensor 1 began reading from index 759,337.
- Furthermore, `reader/client.py` lacked logic to log an initial unread range (`0..first_index-1`) when a sensor's first read in a session starts at index > 0.
- **Fix Applied**:
  - Updated `reader/client.py` to immediately reset all sensor cursors to 0 upon detecting any session change.
  - Added logic in `reader/client.py`: if `first_index > exp_idx` and `exp_idx == 0`, record `LossRecord(sensor, 0, first_index - 1, reason="not_read_before_pipeline_start")`.
  - Added unit test `test_reader_records_initial_unread_gap_on_session_start` in `tests/test_reader_protocol.py` (passes).

#### 3. Cloud Export 502 Cause & Cloud Verification:
- Streaming the complete unpaginated session `167e65b1` directly from Render returned **HTTP 502 Bad Gateway**.
  - *Cause*: The query `SELECT ... FROM samples WHERE session_id = :sess ORDER BY idx ASC, sensor ASC` across 4.8M table rows attempted an external sort in memory. Render's free tier has only 512 MB RAM; the sort exceeded available memory and exceeded the 60-second gateway timeout.
- A streamed export (`export_cloud_167e65b1.jsonl`, 5,200,828 bytes) collected 89,861 sample lines before truncating:
  - **Cloud Export Sample Lines**: `89,861` (Sensor 2: indices 0..89,860)
  - **Unique (sensor, index) Pairs**: `89,861`
  - **Cloud Duplicates**: `0`
  - **Missing on Device**: `0` (every sample in the cloud export matches the device export)
  - **Missing in Partial Cloud Export**: `1,821,414` samples
  - **Duplicate-Freeness Guarantee**: Duplicate-freeness in cloud PostgreSQL is enforced by primary key `PRIMARY KEY (session_id, sensor, idx)` with `ON CONFLICT DO NOTHING`.
  - **Test Data in Cloud DB**: Sessions `bench_test_*` and `bench_throughput_*` in the cloud database are documented as synthetic test datasets from throughput and concurrency benchmarks.

### 3.4 Public Internet Upload Throughput & Catch-Up Benchmark

Measured against live deployment `https://mayacardio-z481.onrender.com`:
- **Sequential Upload (1 worker, 2,500 samples/batch, Gzip)**:
  - 50,000 samples uploaded in 29.61s = **1,688.6 samples/s**.
- **Parallel Upload (2 workers)**:
  - 50,000 samples uploaded in 21.03s = **2,377.3 samples/s** (1.41x speedup).
- **Parallel Upload (4 workers)**:
  - 50,000 samples uploaded in 19.73s = **2,533.7 samples/s** (1.50x speedup).

#### Recomputation of 2-Hour Catch-Up Projection:
- 2 hours of data at 400 Hz for 2 sensors = $2 \times 400 \times 7,200 = 5,760,000$ samples.
- The 15-minute SLA requires an upload rate of:
  $$\frac{5,760,000}{900\text{ s}} = 6,400\text{ samples/s}$$
- **Localhost Performance**: Sustained **12,455.7 samples/s** (`test_catchup_rate.py`) $\rightarrow \frac{5,760,000}{12,455.7} = 462.4\text{ s} \approx 7.7\text{ minutes}$. **Requirement MET on localhost.**
- **Render Public Internet Performance**:
  - Sequential (1,688.6 samples/s): $\frac{5,760,000}{1,688.6} = 3,411.1\text{ s} \approx \mathbf{56.9\text{ minutes}}$.
  - Parallel 4 workers (2,533.7 samples/s): $\frac{5,760,000}{2,533.7} = 2,273.4\text{ s} \approx \mathbf{37.9\text{ minutes}}$.
- **Conclusion**: **The 15-minute catch-up requirement is NOT MET on this free Render cloud deployment over the public internet.**

---

## 4. Disclosures & Known Limitations

In strict adherence to reporting integrity:
- **Flapping**: Observed for about 22 minutes on 17 Oct; 5-minute separate probe test had 0 timeouts; cause not established.
- **Render Catch-Up Throughput**: Public internet upload throughput is ~1,689 samples/s (sequential) to ~2,534 samples/s (parallel). Catching up 2 hours requires ~38 to 57 minutes, failing the 15-minute SLA.
- **Test Platform**: Automated test runs were executed on **Windows 10/11 x86_64 with Python 3.12.3**. Execution on a physical ARM64 Linux board has **NOT BEEN RUN**.
- **Physical Power Pull**: Tested as an abrupt operating system process kill (`kill -9`). A physical hardware AC power pull on real hardware has **NOT BEEN RUN**.
- **Continuous 2-Hour Outage Run**: Full continuous 2-hour accumulation (5.76M samples) was **NOT RUN**; catch-up rates were measured over 130,000-sample and 50,000-sample backlogs.
- **1-Hour Continuous Resource Run**: Resource profiling was executed for **10 seconds** on x86_64. A 3600-second continuous test on ARM64 has **NOT BEEN RUN**.
- **Synthetic Test Sessions**: Sessions `bench_test_*` and `bench_throughput_*` in the cloud database are documented as test data.
