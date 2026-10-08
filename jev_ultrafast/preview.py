"""Capture-only worker. Neither HTTP reads nor the decision loop wait for images."""

import copy
import threading
import time

from .model import action_space

# A capture is read-only, so a transient timeout (page still loading, busy daemon) is retried before it is shown.
CAPTURE_ATTEMPTS = 3
MANUAL_INTERVAL = 0.05  # under manual control the frame follows the user's input closely


class Preview:
    def __init__(self, session_id):
        self.session_id = session_id
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.worker = None
        self.revision = 0
        self.key = None
        self.pending = None
        self.cached = None
        self.error = None
        self.manual = None
        self.manual_browser = None

    def begin_manual(self, browser, manual):
        self.invalidate()
        with self.lock:
            self.manual, self.manual_browser = manual, browser
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, daemon=True)
                self.worker.start()
        self.wake.set()

    def end_manual(self):
        with self.lock:
            browser, self.manual, self.manual_browser = self.manual_browser, None, None
        if hasattr(browser, "stop_screencast"):
            browser.stop_screencast()
        self.invalidate()
        self.wake.set()

    def invalidate(self):
        with self.lock:
            self.revision += 1
            self.key = self.pending = self.cached = None
            self.error = None

    def request(self, browser, page):
        if self.closed.is_set() or not hasattr(browser, "capture"):
            return
        key = (page.get("marker", page["fingerprint"]), page["w"], page["h"], page["url"],
               [(a["node"], a["rect"]) for a in page["actions"] if "rect" in a])
        with self.lock:
            if self.closed.is_set():
                return
            if self.manual is not None:
                return
            if key == self.key:
                return
            self.key = copy.deepcopy(key)
            self.revision += 1
            self.pending = (browser, copy.deepcopy(page), self.revision, 1)
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, daemon=True)
                self.worker.start()
        self.wake.set()

    def snapshot(self, after=None):
        with self.lock:
            capture = self.cached
            if capture and after == str(capture["revision"]):
                capture = None
            return {"session_id": self.session_id, "revision": self.revision,
                    "capture": copy.deepcopy(capture), "error": self.error}

    def _run(self):
        last_started = 0
        shown = None  # screencast frame number currently cached
        context, refreshed = None, 0  # the page context seen with the previous frame, and when it was read
        while not self.closed.is_set():
            self.wake.wait(timeout=MANUAL_INTERVAL if self.manual else None)
            self.wake.clear()
            interval = MANUAL_INTERVAL if self.manual else 0.5
            if self.closed.wait(max(0, interval - (time.monotonic() - last_started))):
                return
            with self.lock:
                pending, self.pending = self.pending, None
                manual, manual_browser, revision = self.manual, self.manual_browser, self.revision
            if manual:
                manual.expire()
                if manual.status not in {"active", "uncertain"}:
                    continue
                last_started = time.monotonic()
                try:
                    if hasattr(manual_browser, "screencast"):
                        screenshot, number = manual_browser.screencast()
                        if number == shown and self.cached is not None:
                            # The page did not repaint. A pop-up opened by the last click does not repaint this
                            # tab either, so look for one now and then.
                            if time.monotonic() - refreshed > 0.5:
                                context, refreshed = manual_browser.manual_context(), time.monotonic()
                            continue
                        # One context per frame: it must match the one read before the frame arrived, which is
                        # what the before/after pair around a polled capture used to guarantee.
                        before, context = context, manual_browser.manual_context(follow=False)
                        refreshed, after = time.monotonic(), context
                        if before is None:
                            continue
                    else:
                        before = manual_browser.manual_context()
                        screenshot, number = manual_browser.capture(), None
                        after = manual_browser.manual_context(follow=False)
                    if not screenshot or any(before[k] != after[k] for k in ("document", "w", "h")):
                        continue
                    with manual.lock:
                        with self.lock:
                            if self.manual is not manual or revision != self.revision or self.closed.is_set():
                                continue
                            context = manual.frame(after)
                            self.revision += 1
                            shown = number
                            self.cached = {"revision": self.revision, "captured_at": time.time(), "manual": True,
                                           "context": context,
                                           "page": {**after, "actions": [], "screenshot": screenshot}, "elements": []}
                            self.error = None
                except Exception:
                    with self.lock:
                        if self.manual is manual:
                            self.error = "Não foi possível atualizar a prévia manual."
                continue
            if pending is None:
                continue
            browser, page, revision, attempt = pending
            last_started = time.monotonic()
            try:
                fresh = getattr(browser, "preview_fresh", browser.fresh)
                if not fresh(page):
                    continue
                screenshot = browser.capture()
                if not screenshot or not fresh(page):
                    continue
                capture = {"revision": revision, "captured_at": time.time(),
                           "page": {k: page[k] for k in ("url", "title", "w", "h", "actions")},
                           "elements": action_space(page["actions"])[0]}
                capture["page"]["screenshot"] = screenshot
                capture["page"]["protected"] = page.get("protected", False)
                with self.lock:
                    if revision == self.revision and not self.closed.is_set():
                        self.cached, self.error = capture, None
            except Exception as error:
                with self.lock:
                    if revision != self.revision:
                        continue
                    if attempt < CAPTURE_ATTEMPTS and self.pending is None:
                        # The minimum interval between captures spaces the retries.
                        self.pending = (browser, page, revision, attempt + 1)
                        self.wake.set()
                        continue
                    self.error = f"Não foi possível capturar a prévia: {type(error).__name__}."
                    if "Timeout" in type(error).__name__:
                        self.error += " A tarefa continua; se o Chrome estiver minimizado, restaure a janela."

    def close(self):
        self.closed.set()
        self.invalidate()
        self.wake.set()
