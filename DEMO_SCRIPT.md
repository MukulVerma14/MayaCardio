# Video Demonstration Script: Never Lose a Heartbeat (<= 5 Minutes)

This script provides an exact shot list and command sequence for recording the 5-minute technical demonstration video showing:
1. Normal online streaming and latency.
2. Network cut, automatic mode switch, and backlog catch-up.
3. Process kill (`kill -9`) and clean resumption.
4. Power cut / hard stop and SQLite integrity verification.

---

## Shot 1: Normal Online Streaming (0:00 – 1:00)

### Action:
1. Open three terminal windows side-by-side:
   - **Terminal 1**: Biosignal mock simulator (or real mock binary)
   - **Terminal 2**: Cloud backend service
   - **Terminal 3**: Bedside pipeline daemon & operator commands

### Commands:
**Terminal 1 (Mock):**
```bash
python tools/mock_sim.py --mode signal --port 7100
```

**Terminal 2 (Cloud):**
```bash
uvicorn cloud.server:app --host 0.0.0.0 --port 8000
```

**Terminal 3 (Device Daemon):**
```bash
python pipeline run --mock-port 7100 --cloud-url http://127.0.0.1:8000
```

**Terminal 4 (Operator Query):**
```bash
python pipeline status
```
*Show that `status` outputs single JSON line: `{"mode":"online","samples_saved":...,"samples_in_cloud":...,"backlog":0}`.*

---

## Shot 2: Network Cut, Mode Switch & Catch-Up (1:00 – 2:30)

### Narration:
*"Simulating bedside network loss in a hospital setting. The device must transition to OFFLINE within 10 seconds and compute metrics locally without losing a single sample."*

### Action:
1. In Terminal 2, stop the cloud server with `Ctrl+C` (or block via iptables).
2. Watch Terminal 3 logs:
   - Within 6 seconds, see: `[INFO] connectivity.monitor: Mode switch triggered: online -> offline`.
3. In Terminal 4, run:
```bash
python pipeline status
```
*Point out: `"mode":"offline"`, `samples_saved` is growing rapidly, `backlog` is accumulating safely in local SQLite.*

4. Show local offline results being computed:
```bash
python pipeline results --session <SESSION_ID>
```

5. In Terminal 2, restart the cloud server:
```bash
uvicorn cloud.server:app --host 0.0.0.0 --port 8000
```

6. Watch Terminal 3:
   - Within 4 seconds, see: `[INFO] connectivity.monitor: Mode switch triggered: offline -> online`.
   - See backlog drain at > 12,000 samples/sec.
7. Run `python pipeline status` to show `backlog: 0`.

---

## Shot 3: Daemon Kill (`kill -9`) & Resumption (2:30 – 3:45)

### Narration:
*"Simulating process crash or watchdog SIGKILL. We will send a hard kill -9 to the daemon process."*

### Action:
1. In Terminal 4, find the pipeline PID and kill it abruptly:
```bash
kill -9 $(pgrep -f "pipeline run")
# On Windows PowerShell:
# Stop-Process -Name python -Force
```
2. Run database integrity check to prove no SQLite corruption:
```bash
python -c "import sqlite3; conn = sqlite3.connect('pipeline.db'); print('Integrity:', conn.execute('PRAGMA integrity_check').fetchone()[0])"
```
3. Restart daemon in Terminal 3:
```bash
python pipeline run --mock-port 7100 --cloud-url http://127.0.0.1:8000
```
*Point out the log: `reader.client: Resuming sensor 1 at index X, sensor 2 at index Y`. Zero duplicates created!*

---

## Shot 4: Power Cut / Sudden Reboot (3:45 – 4:45)

### Narration:
*"Simulating sudden power loss where the bedside device is unplugged from the wall without shutting down."*

### Action:
1. If on Linux/VM:
```bash
# Simulates hard crash
echo b > /proc/sysrq-trigger
```
*(Or pull the physical power plug on Raspberry Pi, or abruptly kill the VM/container without SIGTERM).*
2. Power back on and boot system.
3. Show systemd daemon automatically starts on boot:
```bash
sudo systemctl status mayacardio-pipeline
```
4. Query pipeline status and show that no samples were duplicated and all data prior to the power cut was safely flushed via SQLite WAL with `synchronous=FULL`.

---

## Shot 5: Conclusion & Ground Truth Export (4:45 – 5:00)

### Action:
1. Run final export check:
```bash
python pipeline export --session <SESSION_ID> --from device | head -n 10
python pipeline export --session <SESSION_ID> --from cloud | head -n 10
```
2. Summarize: Zero data loss, sub-5-second cloud latency, 6s/4s mode switching, complete parity.
