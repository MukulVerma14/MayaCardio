# AI Assistance & Verification Disclosure

This document discloses the use of artificial intelligence tools during the development and refinement of the bedside biosignal pipeline, delineating AI-generated components from human engineering analysis and verification.

---

## 1. Tools Employed
- **AI System**: Antigravity (Google DeepMind)
- **Role**: Software Engineering Assistant (Architecture formulation, boilerplate implementation, test harnesses, and documentation drafts).

---

## 2. Scope of AI Involvement
AI assistance was utilized across the following development phases:
1. **Mathematical Core Scaffolding**: Drafting the initial `PeakDetector` and `WindowProcessor` structures, including monotonic deque tracking for $O(1)$ maximum calculation and deterministic `ROUND_HALF_UP` decimal quantization via `decimal.Decimal`.
2. **Crash Resilience Scaffolding**: Initial SQLite WAL configuration with `PRAGMA synchronous = FULL` and the emergency reserve file pattern for ENOSPC disk-full handling.
3. **Protocol Framing & CRC-32 Validation**: Initial raw-byte slice extraction and CRC-32 computation against the IEEE 802.3 standard.
4. **Automated Test Scaffolding**: Generating base test fixtures in `tools/mock_sim.py`, `tools/fault_proxy.py`, and initial scripts in `scripts/`.
5. **PostgreSQL Compatibility Refactoring**: Quoting the reserved identifier `"window"` across SQL statements and adding flat-layout configuration in `pyproject.toml`.

---

## 3. Human Engineering, Root Cause Analysis & Empirical Verification
Crucially, AI hypotheses were critically audited and corrected through empirical measurement:
- **Refutation of Flapping Hypothesis**: Initial AI suggestions attributed the 12s flapping cycle to trans-continental public internet latency jitter. Real measurements disproved this (a separate 5-minute probe test showed 0 timeouts). Empirical testing during batch ingest confirmed that synchronous SQLAlchemy/Neon DB inserts in `/api/v1/ingest` starved FastAPI's single-worker asyncio event loop (spiking probe latencies to >3.2s with 53% failures) and requests scalar timeout split connect/read. The engineer implemented worker-thread offloading (`anyio.to_thread.run_sync`), explicit timeout tuples `(1.0, 1.5)`, upload batch pacing, and upload traffic awareness. In a 587 s run against successful ingest (about 10 minutes), no mode switches were observed; the original flapping lasted 22+ minutes; 15% of probes during uploads still exceeded 2 s; cause of the residual spikes not established.
- **Root Cause of Diverging Cursors & Silent Gaps**: Initial AI suggestions assumed data was lost by mock cleanup. Investigation revealed the architectural defect: cursors were keyed only by `sensor` instead of `(session_id, sensor)`, causing stale cursors from previous sessions to be dispatched on startup before the mock's session was confirmed. The engineer corrected the database schema to composite `PRIMARY KEY (session_id, sensor)`, eliminated unconfirmed `from_index` probes, reset cursors on session transitions, and recorded initial unread gaps as `not_read_before_pipeline_start`.
- **Cloud Export 502 Resolution**: The engineer diagnosed Render's 100-second reverse proxy timeout during unpaginated 1.9M-sample queries and implemented range-based pagination in `cli.py` and `cloud/server.py`.
- **Strict Verification**: Every code change was accompanied by executing all 25 Pytest unit and property tests and all 8 integration suites (`scripts/run_all_tests.py`) on Windows x86_64.
- **Reporting Integrity**: Every unverified claim (including "zero sample loss" and projected catch-up times) was corrected to state exact measured numbers and explicit `[NOT RUN]` classifications.
