# MayaCardio Bedside Biosignal Pipeline ("Never Lose a Heartbeat")

Production-grade, crash-resilient edge data pipeline built for a Raspberry Pi-class Linux board (ARM64, max 4 CPU cores, max 2 GB RAM) and a free-tier cloud service.

---

## Technical Audit & Verification Summary

Every claim and performance figure in this repository is strictly classified below according to empirical execution:
- `[MEASURED]`: Empirically validated with real process execution and verified logs.
- `[PROJECTED / EXTRAPOLATED]`: Derived mathematically from empirical measurements.
- `[UNVERIFIED / NOT RUN]`: Requires physical hardware or long-duration runs not yet executed.

| Requirement / Metric | Claim | Audit Classification | Evidence & Context |
| :--- | :--- | :---: | :--- |
| **Data Preservation** | Zero loss of committed data | `[MEASURED]` | Verified via `test_kill_daemon.py` (`kill -9` process kill). SQLite WAL + `synchronous=FULL` passed `PRAGMA integrity_check`. |
| **Physical Power Pull** | Survives AC hardware disconnect | `[PROJECTED]` | Hard process kill tested. Physical power plug pull requires real hardware (`HUMAN_TODO.md`). |
| **Online Window Latency** | Results appear within 5s | `[MEASURED]` | P95 latency measured at **~1.1s** on local network during streaming benchmark. |
| **Offline Switch Time** | Switch to offline $\le 10$ s | `[MEASURED]` | Measured at **6.08s** upon network cutoff (`test_network_switch.py`). |
| **Online Restore Time** | Switch back to online $\le 10$ s | `[MEASURED]` | Measured at **2.42s** (theoretical hysteresis: 2 probes $\times$ 2.0s = 4.0s max). |
| **Backlog Catch-Up Rate** | Upload rate $\ge 6,500$ samples/s | `[MEASURED]` | Measured sustained rate of **12,455.7 samples/s** over 130,000 samples (`test_catchup_rate.py`). |
| **2-Hour Outage Recovery** | Drains 5.76M samples in $< 15$ min | `[PROJECTED]` | Extrapolated from measured 12,455 samples/s ($\approx 7.7$ min projected). Full 2-hour run not executed. |
| **Cloud Data Persistence** | Survives cloud service restart | `[MEASURED]` | Verified via `test_cloud_restart.py`; 100% data intact across hard cloud server restart. |
| **Resource Profile Duration** | CPU & Memory footprint | `[MEASURED (10s)]` | Measured over 10s run (CPU 25.9% of 1 core, RSS 31.85 MB). Full 3600s run: `[NOT RUN]`. |
| **Execution Platform** | Test execution environment | `[MEASURED]` | Tests executed on **Windows 10/11 x86_64, Python 3.12.3**. Target board: **ARM64 Linux**. |

---

## System Architecture

```mermaid
flowchart LR
    subgraph Device ["Bedside Device (Target: Raspberry Pi ARM64)"]
        Mock["biosignal-mock\n(TCP 7100)"] -->|Raw Lines| Reader["Reader Thread\n(CRC-32 verify)"]
        Reader -->|fsync commit| DB[("SQLite DB (WAL)\nsynchronous=FULL")]
        DB --> OfflineProc["Device Offline\nProcessor"]
        OfflineProc --> DB
        DB --> Uploader["Ack-Based\nUploader"]
        ConnMon["Connectivity\nMonitor"] -.->|Mode| DB
    end
    
    subgraph Cloud ["Free Cloud Backend (Render.com + Neon PostgreSQL)"]
        Uploader -->|Gzip Batches| CloudAPI["Ingest API\n(/api/v1/ingest)"]
        CloudAPI --> SharedCore["Shared Core\nProcessor"]
        SharedCore --> CloudDB[("Neon Serverless\nPostgreSQL")]
    end
```

---

## Installation & Setup on Raspberry Pi OS (ARM64 Target)

### 1. Prerequisites (Fresh Board)
```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git sqlite3
```

### 2. Clone Repository & Setup Virtual Environment
```bash
git clone https://github.com/<your-username>/MayaCardio.git ~/MayaCardio
cd ~/MayaCardio
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -e .
```

### 3. Verify Installation
```bash
# Check pipeline CLI
pipeline status
# Output: {"mode":"online","samples_saved":0,"samples_in_cloud":0,"backlog":0}
```

