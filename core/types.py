"""Core types and data structures shared across device and cloud pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True, slots=True)
class Sample:
    """A single sensor sample containing reading values at a specific index."""
    sensor: int
    idx: int
    values: List[int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sensor": self.sensor,
            "index": self.idx,
            "values": list(self.values),
        }


@dataclass(frozen=True, slots=True)
class LossRecord:
    """A record of missing or lost samples."""
    sensor: int
    from_index: int
    to_index: Optional[int]
    reason: str
    detected_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "sensor": self.sensor,
            "from_index": self.from_index,
            "to_index": self.to_index,
            "reason": self.reason,
        }
        if self.detected_at is not None:
            d["detected_at"] = self.detected_at
        return d


@dataclass(frozen=True, slots=True)
class WindowResult:
    """Computed metric summary for a 1-second (400-sample) window."""
    session_id: str
    window: int
    mode: str  # 'online' or 'offline'
    missing_1: int
    missing_2: int
    complete: bool
    mean: Dict[str, Optional[float]]
    min: Dict[str, Optional[float]]
    max: Dict[str, Optional[float]]
    magnitude: Optional[float]
    heart_rate: Optional[float]
    processed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "window": self.window,
            "mode": self.mode,
            "missing_1": self.missing_1,
            "missing_2": self.missing_2,
            "complete": self.complete,
            "mean": self.mean,
            "min": self.min,
            "max": self.max,
            "magnitude": self.magnitude,
            "heart_rate": self.heart_rate,
            "processed_at": self.processed_at,
        }
