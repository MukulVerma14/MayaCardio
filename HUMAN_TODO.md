# Action Items for Human Candidate (HUMAN_TODO.md)

This document contains everything that requires human intervention, physical hardware, accounts, or external credentials.

---

## 1. Cloud Deployment (100% Free Tier, No Credit Card Required)

The cloud backend is built with FastAPI (`cloud/server.py`) and is production-ready for deployment on any free Python hosting platform.

### Recommended Free Tier Setup:
1. **Render.com (Free Web Service)**:
   - Create a free account at [render.com](https://render.com).
   - Click **New** -> **Web Service** -> Connect your GitHub repo.
   - Set Build Command: `pip install -r requirements.txt` (or `pip install .`)
   - Set Start Command: `uvicorn cloud.server:app --host 0.0.0.0 --port $PORT`
   - Set Environment Variable: `CLOUD_DB_PATH=/data/cloud_store.db` (or attach free database).
2. **Neon.tech Serverless Postgres (Optional Free Tier)**:
   - Create a free project at [neon.tech](https://neon.tech) (0.5 GB free storage, no credit card).
   - Set `CLOUD_DATABASE_URL=postgres://...` in your Render environment variables.
3. **Verify Deployment**:
   - Test connectivity from your machine:
     ```bash
     curl https://your-cloud-app.onrender.com/api/v1/heartbeat
     ```
   - Set `CLOUD_URL=https://your-cloud-app.onrender.com` on your bedside Raspberry Pi.

---

## 2. Official Mock Binary & Ground Truth Tool

The candidate codebase includes `tools/mock_sim.py`, a spec-compliant simulator. When MayaCardio provides the official binaries:
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

## 3. Deployment on Physical Raspberry Pi Hardware

1. **Flash OS**: Install **Raspberry Pi OS (64-bit)** onto a high-endurance microSD card or USB SSD.
2. **Clone & Set Up**:
   ```bash
   git clone <YOUR_REPO_URL> ~/MayaCardio
   cd ~/MayaCardio
   python3 -m venv venv
   source venv/bin/activate
   pip install .
   ```
3. **Install Systemd Service**:
   ```bash
   sudo cp mayacardio-pipeline.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now mayacardio-pipeline
   ```
4. **Physical Power Cut Test**:
   - While streaming, pull the physical power plug from the Raspberry Pi.
   - Re-insert power and allow the board to boot.
   - Run `python pipeline status` and check `journalctl -u mayacardio-pipeline -e` to confirm automatic resumption.

---

## 4. Record Technical Demonstration Video (<= 5 Minutes)

1. Follow the exact shot list in `DEMO_SCRIPT.md`.
2. Cover:
   - Normal streaming and latency (`pipeline status`).
   - Network cut and automatic switch to OFFLINE (`<= 10s`).
   - Backlog catch-up upon network restore (`<= 10s`).
   - Process termination (`kill -9`) and clean resumption.
   - Power cut simulation and database integrity verification.
3. Upload to YouTube (Unlisted), Loom, or Google Drive (public link).

---

## 5. Case Submission
- Copy the GitHub Repository Link.
- Copy the Demonstration Video Link.
- Complete the MayaCardio submission form.
