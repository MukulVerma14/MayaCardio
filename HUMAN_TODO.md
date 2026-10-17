# Action Items for Human Candidate (HUMAN_TODO.md)

This document contains everything that requires human intervention, physical hardware, external credentials, or real-world recording.

---

## 1. Cloud Deployment (Free Tier, per provider documentation, verify at signup)

The cloud backend is built with FastAPI (`cloud/server.py`) and supports PostgreSQL (Neon / Supabase) via `CLOUD_DATABASE_URL`, or SQLite.

### Recommended Free Tier Setup:
1. **Neon Serverless PostgreSQL (neon.tech)**:
   - Create a free account at [neon.tech](https://neon.tech) (0.5 GB free storage, per provider documentation, verify at signup).
   - Copy the connection string: `postgres://user:password@ep-...neon.tech/neondb?sslmode=require`.
2. **Render.com (Free Web Service)**:
   - Create a free account at [render.com](https://render.com) (per provider documentation, verify at signup).
   - Click **New** -> **Web Service** -> Connect your GitHub repo.
   - Set Build Command: `pip install .`
   - Set Start Command: `uvicorn cloud.server:app --host 0.0.0.0 --port $PORT`
   - Set Environment Variable: `CLOUD_DATABASE_URL=postgres://user:password@ep-...neon.tech/neondb?sslmode=require`
3. **Render Cold-Start & Keepalive Verification**:
   - Render free instances spin down after 15 minutes of inactivity (cold-start takes ~50s).
   - **Keepalive (design intent, not tested)**: The bedside pipeline's `ConnectivityMonitor` sends a lightweight `/api/v1/heartbeat` every **2.0 seconds** while online.
   - This continuous heartbeat is designed to keep the free Render instance warm, preventing spin-down during active patient monitoring.
4. **Configure Bedside Device**:
   - Set `CLOUD_URL=https://your-app-name.onrender.com` on your bedside Raspberry Pi.

---

## 2. Physical Hardware & Physical Failure Testing

The automated test suite executed hard process termination (`kill -9`), socket disconnections, and disk full simulations on Windows x86_64. You should perform the physical hardware tests:

1. **Deploy to Raspberry Pi (ARM64)**:
   - Copy repo to a Raspberry Pi 4 running Raspberry Pi OS (64-bit).
   - Install systemd unit:
     ```bash
     sudo cp mayacardio-pipeline.service /etc/systemd/system/
     sudo systemctl daemon-reload
     sudo systemctl enable --now mayacardio-pipeline
     ```
2. **Physical AC Power Pull**:
   - While streaming, physically pull the power cable from the Raspberry Pi.
   - Re-insert power and verify automatic boot, service restart, and DB integrity (`pipeline status`).
3. **Physical Network Cable Pull**:
   - Disconnect the Ethernet cable / turn off WiFi.
   - Verify mode switch to `offline` in `pipeline status`.
   - Re-insert cable and verify automatic return to `online` and backlog catch-up.
4. **1-Hour Continuous Resource Run on ARM64**:
   - Run the 1-hour profiling benchmark on the physical board:
     ```bash
     python scripts/run_1hour_benchmark.py --duration 3600 --mode signal
     ```

---

## 3. Official Mock Binary & Ground Truth Tool

The candidate repository ships with `tools/mock_sim.py`, a spec-compliant simulator. When the hiring team provides the official binaries:
1. Make the binary executable:
   ```bash
   chmod +x biosignal-mock check_ground_truth
   sudo cp check_ground_truth /usr/local/bin/
   ```
2. Start the official mock instead of `mock_sim.py`:
   ```bash
   ./biosignal-mock --mode signal --seed 7 --duration 3600 --port 7100
   ```
3. Run `python scripts/test_ground_truth.py`. It will automatically detect `check_ground_truth` in `PATH` and invoke it directly.

---

## 4. Record Technical Demonstration Video (<= 5 Minutes)

1. Follow the exact shot list in [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md).
2. Cover:
   - Normal streaming and latency (`pipeline status`).
   - Network cut and automatic switch to OFFLINE ($\le 10$ s).
   - Backlog catch-up upon network restore ($\le 10$ s).
   - Process termination (`kill -9`) and clean resumption.
   - Power cut simulation and database integrity verification.
3. Upload to YouTube (Unlisted), Loom, or Google Drive (public link).

---

## 5. Case Submission
- Copy GitHub Repository Link.
- Copy Demonstration Video Link.
- Complete the MayaCardio assessment submission form.
