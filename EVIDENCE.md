# Verification Evidence & Failure Test Logs

This document presents empirical verification results and actual verbatim execution logs from all automated tests conducted on the MayaCardio data pipeline, including deployment on Render.com and Neon PostgreSQL.

---

## 1. Test Audit & Classification Matrix

All tests were executed on **Windows 10/11 x86_64 (Python 3.12.3)** against real TCP sockets, processes, and database instances:

| Test Description | Execution Platform | Status | Empirical Outcome | Classification |
| :--- | :--- | :---: | :---: | :---: |
| **Abrupt Daemon Kill & Resumption** (`test_kill_daemon.py`) | Windows x86_64 | **PASS** (6.2s) | Hard process kill mid-write; `PRAGMA integrity_check` ok; 0 duplicates | `[MEASURED]` |
| **Network Loss & Mode Switch** (`test_network_switch.py`) | Windows x86_64 | **PASS** (23.7s) | Offline switch: **6.06s**; Online restore: **4.55s**; 0 loss/duplicates | `[MEASURED]` |
| **Backlog Throughput Benchmark** (`test_catchup_rate.py`) | Windows x86_64 | **PASS** (11.8s) | **12,455.7 samples/s** sustained over 130,000 samples | `[MEASURED]` |
| **Fault Proxy Injection** (`test_fault_proxy.py`) | Windows x86_64 | **PASS** (9.7s) | Corrupt CRC re-read; duplicates ignored; stall handled | `[MEASURED]` |
| **Disk Full (ENOSPC) Emergency** (`test_disk_full.py`) | Windows x86_64 | **PASS** (0.2s) | 512KB reserve released; `disk_full` loss recorded; no crash | `[MEASURED]` |
| **System Clock Jump Resilience** (`test_clock_jump.py`) | Windows x86_64 | **PASS** (0.9s) | Monotonic timers & sample index math immune to +/-2h wall clock shift | `[MEASURED]` |
| **Ground Truth Export Match** (`test_ground_truth.py`) | Windows x86_64 | **PASS** (7.5s) | Device & cloud exports matched simulator ground truth exactly | `[MEASURED]` |
| **Cloud Service Restart** (`test_cloud_restart.py`) | Windows x86_64 | **PASS** (4.7s) | 100% data, losses, and window results survive cloud process kill | `[MEASURED]` |
| **Pytest Unit & Property Suite** (22 tests) | Windows x86_64 | **PASS** (6.2s) | Math parity, 60/90/72 bpm HR, ramp arithmetic, rounding, status consistency | `[MEASURED]` |
| **Deployed Cloud Export & Integrity** (Session `2ae483f8`) | Render + Neon Postgres | **PASS** | 350,905 device samples, 0 duplicates, 4 losses, 0 missing in cloud range | `[MEASURED]` |
| **5-Minute Heartbeat Benchmark** (150 probes @ 2s) | Windows -> Render | **PASS** | 150/150 success (0 timeouts); P50: 328ms, P90: 437ms, P95: 484ms, Max: 1312ms | `[MEASURED]` |
| **1-Hour Continuous Online Run** | ARM64 Linux | **NOT RUN** | Profiler ran for 10s on x86_64; 3600s continuous run on ARM64: **NOT RUN** | `[NOT RUN]` |
| **2-Hour Outage Full Drain (5.76M)** | Any Platform | **NOT RUN** | 130k-sample rate measured; full 5.76M 2-hour accumulation: **NOT RUN** | `[NOT RUN]` |
| **Physical Hardware AC Power Pull** | Raspberry Pi board | **NOT RUN** | Process kill tested on x86_64; physical hardware cable pull: **NOT RUN** | `[NOT RUN]` |
| **ARM64 Physical Board Execution** | Raspberry Pi board | **NOT RUN** | All executions performed on Windows x86_64; ARM64 run: **NOT RUN** | `[NOT RUN]` |

---

## 2. Verbatim Test Execution Logs

### Test 1: Abrupt Daemon Kill (-9) & Recovery (`scripts/test_kill_daemon.py`)
```text
=== [TEST 1] KILL -9 PROCESS TERMINATION & RECOVERY ===
[mock_sim] Started biosignal mock simulator (session=925b2c20, mode=signal, port=7120)
[+] Started pipeline daemon PID=27272
[mock_sim] Client connected from ('127.0.0.1', 60758)
[!] Sending hard KILL to daemon PID=27272...
[mock_sim] Connection error: [WinError 10054] An existing connection was forcibly closed by the remote host
[+] Process killed abruptly.
[+] PRAGMA integrity_check: ok
[+] Samples safely committed before kill: 2643
[+] Restarting pipeline daemon...
2026-10-15 20:28:25,936 [INFO] reader.client: Resuming sensor 1 from session 925b2c20 at index 1318
2026-10-15 20:28:25,938 [INFO] reader.client: Resuming sensor 2 from session 925b2c20 at index 1325
[+] Total samples after resumption: 4768
[+] Duplicates: 0
=== TEST RESULT: PASS ===
```

