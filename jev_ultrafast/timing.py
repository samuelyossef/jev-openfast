"""Bounded, thread-safe timing evidence; overlapping stages are not additive."""

import threading
import time
from contextlib import contextmanager
from functools import wraps


class Timings:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.since = 0
        self.stages = {}

    def reset(self, since):
        with self.lock:
            self.since = since
            self.events = []
            self.stages = {}

    def extend(self, events):
        with self.lock:
            for event in events:
                if event["started_at"] < self.since:
                    continue
                self.events.append(dict(event))
                stage = self.stages.setdefault(event["stage"], {"count": 0, "duration_ms": 0})
                stage["count"] += 1
                stage["duration_ms"] = round(stage["duration_ms"] + event["duration_ms"], 3)
            del self.events[:-1000]

    @contextmanager
    def measure(self, stage):
        started = time.perf_counter()
        status = "ok"
        try:
            yield
        except Exception:
            status = "error"
            raise
        finally:
            event = {"stage": stage, "started_at": started,
                     "duration_ms": round((time.perf_counter() - started) * 1000, 3), "status": status}
            self.extend([event])

    def snapshot(self, since=0, details=True):
        with self.lock:
            if not details and since <= self.since:
                return {"stages": {name: dict(value) for name, value in self.stages.items()}}
            events = [dict(event) for event in self.events if event["started_at"] >= since]
            totals = {name: dict(value) for name, value in self.stages.items()} if since <= self.since else None
        stages = {}
        for event in events:
            stage = stages.setdefault(event["stage"], {"count": 0, "duration_ms": 0})
            stage["count"] += 1
            stage["duration_ms"] = round(stage["duration_ms"] + event["duration_ms"], 3)
        return {"stages": totals if totals is not None else stages, **({"events": events} if details else {})}


def timed(stage):
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            if not hasattr(self, "timings"):
                self.timings = Timings()
            with self.timings.measure(stage):
                return method(self, *args, **kwargs)
        return wrapped
    return decorate
