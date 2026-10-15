# AI Assistance & Tools Disclosure

In accordance with hiring assessment guidelines, this document fully discloses the use of artificial intelligence tools during the development of the MayaCardio bedside data pipeline submission.

---

## 1. Tools Employed
- **AI System**: Antigravity (Google DeepMind)
- **Role**: Software Engineering Assistant (Architecture design, algorithm implementation, unit & integration test development, failure injection harness, and documentation).

---

## 2. Scope of AI Involvement
AI assistance was utilized across the following development phases:
1. **Mathematical Core Implementation**: Implementing the stateful `PeakDetector` and `WindowProcessor` according to Section 3 specifications, including monotonic deque tracking for $O(1)$ maximum calculation and deterministic `ROUND_HALF_UP` decimal quantization.
2. **Crash Resilience & Storage Architecture**: Configuring SQLite WAL mode with `PRAGMA synchronous = FULL` and the emergency space reserve mechanism for ENOSPC disk-full handling.
3. **Protocol & CRC-32 Extraction**: Writing the raw-byte slice extractor and CRC-32 validator to strictly prevent JSON whitespace re-serialization discrepancy.
4. **Automated Testing Harness**: Creating `tools/mock_sim.py`, `tools/fault_proxy.py`, and the failure test suite in `scripts/`.

---

## 3. Verification & Validation Methodology
No AI-generated code or output was accepted without rigorous empirical validation:
- **Test Suite Verification**: All 21 Pytest unit and property tests passed cleanly.
- **Offline/Online Parity**: A property test (`tests/test_parity_property.py`) empirically proved byte-identical numeric outputs across arbitrary batch splits and loss intervals.
- **Real Execution**: All failure tests (`test_kill_daemon.py`, `test_network_switch.py`, `test_catchup_rate.py`, `test_fault_proxy.py`, `test_disk_full.py`, `test_clock_jump.py`, `test_ground_truth.py`) were executed as real OS processes with real TCP sockets and SQLite databases.
- **Ground Truth Export Match**: All exported samples were verified line-by-line against ground-truth simulation logs.
