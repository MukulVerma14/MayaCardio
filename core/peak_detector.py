"""Stateful Peak Detector and Heart Rate Calculator.

In accordance with Section 3 of the specification:
1. Peak detection is a property of the session stream, not of a window:
   compute it in index order across the whole session (stateful, with
   'previous accepted peak' carried across windows).
2. Sample i is a peak iff ALL hold:
   - The 800 samples before it AND the sample after it are available.
   - Strictly larger than both neighbours: s1c2[i] > s1c2[i-1] and s1c2[i] > s1c2[i+1].
   - Larger than half of the MAXIMUM of the 800 samples before it:
     s1c2[i] > 0.5 * max(s1c2[i-800 ... i-1]).
   - At least 100 samples after the previous accepted peak.
3. For window w, take accepted peaks within the last 5 seconds (samples 400(w-4) .. 400(w+1)-1).
   heart_rate = 60 / (mean gap between consecutive peaks in seconds).
4. heart_rate is null if w < 4, or any sample in those 5 seconds is missing, or fewer than 2 peaks.
5. A window's heart_rate is final only after sample 400(w+1) of sensor 1 has arrived or been declared lost.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, List, Optional, Tuple
from core.rounding import round_metric

PEAK_HISTORY_WINDOW = 800
MIN_PEAK_DISTANCE = 100
HR_WINDOW_SECONDS = 5
SAMPLES_PER_SECOND = 400
HR_WINDOW_SAMPLES = HR_WINDOW_SECONDS * SAMPLES_PER_SECOND  # 2000 samples


class MonotonicMaxDeque:
    """Sliding-window maximum tracker in O(1) amortized time."""

    __slots__ = ("_deque", "_window_size")

    def __init__(self, window_size: int = PEAK_HISTORY_WINDOW) -> None:
        self._window_size = window_size
        # Deque contains (index, value) tuples in strictly descending order of value
        self._deque: deque[Tuple[int, int]] = deque()

    def add(self, idx: int, value: int) -> None:
        """Add sample at idx with given value."""
        min_valid_idx = idx - self._window_size + 1
        # Evict elements that fell outside the window
        while self._deque and self._deque[0][0] < min_valid_idx:
            self._deque.popleft()

        # Evict smaller elements from back to maintain monotonic descending order
        while self._deque and self._deque[-1][1] <= value:
            self._deque.pop()

        self._deque.append((idx, value))

    def get_max_in_range(self, start_idx: int, end_idx: int) -> Optional[int]:
        """Return the maximum value in [start_idx, end_idx] (inclusive), or None if empty."""
        while self._deque and self._deque[0][0] < start_idx:
            self._deque.popleft()
        if not self._deque:
            return None
        return self._deque[0][1]

    def clear(self) -> None:
        self._deque.clear()


class PeakDetector:
    """Stateful peak detector maintaining stream continuity across windows."""

    def __init__(self) -> None:
        self.last_accepted_peak: Optional[int] = None
        self.accepted_peaks: List[int] = []
        self._last_evaluated_idx: int = -1
        self._max_tracker = MonotonicMaxDeque(PEAK_HISTORY_WINDOW)

    def advance_to(
        self,
        target_idx: int,
        get_sample: Callable[[int], Optional[int]],
        is_sample_available: Callable[[int], bool],
    ) -> None:
        """Evaluate all candidate peaks from (last_evaluated_idx + 1) up to target_idx.

        Args:
            target_idx: Highest index to evaluate as candidate peak.
                        Note that evaluating candidate i requires sample i+1 to be available.
            get_sample: Function returning s1c2 value for index i, or None.
            is_sample_available: Function returning True iff sample i is present (not lost/missing).
        """
        start = max(0, self._last_evaluated_idx + 1)
        for i in range(start, target_idx + 1):
            self._evaluate_index(i, get_sample, is_sample_available)
        self._last_evaluated_idx = max(self._last_evaluated_idx, target_idx)

    def _evaluate_index(
        self,
        i: int,
        get_sample: Callable[[int], Optional[int]],
        is_sample_available: Callable[[int], bool],
    ) -> bool:
        """Evaluate if sample i is an accepted peak."""
        # Update sliding max tracker with sample i-1 if available
        if i > 0 and is_sample_available(i - 1):
            val_prev = get_sample(i - 1)
            if val_prev is not None:
                self._max_tracker.add(i - 1, val_prev)

        # Condition 1: Must have 800 samples before it and 1 sample after it
        if i < PEAK_HISTORY_WINDOW:
            return False

        # Must have sample i and sample i+1 available
        if not is_sample_available(i) or not is_sample_available(i + 1):
            return False

        # Must have sample i-1 available
        if not is_sample_available(i - 1):
            return False

        # Condition: ALL 800 samples before it must be available
        # Check if all samples from i - 800 to i - 1 are present
        for check_idx in range(i - PEAK_HISTORY_WINDOW, i):
            if not is_sample_available(check_idx):
                return False

        cur_val = get_sample(i)
        prev_val = get_sample(i - 1)
        next_val = get_sample(i + 1)

        if cur_val is None or prev_val is None or next_val is None:
            return False

        # Condition: Strictly larger than both neighbours
        if not (cur_val > prev_val and cur_val > next_val):
            return False

        # Condition: Larger than half of the MAXIMUM of the 800 samples before it
        # Range is [i - 800, i - 1]
        max_prior = self._max_tracker.get_max_in_range(i - PEAK_HISTORY_WINDOW, i - 1)
        if max_prior is None:
            return False

        if cur_val <= 0.5 * max_prior:
            return False

        # Condition: At least 100 samples after the previous accepted peak
        if self.last_accepted_peak is not None:
            if (i - self.last_accepted_peak) < MIN_PEAK_DISTANCE:
                return False

        # All conditions met! Accept this peak
        self.last_accepted_peak = i
        self.accepted_peaks.append(i)
        return True

    def compute_heart_rate_for_window(
        self,
        window: int,
        is_sample_available: Callable[[int], bool],
    ) -> Optional[float]:
        """Compute heart rate for window w.

        Rules:
        - heart_rate is null if w < 4.
        - heart_rate is null if any sample in the last 5 seconds
          [400(w-4) .. 400(w+1)-1] is missing.
        - heart_rate is null if fewer than 2 peaks in those 5 seconds.
        - Otherwise: 60 / (mean gap between consecutive peaks in seconds).
        - Rounded to 1 decimal place.
        """
        if window < 4:
            return None

        start_idx = SAMPLES_PER_SECOND * (window - 4)
        end_idx = SAMPLES_PER_SECOND * (window + 1) - 1

        # Check if any sample in the 5-second interval is missing
        for idx in range(start_idx, end_idx + 1):
            if not is_sample_available(idx):
                return None

        # Filter accepted peaks falling into [start_idx, end_idx]
        peaks_in_window = [p for p in self.accepted_peaks if start_idx <= p <= end_idx]

        if len(peaks_in_window) < 2:
            return None

        # Calculate mean gap between consecutive peaks
        # For peaks p_0, p_1, ..., p_{N-1}:
        # sum of gaps = (p_{N-1} - p_0) in samples
        # mean gap in samples = (p_{N-1} - p_0) / (N - 1)
        # mean gap in seconds = mean gap in samples / 400.0
        # heart_rate = 60.0 / mean gap in seconds
        #            = (60.0 * 400.0 * (N - 1)) / (p_{N-1} - p_0)
        n = len(peaks_in_window)
        total_sample_span = peaks_in_window[-1] - peaks_in_window[0]
        if total_sample_span <= 0:
            return None

        mean_gap_seconds = (total_sample_span / (n - 1)) / float(SAMPLES_PER_SECOND)
        if mean_gap_seconds <= 0:
            return None

        hr = 60.0 / mean_gap_seconds
        return round_metric(hr)