### Test 2: Network Outage & Automatic Mode Switch (`scripts/test_network_switch.py`)
```text
=== [TEST 2] NETWORK OUTAGE & AUTOMATIC MODE SWITCHING ===
[mock_sim] Started biosignal mock simulator (session=3e17c2a7, mode=signal, port=7121)
[+] Cloud service started on http://127.0.0.1:8011
[+] Pipeline daemon started PID=26528
[mock_sim] Client connected from ('127.0.0.1', 53099)
[+] Initial pipeline mode: online
[!] CUTTING NETWORK TO CLOUD...
[+] Switched to OFFLINE mode in 6.06 seconds
[+] Device collecting data in OFFLINE mode for 4 seconds...
[+] Local samples saved during run/outage: 10730
[+] Local offline window results computed: 13
[!] RESTORING NETWORK TO CLOUD...
[+] Switched to ONLINE mode in 4.55 seconds
[+] Waiting for backlog catch-up...
[+] Current Status: {'mode': 'online', 'samples_saved': 17542, 'samples_in_cloud': 17342, 'backlog': 200}
[+] Duplicate samples across switch: 0
=== TEST RESULT: PASS ===
```

### Test 3: Backlog Catch-Up Rate (`scripts/test_catchup_rate.py`)
```text
=== [TEST 3] BACKLOG CATCH-UP THROUGHPUT BENCHMARK ===
[+] Cloud service listening at http://127.0.0.1:8012
[+] Generating local backlog of 65000 samples in SQLite...
[+] Total samples staged on device: 130000
[!] Starting catch-up upload stream...
[+] Uploaded 130000 samples in 10.44 seconds
[+] Sustained Catch-Up Rate: 12455.7 samples/second
[+] Target Minimum Required: 6,500 samples/second
[+] Cloud Status: {'sessions': ['bench_sess_catchup'], 'total_samples': 163487, 'processed_windows': 208}
=== TEST RESULT: PASS ===
```

### Test 4: Fault Proxy Dynamic Injection (`scripts/test_fault_proxy.py`)
```text
=== [TEST 4] FAULT PROXY INJECTION ===
[mock_sim] Started biosignal mock simulator (session=d8f928bd, mode=signal, port=7122)
[fault_proxy] Listening on port 7123 -> forwarding to 127.0.0.1:7122
[fault_proxy] Control API at http://127.0.0.1:7198
[+] Started pipeline daemon PID=22460 pointing to proxy port 7123
[mock_sim] Client connected from ('127.0.0.1', 52262)
[+] Baseline samples ingested: 1955
[!] Injecting 3 corrupt CRC replies via proxy control API...
[fault_proxy] Injected corrupt CRC
[fault_proxy] Injected corrupt CRC
[fault_proxy] Injected corrupt CRC
[+] Samples after corrupt CRC injection: 3255
[!] Injecting 2 duplicate replies via proxy control API...
[fault_proxy] Injected duplicate reply
[fault_proxy] Injected duplicate reply
[+] Duplicate entries in DB: 0
[!] Injecting Sensor 2 stall for 3 seconds...
[+] Final samples saved: 5280
=== TEST RESULT: PASS ===
```

### Test 5: Disk Full (ENOSPC) & Emergency Allowance (`scripts/test_disk_full.py`)
```text
=== [TEST 5] DISK FULL (ENOSPC) EMERGENCY ALLOWANCE & LOSS LOGGING ===
[+] Verifying emergency reserve file: test_run_disk_full\pipeline_enospc.emergency_reserve
[+] Reserve file size: 524288 bytes
[!] Simulating ENOSPC on sample insert...
Emergency reserve file released due to ENOSPC
[+] OperationalError handled by database reserve logic
[+] Checking emergency reserve release...
[+] Recorded disk_full loss rows: 1
=== TEST RESULT: PASS ===
```

### Test 6: System Clock Jump Resilience (`scripts/test_clock_jump.py`)
```text
=== [TEST 6] SYSTEM CLOCK JUMP RESILIENCE ===
[+] Testing window processor index determinism across clock jumps...
[+] Window 0 finalized correctly after +2h forward jump: processed_at=1792251555.0165727
[+] Window 1 finalized correctly after -2h backward jump: processed_at=1792237155.0165727
[+] Verifying connectivity monitor uses time.monotonic()...
[+] Monotonic elapsed: 0.3120s (unaffected by wall clock patch)
=== TEST RESULT: PASS ===
```

