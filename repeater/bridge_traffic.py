"""Bounded per-direction crossing admission. Local repeats do not use this budget."""

import threading
import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TrafficLimits:
    per_minute: int = 12
    burst: int = 3
    max_pending_per_direction: int = 16

    @classmethod
    def from_config(cls, cfg):
        if not isinstance(cfg, dict) or set(cfg) - set(cls.__dataclass_fields__):
            raise ValueError("Invalid bridge traffic limits")
        ranges = {"per_minute": (1, 600), "burst": (1, 100), "max_pending_per_direction": (1, 1000)}
        for key, value in cfg.items():
            low, high = ranges[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid bridge traffic limit")
        return cls(**cfg)


class DirectionalTrafficGuard:
    """Direct traffic bypasses flood tokens, but every crossing occupies a queue slot.

    Charge at admission, not successful TX: failed attempts must not turn a busy
    radio into an unlimited retry loop. A known-unsent retry inside one admitted
    native send is covered by its original admission. Limits reset on restart.
    """

    def __init__(self, limits=None, *, clock=time.monotonic):
        self.limits = limits or TrafficLimits()
        self.clock = clock
        self._directions = {}
        self._lock = threading.Lock()

    def admit(self, source, target, *, is_flood):
        if not source or not target or source == target:
            return "Bridge direction unavailable"
        with self._lock:
            now = self.clock()
            key = (source, target)
            # The configured bridge has exactly two directions. Do not let
            # malformed metadata grow an unbounded metrics or token table.
            if key not in self._directions and len(self._directions) >= 2:
                return "Bridge direction unavailable"
            state = self._directions.setdefault(
                key,
                {
                    "pending": 0,
                    "admitted": 0,
                    "rate_limited": 0,
                    "queue_full": 0,
                    "tokens": float(self.limits.burst),
                    "updated": now,
                },
            )
            elapsed = max(0, now - state["updated"])
            state["tokens"] = min(
                self.limits.burst, state["tokens"] + elapsed * self.limits.per_minute / 60
            )
            state["updated"] = max(now, state["updated"])
            if state["pending"] >= self.limits.max_pending_per_direction:
                state["queue_full"] += 1
                return "Bridge direction queue full"
            if is_flood and state["tokens"] < 1:
                state["rate_limited"] += 1
                return "Bridge direction flood rate limited"
            if is_flood:
                state["tokens"] -= 1
            state["pending"] += 1
            state["admitted"] += 1
            return None

    def release(self, source, target):
        with self._lock:
            state = self._directions.get((source, target))
            if state:
                state["pending"] = max(0, state["pending"] - 1)

    def status(self):
        with self._lock:
            return {
                "limits": asdict(self.limits),
                "directions": [
                    {
                        "source_radio": source,
                        "target_radio": target,
                        **{
                            key: state[key]
                            for key in ("pending", "admitted", "rate_limited", "queue_full")
                        },
                    }
                    for (source, target), state in sorted(self._directions.items())
                ],
            }
