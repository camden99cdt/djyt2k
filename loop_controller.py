from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LoopBounds:
    start_fraction: float
    end_fraction: float


class LoopController:
    """Simple loop state container shared between the GUI and player."""

    def __init__(self):
        self.enabled = False
        self._bounds = LoopBounds(0.0, 1.0)

    def reset_bounds(self):
        """Reset loop markers to the full track (0 → 1)."""
        self._bounds = LoopBounds(0.0, 1.0)

    def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        return self.enabled

    def set_start(self, position_seconds: float, duration_seconds: float) -> bool:
        """Set the loop start in seconds, stored internally as a fraction."""
        if duration_seconds <= 0:
            return False

        fraction = max(0.0, min(position_seconds / duration_seconds, 1.0))
        if fraction >= self._bounds.end_fraction:
            return False

        self._bounds.start_fraction = fraction
        return True

    def set_end(self, position_seconds: float, duration_seconds: float) -> bool:
        """Set the loop end in seconds, stored internally as a fraction."""
        if duration_seconds <= 0:
            return False

        fraction = max(0.0, min(position_seconds / duration_seconds, 1.0))
        if fraction <= self._bounds.start_fraction:
            return False

        self._bounds.end_fraction = fraction
        return True

    def get_bounds_seconds(self, duration_seconds: float) -> tuple[float, float]:
        duration = max(duration_seconds, 0.0)
        return (
            self._bounds.start_fraction * duration,
            self._bounds.end_fraction * duration,
        )

    def get_bounds_samples(self, total_samples: int) -> tuple[int, int] | None:
        if total_samples <= 0:
            return None

        start = int(self._bounds.start_fraction * total_samples)
        end = int(self._bounds.end_fraction * total_samples)

        start = max(0, min(start, total_samples - 1))
        end = max(start + 1, min(end, total_samples))

        if start >= end:
            return None
        return start, end
