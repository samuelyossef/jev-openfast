"""Timing totals remain complete when detailed evidence is bounded."""

from jev_ultrafast.timing import Timings


def test_totals_survive_event_log_limit():
    timing = Timings()
    timing.reset(10)
    timing.extend([{"stage": "guard", "started_at": 10, "duration_ms": 2, "status": "ok"}] * 1200)
    snapshot = timing.snapshot(since=10)
    assert len(snapshot["events"]) == 1000
    assert snapshot["stages"]["guard"] == {"count": 1200, "duration_ms": 2400}
    assert timing.snapshot(since=10, details=False) == {"stages": snapshot["stages"]}


def test_late_stage_from_previous_turn_cannot_contaminate_new_totals():
    timing = Timings()
    timing.reset(20)
    timing.extend([{"stage": "capture", "started_at": 19, "duration_ms": 100, "status": "ok"},
                   {"stage": "guard", "started_at": 21, "duration_ms": 2, "status": "ok"}])
    assert timing.snapshot(since=20)["stages"] == {"guard": {"count": 1, "duration_ms": 2}}


def test_compact_totals_do_not_walk_detailed_events():
    timing = Timings()
    timing.extend([{"stage": "guard", "started_at": 10, "duration_ms": 2, "status": "ok"}])
    class UnreadableEvents:
        def __iter__(self):
            raise AssertionError("Compact polling must not copy detailed events")
    timing.events = UnreadableEvents()
    assert timing.snapshot(details=False) == {"stages": {"guard": {"count": 1, "duration_ms": 2}}}


def test_detailed_percentiles_use_retained_samples_and_current_turn_only():
    timing = Timings()
    timing.reset(10)
    timing.extend([{"stage": "model", "started_at": 9, "duration_ms": 999, "status": "ok"}])
    timing.extend([{"stage": "model", "started_at": 10, "duration_ms": value, "status": "ok"}
                   for value in range(1, 21)])
    assert timing.snapshot()["latency_samples"]["model"] == {
        "sample_count": 20, "p50_ms": 10.5, "p95_ms": 19}
    assert timing.snapshot(since=11)["latency_samples"] == {}
    assert "latency_samples" not in timing.snapshot(details=False)
