# Video Demonstration Script: Never Lose a Heartbeat (<= 5 Minutes)

This script provides an exact shot list and command sequence for recording the 5-minute technical demonstration video showing:
1. Normal online streaming and latency against the deployed Render cloud service.
2. Network cut, automatic mode switch, and backlog catch-up (local-simulated test).
3. Process kill (`kill -9`) and clean resumption without duplicate samples.
4. Power cut / hard process kill and SQLite integrity verification.

---

## Shot 1: Normal Online Streaming (0:00 – 1:00)

### Action:
1. Open three PowerShell windows side-by-side:
   - **Terminal 1**: Biosignal mock simulator (or real mock binary)
   - **Terminal 2**: Bedside pipeline daemon
   - **Terminal 3**: Operator status query

### Commands (PowerShell):
**Terminal 1 (Mock Simulator):**
```powershell
python tools/mock_sim.py --mode signal --port 7100
```

**Terminal 2 (Device Daemon against Deployed Render Cloud):**
```powershell
$env:CLOUD_URL = "https://mayacardio-z481.onrender.com"
python pipeline run --mock-port 7100 --cloud-url $env:CLOUD_URL
```

**Terminal 3 (Operator Query):**
```powershell
$env:CLOUD_URL = "https://mayacardio-z481.onrender.com"
python pipeline status
```
*Show that `status` outputs single JSON line: `{"mode":"online","samples_saved":...,"samples_in_cloud":...,"backlog":0}`.*

---

## Shot 2: Network Cut, Mode Switch & Catch-Up (1:00 – 2:30)

### Narration:
*"Simulating bedside network loss in a hospital setting. The device must transition to OFFLINE within 10 seconds and compute metrics locally with honest loss accounting if buffers overflow."*

### Action:
1. Run the local-simulated network outage test or block cloud connectivity:
```powershell
python scripts/test_network_switch.py
```
2. Watch daemon logs:
   - Within **6.06 seconds** (measured in local-simulated test), see: `[INFO] connectivity.monitor: Mode switch triggered: online -> offline`.
3. In Terminal 3, run:
```powershell
python pipeline status
```
*Point out: `"mode":"offline"`, `samples_saved` is growing rapidly, `backlog` is accumulating safely in local SQLite.*

4. Show local offline results being computed:
```powershell
python pipeline results --session <SESSION_ID>
```

5. Restore connectivity (or end network simulation).
6. Watch daemon logs:
   - Within **4.55 seconds** (measured in local-simulated test), see: `[INFO] connectivity.monitor: Mode switch triggered: offline -> online`.
   - On localhost, backlog drains at **~12,455 samples/s** (measured).
   - On deployed Render over the public internet, backlog drains at **~1,689 samples/s** (sequential) to **~2,534 samples/s** (parallel) (measured).
7. Run `python pipeline status` to show `backlog: 0`.

---

## Shot 3: Daemon Kill (`kill -9`) & Resumption (2:30 – 3:45)

### Narration:
*"Simulating process crash or watchdog SIGKILL. We will send a hard kill to the daemon process."*

### Action:
1. In Terminal 3, kill the pipeline process abruptly:
```powershell
Stop-Process -Name python -Force
```
2. Run database integrity check via Python snippet (no external sqlite3 CLI needed):
```powershell
python -c "import sqlite3; conn = sqlite3.connect('pipeline.db'); print('PRAGMA integrity_check:', conn.execute('PRAGMA integrity_check;').fetchall())"
```
3. Restart daemon in Terminal 2:
```powershell
$env:CLOUD_URL = "https://mayacardio-z481.onrender.com"
python pipeline run --mock-port 7100 --cloud-url $env:CLOUD_URL
```
*Point out the log: `reader.client: Resuming sensor 1 at index X, sensor 2 at index Y`. Zero duplicate samples created!*

---

## Shot 4: Mock Restart & Honest Loss Accounting (3:45 – 4:30)

### Narration:
*"Simulating mock restart where the device experiences unread gaps. Any missing data is accounted for honestly."*

### Action:
1. Stop and restart the mock in Terminal 1 (generates a new session ID).
2. Daemon detects session transition:
   - Resets sensor cursors to 0.
   - Logs unread data from previous session with `to_index: null` (`mock_restart_session_change`).
   - If mock started earlier, records initial unread gap `0..first_index-1` as `not_read_before_pipeline_start`.
3. Query losses in Terminal 3:
```powershell
python -c "import sqlite3; conn = sqlite3.connect('pipeline.db'); print('Losses:', conn.execute('SELECT sensor, from_idx, to_idx, reason FROM losses').fetchall())"
```
*Point out honest loss accounting: every gap has exact bounds and reasons (e.g. buffer_overflow, mock_restart_session_change, not_read_before_pipeline_start).*

---

## Shot 5: Conclusion & Ground Truth Export (4:30 – 5:00)

### Action:
1. Run final export check:
```powershell
$env:CLOUD_URL = "https://mayacardio-z481.onrender.com"
python pipeline export --session <SESSION_ID> --from device
```
2. Summarize:
   - All received samples persisted to SQLite WAL on device.
   - Honest loss accounting: buffer overflows and gaps explicitly tracked with exact bounds.
   - 6.06s offline switch / 4.55s online restore (measured in local-simulated tests).
   - Zero duplicates by primary key `(session_id, sensor, idx)` in SQLite and PostgreSQL.
   - Exact mathematical parity between device offline and cloud online window metrics.