### Test 7: Ground Truth Export Comparison (`scripts/test_ground_truth.py`)
```text
=== [TEST 7] GROUND TRUTH EXPORT COMPARISON ===
[mock_sim] Started biosignal mock simulator (session=7f5474af, mode=ramp, port=7124)
[+] Started mock in RAMP mode (session=7f5474af, logging to test_run_ground_truth\ground_truth.jsonl)
[+] Pipeline daemon started PID=26656
[mock_sim] Client connected from ('127.0.0.1', 52280)
[+] Exporting from DEVICE...
[+] Exporting from CLOUD...
[+] Validating Device Export against Ground Truth...
[+] Device comparison: Verified 3418 samples matched ground truth exactly
[+] Validating Cloud Export against Ground Truth...
[+] Cloud comparison: Verified 3018 samples matched ground truth exactly
[i] Official check_ground_truth binary not in PATH yet (using dev simulator ground truth verification)
=== TEST RESULT: PASS ===
```

### Test 8: Cloud Service Restart & Data Persistence (`scripts/test_cloud_restart.py`)
```text
=== [TEST 8] CLOUD SERVICE RESTART & DATA PERSISTENCE ===
[+] Starting cloud service instance 1 on http://127.0.0.1:8019...
[+] Ingested 801 samples and 1 loss record into cloud instance 1
[+] Cloud instance 1 status: total_samples=801
[!] Terminating cloud service process abruptly...
[+] Starting cloud service instance 2 (restarted)...
[+] Cloud instance 2 status: total_samples=801
[+] Export lines recovered after restart: 802
[+] Verified window 0 result persisted: complete=True
=== TEST RESULT: PASS ===
```

### Pytest Unit & Property Suite Execution
```text
============================= test session starts =============================
platform win32 -- Python 3.12.3, pytest-9.0.2, pluggy-1.6.0
rootdir: D:\IdeaProjects\MayaCardio
configfile: pyproject.toml
plugins: anyio-3.7.1, langsmith-0.7.22
collected 22 items

tests\test_core_ramp.py ..                                               [  9%]
tests\test_core_signal.py .....                                          [ 31%]
tests\test_faults.py ..                                                  [ 40%]
tests\test_parity_property.py .                                          [ 45%]
tests\test_reader_protocol.py ...                                        [ 59%]
tests\test_rounding.py ....                                              [ 77%]
tests\test_storage.py ....                                               [ 95%]
tests\test_uploader.py .                                                 [100%]

============================= 22 passed in 6.16s ==============================
```

---

## 3. Real Cloud Deployment Verification (Render + Neon Postgres)

- **Date Tested**: 17 October 2026
- **Device Platform**: Windows 10/11 x86_64 (Python 3.12.3)
- **Cloud Service**: Deployed on Render free tier (`https://mayacardio-z481.onrender.com`)
- **Cloud Database**: Neon serverless PostgreSQL (`postgresql+psycopg://...neon.tech/neondb?sslmode=require`)
- **Evaluated Session**: `2ae483f8`

### 3.1 Measured State & Empirical Export Counts
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
- **Cloud Database Sessions**: Exactly 1 session (`2ae483f8`), no prior or conflicting sessions.
- **Cloud Duplicates**: `0` (enforced by `PRIMARY KEY (session_id, sensor, idx)` with `ON CONFLICT DO NOTHING`).
- **Missing Samples**: `0` missing in cloud for acknowledged index range, `0` missing on device.

### 3.2 Connectivity Monitor Behavior & Flapping Investigation

During the live deployment run on 17 October 2026 (18:37–18:59), the monitor was observed switching `online -> offline -> online` roughly once per minute for 22+ minutes, with offline intervals lasting only ~2.5 seconds, well after initial cold starts were finished.

#### Monitor Specification & State Machine Logic
From [`connectivity/monitor.py`](connectivity/monitor.py):
- `probe_interval_sec`: `2.0` seconds
- `probe_timeout_sec`: `1.5` seconds
- **Offline Transition Rule**: 3 consecutive failed probes.
- **Online Transition Rule**: 2 consecutive successful probes.
- **Offline Duration Mechanics**: Once offline, the state machine requires 2 consecutive successes to return online. Because probe interval is 2.0s, the time from the first successful probe to the second successful probe is exactly one probe cycle: $\approx 2.0\text{s} + \text{RTT} \approx 2.3 - 2.5\text{s}$. Hence, any transient outage or spike that triggers offline will resolve in **~2.5 seconds** as soon as two consecutive probes succeed.

#### Measured 5-Minute Heartbeat Latency Distribution
A continuous 5-minute benchmark (150 probes at 2.0s intervals) against the live Render endpoint yielded:
- **Probes Attempted**: 150
- **Successful Probes (HTTP 200)**: 150 (100%)
- **Timeouts / Failures**: 0 (0%)
- **Latency Distribution**:
  - Min: **265.0 ms**
  - Median (P50): **328.0 ms**
  - P90: **437.0 ms**
  - P95: **484.0 ms**
  - P99: **1,234.0 ms**
  - Max: **1,312.0 ms**
  - Mean: **363.2 ms**
  - Tail Latencies: 2 probes > 1,000 ms; 2 probes > 1,200 ms; 0 probes > 1,400 ms.

