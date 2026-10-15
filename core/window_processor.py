"""Core window processing logic shared across device and cloud pipelines.

Computes window statistics, channel min/max/mean, sensor 2 magnitude,
and window heart rate according to Section 3 of the specification.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Set, Tuple

from core.peak_detector import PeakDetector, SAMPLES_PER_SECOND
from core.rounding import round_metric
from core.types import WindowResult


class WindowProcessor:
    """Stateful window processor for a single session stream.

    Processes 1-second windows (400 samples each).
    Can process sequentially as batches arrive, maintaining peak detector state.
    """

    def __init__(self, session_id: str, mode: str = "online") -> None:
        self.session_id = session_id
        self.mode = mode
        self.peak_detector = PeakDetector()

        # In-memory index-to-sample stores
        # sensor 1: idx -> tuple of 3 channel ints (c1, c2, c3)
        self._s1_samples: Dict[int, Tuple[int, int, int]] = {}
        # sensor 2: idx -> tuple of 2 channel ints (c1, c2)
        self._s2_samples: Dict[int, Tuple[int, int]] = {}

        # Known missing/lost indices
        self._s1_lost: Set[int] = set()
        self._s2_lost: Set[int] = set()

        # Highest index seen (or declared lost) per sensor
        self.s1_highest_known: int = -1
        self.s2_highest_known: int = -1

        # Windows already finalized
        self.next_window_to_finalize: int = 0
        self.finalized_results: List[WindowResult] = []

        # Flag indicating session has ended
        self.session_ended: bool = False

    def add_sample(self, sensor: int, idx: int, values: List[int]) -> None:
        """Add a received sample."""
        if sensor == 1:
            if len(values) >= 3:
                self._s1_samples[idx] = (int(values[0]), int(values[1]), int(values[2]))
                if idx > self.s1_highest_known:
                    self.s1_highest_known = idx
        elif sensor == 2:
            if len(values) >= 2:
                self._s2_samples[idx] = (int(values[0]), int(values[1]))
                if idx > self.s2_highest_known:
                    self.s2_highest_known = idx

    def add_loss_range(self, sensor: int, from_idx: int, to_idx: Optional[int]) -> None:
        """Mark a range of indices as lost/missing."""
        if to_idx is None:
            # If to_idx is null, it indicates open-ended loss (e.g. mock restart or disconnect).
            # We record up to the current window lookahead boundary or mark session ended.
            return

        if sensor == 1:
            for i in range(from_idx, to_idx + 1):
                self._s1_lost.add(i)
            if to_idx > self.s1_highest_known:
                self.s1_highest_known = to_idx
        elif sensor == 2:
            for i in range(from_idx, to_idx + 1):
                self._s2_lost.add(i)
            if to_idx > self.s2_highest_known:
                self.s2_highest_known = to_idx

    def mark_session_ended(self) -> None:
        """Mark that no further samples will arrive for this session."""
        self.session_ended = True

    def is_s1_available(self, idx: int) -> bool:
        """Return True iff sensor 1 sample at idx is present (available)."""
        return idx in self._s1_samples

    def is_s1_known(self, idx: int) -> bool:
        """Return True if sample idx is either available or confirmed lost."""
        if idx in self._s1_samples or idx in self._s1_lost:
            return True
        if self.session_ended and idx > self.s1_highest_known:
            return True
        return False

    def is_s2_known(self, idx: int) -> bool:
        """Return True if sample idx is either available or confirmed lost."""
        if idx in self._s2_samples or idx in self._s2_lost:
            return True
        if self.session_ended and idx > self.s2_highest_known:
            return True
        return False

    def can_finalize_window(self, window: int) -> bool:
        """Check if window w is finalizable.

        A window is finalizable when, for both sensors, every index in it
        (plus lookahead sample 400(w+1) for sensor 1) is either present or declared lost/missing.
        """
        start_idx = window * SAMPLES_PER_SECOND
        end_idx = start_idx + SAMPLES_PER_SECOND - 1
        lookahead_s1_idx = end_idx + 1  # 400(w+1)

        # Check Sensor 2 indices for the window
        for idx in range(start_idx, end_idx + 1):
            if not self.is_s2_known(idx):
                return False

        # Check Sensor 1 indices for the window PLUS lookahead sample
        for idx in range(start_idx, lookahead_s1_idx + 1):
            if not self.is_s1_known(idx):
                return False

        return True

    def finalize_available_windows(self, processed_at: Optional[float] = None) -> List[WindowResult]:
        """Finalize all windows that have become finalizable.

        Returns newly finalized window results.
        """
        new_results: List[WindowResult] = []

        while self.can_finalize_window(self.next_window_to_finalize):
            w = self.next_window_to_finalize
            result = self._compute_window(w, processed_at=processed_at)
            self.finalized_results.append(result)
            new_results.append(result)
            self.next_window_to_finalize += 1

            # Evict historical samples older than peak history requirement (800 + 2000 = 2800 samples)
            # to keep memory usage minimal and bounded on embedded systems
            self._prune_history(self.next_window_to_finalize)

        return new_results

    def _compute_window(self, window: int, processed_at: Optional[float] = None) -> WindowResult:
        """Compute metrics for window w."""
        start_idx = window * SAMPLES_PER_SECOND
        end_idx = start_idx + SAMPLES_PER_SECOND - 1
        lookahead_idx = end_idx + 1

        # Advance peak detector up to end_idx
        # (needs sample lookahead_idx to evaluate end_idx)
        def get_s1c2(idx: int) -> Optional[int]:
            s = self._s1_samples.get(idx)
            return s[1] if s is not None else None

        self.peak_detector.advance_to(
            target_idx=end_idx,
            get_sample=get_s1c2,
            is_sample_available=self.is_s1_available,
        )

        # Compute heart rate for window
        hr = self.peak_detector.compute_heart_rate_for_window(
            window=window,
            is_sample_available=self.is_s1_available,
        )

        # Compute missing counts
        missing_1 = 0
        missing_2 = 0

        # Collect available samples in window
        s1_avail: List[Tuple[int, int, int]] = []
        s2_avail: List[Tuple[int, int]] = []

        for idx in range(start_idx, end_idx + 1):
            s1 = self._s1_samples.get(idx)
            if s1 is not None:
                s1_avail.append(s1)
            else:
                missing_1 += 1

            s2 = self._s2_samples.get(idx)
            if s2 is not None:
                s2_avail.append(s2)
            else:
                missing_2 += 1

        complete = (missing_1 == 0 and missing_2 == 0)

        # Compute per-channel min, max, mean
        means: Dict[str, Optional[float]] = {}
        mins: Dict[str, Optional[float]] = {}
        maxs: Dict[str, Optional[float]] = {}

        if s1_avail:
            for ch_idx, ch_name in enumerate(["s1c1", "s1c2", "s1c3"]):
                vals = [s[ch_idx] for s in s1_avail]
                means[ch_name] = round_metric(sum(vals) / len(vals))
                mins[ch_name] = round_metric(min(vals))
                maxs[ch_name] = round_metric(max(vals))
        else:
            for ch_name in ["s1c1", "s1c2", "s1c3"]:
                means[ch_name] = None
                mins[ch_name] = None
                maxs[ch_name] = None

        if s2_avail:
            for ch_idx, ch_name in enumerate(["s2c1", "s2c2"]):
                vals = [s[ch_idx] for s in s2_avail]
                means[ch_name] = round_metric(sum(vals) / len(vals))
                mins[ch_name] = round_metric(min(vals))
                maxs[ch_name] = round_metric(max(vals))
        else:
            for ch_name in ["s2c1", "s2c2"]:
                means[ch_name] = None
                mins[ch_name] = None
                maxs[ch_name] = None

        # Compute magnitude over available Sensor 2 samples:
        # mean of sqrt(ch1^2 + ch2^2)
        if s2_avail:
            mags = [math.sqrt(float(s[0] ** 2 + s[1] ** 2)) for s in s2_avail]
            magnitude = round_metric(sum(mags) / len(mags))
        else:
            magnitude = None

        return WindowResult(
            session_id=self.session_id,
            window=window,
            mode=self.mode,
            missing_1=missing_1,
            missing_2=missing_2,
            complete=complete,
            mean=means,
            min=mins,
            max=maxs,
            magnitude=magnitude,
            heart_rate=hr,
            processed_at=processed_at,
        )

    def _prune_history(self, current_window: int) -> None:
        """Prune historical samples no longer needed for window or peak calculations.

        Peak detection needs at most 5 seconds of window lookback (2000 samples)
        plus 800 prior samples for peak validation = 2800 samples (~7 windows).
        We keep a safe margin of 10 windows (4000 samples).
        """
        keep_from_idx = max(0, (current_window - 10) * SAMPLES_PER_SECOND)
        if keep_from_idx > 0:
            # Clean up old samples
            s1_keys_to_del = [k for k in self._s1_samples if k < keep_from_idx]
            for k in s1_keys_to_del:
                del self._s1_samples[k]

            s2_keys_to_del = [k for k in self._s2_samples if k < keep_from_idx]
            for k in s2_keys_to_del:
                del self._s2_samples[k]

            # Clean up old lost records
            self._s1_lost = {k for k in self._s1_lost if k >= keep_from_idx}
            self._s2_lost = {k for k in self._s2_lost if k >= keep_from_idx}
