"""Race regressions for image-only preview work; no browser/model providers."""

import copy
import threading
import time

from jev_ultrafast.preview import Preview


class Camera:
    def __init__(self):
        self.entered, self.released = threading.Event(), threading.Event()
        self.starts = []
        self.current = "a"

    def fresh(self, page):
        return page["fingerprint"] == self.current

    def capture(self):
        self.starts.append(time.monotonic())
        self.entered.set()
        assert self.released.wait(3)
        return self.current + "-image"


def page(fingerprint="a", width=500):
    return {"fingerprint": fingerprint, "url": "https://example.org", "title": "Test",
            "w": width, "h": 600, "actions": []}


def wait_for_capture(preview):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = preview.snapshot()["capture"]
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Preview did not finish")


def test_latest_capture_preserves_pair_and_minimum_interval():
    camera, preview = Camera(), Preview("session")
    try:
        preview.request(camera, page())
        assert camera.entered.wait(1)
        camera.current = "b"
        latest = page("b", width=800)
        preview.request(camera, latest)
        latest["w"] = 123  # The caller cannot modify the queued observation.
        camera.released.set()
        capture = wait_for_capture(preview)
        assert capture["page"]["screenshot"] == "b-image"
        assert capture["page"]["w"] == 800 and capture["revision"] == 2
        assert len(camera.starts) == 2
        assert camera.starts[1] - camera.starts[0] >= 0.49
    finally:
        preview.close()
        camera.released.set()
        preview.worker.join(3)


def test_resize_invalidates_capture_already_in_flight():
    camera, preview = Camera(), Preview("session")
    preview.request(camera, page())
    assert camera.entered.wait(1)
    preview.invalidate()
    camera.released.set()
    preview.close()
    preview.worker.join(3)
    assert preview.snapshot()["capture"] is None


def test_unchanged_preview_does_not_copy_capture(monkeypatch):
    preview = Preview("session")
    preview.cached = {"revision": 7, "page": {"screenshot": "image"}}
    copied = []
    original = copy.deepcopy
    monkeypatch.setattr(copy, "deepcopy", lambda value: copied.append(value) or original(value))
    assert preview.snapshot(after="7")["capture"] is None
    assert copied == [None]
    changed = preview.snapshot(after="6")["capture"]
    changed["page"]["screenshot"] = "modified"
    assert preview.cached["page"]["screenshot"] == "image"


def test_preview_failure_is_visible_and_does_not_change_observation():
    camera, preview = Camera(), Preview("session")
    observed = page()
    before = copy.deepcopy(observed)
    def fail():
        raise TimeoutError("capture unavailable")
    camera.capture = fail
    try:
        preview.request(camera, observed)
        deadline = time.monotonic() + 4
        while not preview.snapshot()["error"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "TimeoutError" in preview.snapshot()["error"]
        assert observed == before
    finally:
        preview.close()
        preview.worker.join(3)



def test_transient_capture_timeout_is_retried_without_showing_an_error():
    camera, preview = Camera(), Preview("session")
    camera.released.set()
    real, failures = camera.capture, []

    def flaky():
        if len(failures) < 2:
            failures.append(1)
            raise TimeoutError("busy")
        return real()

    camera.capture = flaky
    try:
        preview.request(camera, page())
        assert wait_for_capture(preview)["page"]["screenshot"] == "a-image"
        assert preview.snapshot()["error"] is None and len(failures) == 2
    finally:
        preview.close()
        preview.worker.join(3)