### 4. Install Systemd Service (Auto-Start on Boot)
```bash
sudo cp mayacardio-pipeline.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable mayacardio-pipeline
sudo systemctl start mayacardio-pipeline

# Check live logs
journalctl -u mayacardio-pipeline -f
```

---

## Cloud Deployment (100% Free Tier, No Credit Card Required)

Deploy `cloud/server.py` to **Render.com** (Free Web Service) using **Neon.tech** (Free Serverless PostgreSQL):

1. **Database Setup (Neon.tech)**:
   - Create a free project at [neon.tech](https://neon.tech) (0.5 GB free storage, no credit card required).
   - Copy the PostgreSQL connection string: `postgres://user:password@ep-...neon.tech/neondb?sslmode=require`.

2. **Web Service Deployment (Render.com)**:
   - Create a free account at [render.com](https://render.com) (no credit card required).
   - Connect repository and select **New Web Service**.
   - Build Command: `pip install .`
   - Start Command: `uvicorn cloud.server:app --host 0.0.0.0 --port $PORT`
   - Environment Variable: `CLOUD_DATABASE_URL=postgres://user:password@ep-...neon.tech/neondb?sslmode=require`

3. **Render Cold-Start & Keepalive Behavior**:
   - Render free instances spin down after 15 minutes of inbound HTTP inactivity. Cold starts take ~50 seconds, which would violate the 5s result latency.
   - **Built-in Keepalive**: While patient monitoring is active, the bedside device's `ConnectivityMonitor` transmits a lightweight `/api/v1/heartbeat` probe every **2.0 seconds**.
   - This regular probe keeps the free Render web service continuously warm and active, preventing cold-start spin-down while the bedside device is in service.

---

## CLI Reference

The device program is named `pipeline`:

| Command | Description |
| :--- | :--- |
| `pipeline run` | Runs the long-running daemon (reader, uploader, connectivity monitor, offline processor) |
| `pipeline status` | Outputs exactly one JSON line: `{"mode":"online","samples_saved":...,"samples_in_cloud":...,"backlog":...}` |
| `pipeline export --session <ID> --from device` | Streams samples and losses in JSON lines format directly from local SQLite |
| `pipeline export --session <ID> --from cloud` | Streams samples and losses from the cloud backend |
| `pipeline results --session <ID>` | Prints computed window metric results for the session |

---

## Running the Automated Test Suite

### 1. Pytest Unit & Property Suite (21 Tests)
```bash
pytest tests/
```
Verifies deterministic rounding, 60/90/72 BPM heart-rate logic, ramp arithmetic, offline/online byte parity, and CRC-32 extraction.

### 2. Failure & Resilience Test Suite
```bash
python scripts/run_all_tests.py
```
Executes:
1. `scripts/test_kill_daemon.py`: Abrupt process kill (`kill -9`) and recovery.
2. `scripts/test_network_switch.py`: Network outage, mode switching (< 10s), catch-up.
3. `scripts/test_catchup_rate.py`: Backlog throughput benchmark (> 6,500 samples/s).
4. `scripts/test_fault_proxy.py`: Fault injection (bad CRC, duplicates, stalls).
5. `scripts/test_disk_full.py`: ENOSPC handling and emergency reserve allowance.
6. `scripts/test_clock_jump.py`: Clock jump (+/- 2h) immunity via monotonic timers.
7. `scripts/test_ground_truth.py`: Line-by-line verification against simulator ground truth.
8. `scripts/test_cloud_restart.py`: Cloud process termination and 100% data persistence.

---

## Deliverables in Repository

- [`DESIGN.md`](file:///D:/IdeaProjects/MayaCardio/DESIGN.md): Architecture diagram, guarantees, power cut analysis, and assumptions.
- [`EVIDENCE.md`](file:///D:/IdeaProjects/MayaCardio/EVIDENCE.md): Empirical test logs and verbatim test outputs.
- [`RESOURCES.md`](file:///D:/IdeaProjects/MayaCardio/RESOURCES.md): Resource benchmarks and ARM64 profiling instructions.
- [`AI_DISCLOSURE.md`](file:///D:/IdeaProjects/MayaCardio/AI_DISCLOSURE.md): Disclosure of AI tools and verification methodology.
- [`DEMO_SCRIPT.md`](file:///D:/IdeaProjects/MayaCardio/DEMO_SCRIPT.md): Shot list and commands for the <= 5 min demo video.
- [`HUMAN_TODO.md`](file:///D:/IdeaProjects/MayaCardio/HUMAN_TODO.md): Candidate action items (accounts, hardware, video, submission).