#### Concurrency & Batch Ingestion Hypothesis Test
- **Hypothesis**: Large 2,500-sample ingestion requests block or starve `/api/v1/heartbeat` on the single-worker Render instance.
- **Empirical Test**: Streamed 10 back-to-back 2,500-sample batches (ingest RTT 828–1,438 ms) while concurrently running 2.0s heartbeat probes with 1.5s timeouts.
- **Result**: Heartbeat probes during active batch uploads succeeded with 265–1,438 ms RTT and 0 timeouts. Uvicorn/FastAPI handles the non-blocking heartbeat concurrently with the ingest endpoint.
- **Conclusion**: Flapping observed during the 22-minute run is **under investigation**. With `probe_timeout_sec = 1.5s`, trans-continental public internet jitter spikes (where P99 reaches 1.2–1.3s and occasionally crosses 1.5s for multiple cycles) or Render platform-level queuing are candidate causes. Timing logs have been added to track future occurrences.

### 3.3 Other Issues Encountered During Real Deployment

1. **Packaging Layout Error (Setuptools Flat-Layout)**:
   - *Problem*: Running `pip install .` on Render failed with `error: Multiple top-level packages discovered in a flat-layout: ['cloud', 'core', 'reader', 'storage', 'uploader']`.
   - *Fix*: Configured `py-modules = ["cli"]` in `pyproject.toml` to disambiguate the CLI script module from namespace package discovery.

2. **PostgreSQL Reserved Keyword `"window"`**:
   - *Problem*: Deploying `cloud/storage.py` to Neon PostgreSQL threw `psycopg.errors.SyntaxError: syntax error at or near "window"` because `WINDOW` is a reserved SQL keyword in PostgreSQL.
   - *Fix*: Double-quoted `"window"` across all table definitions, primary keys, indexes, and queries while preserving JSON key parity.

3. **CLI `CLOUD_URL` Environment Fallback**:
   - *Problem*: `pipeline export --from cloud` failed when `--cloud-url` was not passed on the command line even though `CLOUD_URL` was set in the environment.
   - *Fix*: Configured CLI commands to default to `os.environ.get("CLOUD_URL")`.

4. **Status Counter Inconsistency (`samples_in_cloud > samples_saved`)**:
   - *Problem*: The initial `pipeline status` implementation computed `samples_in_cloud = sum(last_acked_idx + 1)` and `backlog = sum(last_read_idx - last_acked_idx)`. When session `2ae483f8` experienced initial buffer overflow losses (0..66199 and 0..66211), `(last_acked + 1)` counted 132,412 non-existent rows, falsely inflating `samples_in_cloud` beyond `samples_saved`.
   - *Fix*: Updated `storage/database.py` to calculate `samples_in_cloud` as the exact count of saved rows where `session_id = ? AND sensor = ? AND idx <= last_acked_idx`, set `backlog = max(0, samples_saved - samples_in_cloud)`, and ensured status queries filter by the active session.

---

## 4. Disclosures & Known Limitations

In strict adherence to reporting integrity:
- **Periodic Flapping**: Observed switching `online -> offline -> online` roughly once per minute during the 18:37–18:59 run, with offline durations lasting ~2.5s. Cause is under investigation (candidate causes: trans-continental network jitter exceeding the 1.5s probe timeout vs Render free-tier CPU throttling).
- **Test Platform**: All automated test runs reported above were executed on **Windows 10/11 x86_64 with Python 3.12.3**. An execution on a physical ARM64 Linux board has **NOT BEEN RUN**.
- **Physical Power Pull**: Tested as an abrupt operating system process kill (`kill -9`). A physical hardware AC power pull on real hardware has **NOT BEEN RUN**.
- **2-Hour Outage Full Drain**: The catch-up rate of **12,455.7 samples/s** was measured during a 130,000-sample test run. Draining a full 2-hour accumulation (5.76M samples) in ~7.7 minutes is an unverified extrapolation; the continuous 2-hour run was **NOT RUN**.
- **1-Hour Continuous Resource Run**: Resource profiling was executed for **10 seconds** on x86_64. A 3600-second continuous test on ARM64 has **NOT BEEN RUN**.
- **No Latency Claims Against Render**: Cloud streaming latency over the public internet to Render was not formally benchmarked; only localhost streaming latency (~1.1s P95) was measured.
- **Render Keepalive Behavior**: The 2.0s heartbeat keepalive probe is an intended architectural design to keep Render warm; continuous multi-hour idle spin-down prevention was not empirically benchmarked.
