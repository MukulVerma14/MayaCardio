"""Core Processing Module.

Shared between embedded device (offline processing) and cloud pipeline (online processing).
Guarantees byte-identical mathematical output.
"""

from core.types import Sample, LossRecord, WindowResult
from core.rounding import round_metric
from core.peak_detector import PeakDetector, SAMPLES_PER_SECOND
from core.window_processor import WindowProcessor

__all__ = [
    "Sample",
    "LossRecord",
    "WindowResult",
    "round_metric",
    "PeakDetector",
    "SAMPLES_PER_SECOND",
    "WindowProcessor",
]
