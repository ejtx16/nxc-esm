"""Simulation clock. Scenarios replay on the case-study calendar at a speed multiplier."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from app import settings

FMT = "%Y-%m-%dT%H:%M:%S"


def parse(ts: str | datetime | None) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if not ts:
        return clock.now()
    return datetime.fromisoformat(str(ts).strip())


def iso(dt: datetime) -> str:
    return dt.strftime(FMT)


class SimClock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.speed = 1.0
        self.paused = False
        self._anchor_sim = settings.sim_start()
        self._anchor_real = time.monotonic()

    def now(self) -> datetime:
        with self._lock:
            if self.paused:
                return self._anchor_sim
            return self._anchor_sim + timedelta(seconds=(time.monotonic() - self._anchor_real) * self.speed)

    def now_iso(self) -> str:
        return iso(self.now())

    def reset(self, start: datetime | None = None, speed: float = 1.0) -> None:
        with self._lock:
            self._anchor_sim = start or settings.sim_start()
            self._anchor_real = time.monotonic()
            self.speed = speed
            self.paused = False

    def jump_to(self, target: datetime) -> None:
        """Move forward to `target` (never backwards) keeping the current speed."""
        with self._lock:
            if target > self.now():
                self._anchor_sim = target
                self._anchor_real = time.monotonic()

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._anchor_sim = self.now() + timedelta(seconds=seconds)
            self._anchor_real = time.monotonic()

    def set_speed(self, speed: float) -> None:
        with self._lock:
            self._anchor_sim = self.now()
            self._anchor_real = time.monotonic()
            self.speed = max(0.0, speed)

    def pause(self) -> None:
        with self._lock:
            self._anchor_sim = self.now()
            self.paused = True

    def resume(self) -> None:
        with self._lock:
            self._anchor_real = time.monotonic()
            self.paused = False


clock = SimClock()
