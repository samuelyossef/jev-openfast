"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp, drain_events

from .timing import timed

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
PRIVACY = Path(__file__).with_name("privacy.js").read_text(encoding="utf-8")
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class NoHistory(ValueError):
    """Back or Forward with no page in that direction; nothing was navigated."""


def navigating(error):
    """CDP reports a document swap mid-evaluation as a protocol error, not a JS exception."""
    message = str(error).lower()
    return "context" in message or "navigat" in message


def session_lost(error):
    """The tab behind a CDP session closed (a pop-up that closed itself)."""
    message = str(error).lower()
    return "session with given id not found" in message or "target closed" in message


def validate_viewport(width, height):
    if any(type(value) is not int or not 1 <= value <= 4096 for value in (width, height)):
        raise ValueError("A largura e a altura devem ser inteiros entre 1 e 4096 pixels.")
    return width, height


class Browser:
    @timed("startup")
    def __init__(self, url, *, viewport=None):
        self.viewport = validate_viewport(*(viewport or (1120, 780)))
        ensure_daemon()
        self.target = None
        self.openers = []  # (target, session) of owned tabs below the active one, most recent last
        self.popups = set()  # pages our tabs opened that were already followed or skipped
        self.tab_lock = threading.RLock()  # the preview worker and the input path both follow tabs
        try:
            self.target = cdp(
                "Target.createTarget", url="about:blank",
                background=os.environ.get("JEV_HEADLESS_BROWSER") != "1", _response_timeout=15,
            )["targetId"]
            self.session = cdp(
                "Target.attachToTarget", targetId=self.target, flatten=True, _response_timeout=15
            )["sessionId"]
            self._setup_session()
            self.navigate(url)
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _setup_session(self):
        self.set_viewport(*self.viewport)
        self.call("Page.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", source=PRIVACY, runImmediately=True)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)

    def follow_tabs(self):
        """Follow a page an owned tab opened (target=_blank, window.open, sign-in pop-ups) and return to
        its opener when it closes. Returns True when the active tab changed."""
        with self.tab_lock:
            pages = {info["targetId"]: info for info in cdp("Target.getTargets", _response_timeout=15)["targetInfos"]
                     if info["type"] == "page"}
            changed = False
            while self.openers and self.target not in pages:
                self.target, self.session = self.openers.pop()
                changed = True
            owned = {target for target, _ in self.openers} | {self.target}
            # Only pages not seen before: a pop-up already followed or skipped must not take the tab back later.
            opened = [target for target, info in pages.items()
                      if info.get("openerId") in owned and target not in owned and target not in self.popups]
            self.popups.update(opened)
            self.popups &= set(pages)  # forget pages that closed
            if opened:
                session = cdp("Target.attachToTarget", targetId=opened[-1], flatten=True, _response_timeout=15)
                self.openers.append((self.target, self.session))
                self.target, self.session = opened[-1], session["sessionId"]
                # ponytail: secrets typed earlier in another tab are not copied (Python never keeps them, and a
                # page-readable export would leak them); text typed from now on is remembered in every owned tab.
                self._setup_session()
                changed = True
            return changed

    def tabs(self):
        return len(self.openers) + 1

    def set_viewport(self, width, height):
        self.viewport = validate_viewport(width, height)
        self.call("Emulation.setDeviceMetricsOverride", width=self.viewport[0], height=self.viewport[1],
                  deviceScaleFactor=1, mobile=False)

    @timed("navigation")
    def navigate(self, url):
        """Navigate the owned tab once; callers record the request before observing."""
        result = self.call("Page.navigate", url=url)
        if result.get("errorText"):
            raise RuntimeError(f"Navegação falhou: {result['errorText']}")
        self._wait_loaded(blank=url == "about:blank")

    @timed("navigation")
    def reload(self):
        origin = self.evaluate("performance.timeOrigin")
        self.call("Page.reload")
        self._wait_loaded(also=f"performance.timeOrigin!=={json.dumps(origin)}")

    @timed("navigation")
    def history_step(self, step):
        history = self.call("Page.getNavigationHistory")
        index = history["currentIndex"] + step
        if not 0 <= index < len(history["entries"]):
            raise NoHistory("Não há página para " + ("voltar." if step < 0 else "avançar."))
        entry = history["entries"][index]
        self.call("Page.navigateToHistoryEntry", entryId=entry["id"])
        # A cached (back/forward) page keeps its timeOrigin, so wait for the entry's URL instead.
        self._wait_loaded(also=f"location.href==={json.dumps(entry['url'])}")

    def _wait_loaded(self, blank=False, timeout=15, also="true"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                # DOMContentLoaded includes deferred scripts, but does not wait for images.
                # A tab starts on about:blank, which is already complete; wait for the requested document.
                if self.evaluate(f"({also}) && " + (
                        "location.href!=='about:blank' && (document.readyState==='complete' || "
                        "performance.getEntriesByType('navigation')[0]?.domContentLoadedEventEnd>0)"
                        if not blank else "document.readyState==='complete'")):
                    return
            except StalePage:
                pass
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, _response_timeout=15, **params)

    def evaluate(self, expression):
        try:
            response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        except RuntimeError as error:
            if session_lost(error):
                self.follow_tabs()  # back to the opener of the pop-up that just closed
                raise StalePage("Tab closed; observe the page again") from None
            if navigating(error):
                raise StalePage("Document changed during evaluation") from None
            raise
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    @timed("observation")
    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except (RuntimeError, TimeoutError):
                pass
            if self.follow_tabs():
                self._wait_loaded(timeout=5)  # a new tab is about:blank until its document arrives
        # Real navigations take longer than one frame; only a changing page pays for this wait.
        deadline = time.monotonic() + 5
        while True:
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except RuntimeError as error:
                if not session_lost(error):
                    raise
                self.follow_tabs()
            except StalePage:
                if time.monotonic() >= deadline:
                    raise StalePage("Page did not settle") from None
                time.sleep(0.05)

    @timed("guard")
    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "fill", "select", "press_enter"}:
            node = action["node"]
            if type(node) is not int or page["guards"].get(str(node)) is None:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    @timed("execution")
    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        if result.get("open"):
            self._open_tab(result["open"])
        return result

    def _open_tab(self, url):
        """Open an observed new-tab link the way the click would, as an owned tab on top of this one."""
        target = cdp("Target.createTarget", url="about:blank",
                     background=os.environ.get("JEV_HEADLESS_BROWSER") != "1", _response_timeout=15)["targetId"]
        with self.tab_lock:
            below = (self.target, self.session)
            try:
                session = cdp("Target.attachToTarget", targetId=target, flatten=True, _response_timeout=15)["sessionId"]
                self.openers.append(below)
                self.target, self.session = target, session
                self.popups.add(target)
                self._setup_session()  # privacy before the document loads, unlike a tab Chrome opened itself
                self.navigate(url)
            except Exception:
                # A download link or a failed navigation: leave the opener active, as if the click never happened.
                if self.openers and self.openers[-1] == below:
                    self.openers.pop()
                self.target, self.session = below
                try:
                    cdp("Target.closeTarget", targetId=target, _response_timeout=15)
                except (RuntimeError, TimeoutError):
                    pass
                raise

    @timed("capture")
    def capture(self):
        return cdp("Page.captureScreenshot", session_id=self.session, _response_timeout=2,
                   format="jpeg", quality=72).get("data")

    @timed("preview_guard")
    def preview_fresh(self, page):
        if not self.fresh(page):
            return False
        targets = {action["node"]: action["rect"] for action in page["actions"] if "rect" in action}
        return self.evaluate("""(targets => Object.entries(targets).every(([id,expected]) => {
          const node=window.__jevFast?.nodes.get(Number(id));
          if (!node?.isConnected) return false;
          const r=node.getBoundingClientRect();
          return r.x===expected.x && r.y===expected.y && r.width===expected.w && r.height===expected.h;
        }))(""" + json.dumps(targets) + ")") is True

    def close(self):
        targets = [target for target in [*(target for target, _ in getattr(self, "openers", [])), self.target]
                   if target]  # never None: every page without an opener would match it
        self.openers, self.target = [], None
        if not targets:
            return
        try:
            # Include tabs our tabs opened but we never followed (an action interrupted mid-click).
            pages = [info for info in cdp("Target.getTargets", _response_timeout=15)["targetInfos"]
                     if info["type"] == "page"]
            for info in pages * len(pages):  # repeated passes reach pop-ups opened by pop-ups
                if info.get("openerId") in targets and info["targetId"] not in targets:
                    targets.append(info["targetId"])
        except (RuntimeError, TimeoutError, KeyError):
            pass
        for target in reversed(targets):
            try:
                cdp("Target.closeTarget", targetId=target, _response_timeout=15)
            except RuntimeError:
                pass  # a pop-up may already have closed itself

    def screencast(self):
        """(frame, number) for the active tab: Chrome pushes a JPEG whenever the page repaints, which is smoother
        and cheaper than polling Page.captureScreenshot. The number changes only when a new frame arrived."""
        session = self.session  # one read: a tab switch on another thread must not split start and bookkeeping
        if getattr(self, "screencasting", None) != session:
            self.stop_screencast()
            cdp("Page.startScreencast", session_id=session, _response_timeout=15,
                format="jpeg", quality=72, everyNthFrame=1)
            self.screencasting, self.frame, self.frames = session, None, getattr(self, "frames", 0)
        # ponytail: drain_events() empties the daemon's shared queue; the screencast is its only reader in Jev.
        acks = []
        for event in drain_events():
            if event["method"] == "Page.screencastFrame" and event.get("session_id") == session:
                acks.append(event["params"]["sessionId"])
                self.frame, self.frames = event["params"]["data"], self.frames + 1
        if acks:
            # Each acknowledgement is a ~70 ms round trip and Chrome only needs it eventually: never wait for it.
            if getattr(self, "acker", None) is None:
                self.acker = ThreadPoolExecutor(max_workers=1)
            self.acker.submit(self._ack, session, acks)
        return self.frame, self.frames

    @staticmethod
    def _ack(session, ids):
        for number in ids:
            try:
                cdp("Page.screencastFrameAck", session_id=session, sessionId=number, _response_timeout=5)
            except RuntimeError as error:
                if session_lost(error):
                    return  # the tab closed
            except TimeoutError:
                continue  # one slow acknowledgement must not stall the rest of the batch

    def stop_screencast(self):
        session, self.screencasting, self.frame = getattr(self, "screencasting", None), None, None
        if session:
            try:
                cdp("Page.stopScreencast", session_id=session, _response_timeout=5)
            except (RuntimeError, TimeoutError):
                pass  # the tab closed or navigated away; nothing is streaming anymore

    def manual_context(self, follow=True):
        if follow:
            self.follow_tabs()  # a click under manual control may open a sign-in pop-up
        return self.evaluate("(() => { window.__jevPrivacy?.collect(); const p=window.__jevPrivacy; "
                             "return {document:String(performance.timeOrigin)+':'+(p?.locationVersion() || 0),"
                             "w:innerWidth,h:innerHeight,url:p ? p.url(location.href) : location.href,"
                             "title:p ? p.scrub(document.title) : document.title,"
                             "protected:p?.protected() || false}; })()")

    def manual_mode(self, active):
        self.evaluate(f"window.__jevPrivacy.manual={json.dumps(active)}")
        with self.tab_lock:
            below = list(self.openers)
        for _, session in below:  # a tab we left must not stay in manual mode (its typing would be redacted)
            try:
                cdp("Runtime.evaluate", session_id=session, _response_timeout=15,
                    expression=f"window.__jevPrivacy&&(window.__jevPrivacy.manual={json.dumps(active)})")
            except (RuntimeError, TimeoutError):
                pass

    def protect_manual_text(self, text, group):
        # Chrome retains the redaction data across navigation; Python retains no values.
        source = f"window.__jevPrivacy.remember({json.dumps(text)},{json.dumps(group)});"
        self._remember_everywhere(source)
        self.evaluate(source + "window.__jevPrivacy.protect();")

    def _remember_everywhere(self, source):
        """Every owned tab redacts text typed in any of them, now and after it navigates."""
        with self.tab_lock:
            tabs = [*self.openers, (self.target, self.session)]
        for _, session in tabs:
            active = session == self.session
            try:
                cdp("Page.addScriptToEvaluateOnNewDocument", session_id=session, source=source, _response_timeout=15)
                if not active:
                    cdp("Runtime.evaluate", session_id=session, expression=source, _response_timeout=15)
            except RuntimeError as error:
                if active:
                    raise
                if session_lost(error):  # a middle tab closed while another popup was on top of it
                    with self.tab_lock:
                        self.openers = [entry for entry in self.openers if entry[1] != session]
                # otherwise that tab is navigating; the registered script covers its next document

    def manual_event(self, event, group):
        kind = event["type"]
        if kind == "text":
            self.protect_manual_text(event["text"], group)
            self.call("Input.insertText", text=event["text"])
        elif kind == "key":
            if event["key"] == "Backspace" and event["action"] == "down":
                source = f"window.__jevPrivacy.backspace({json.dumps(group)});"
                self._remember_everywhere(source)
                self.evaluate(source)
            self.evaluate("window.__jevPrivacy.protect()")
            codes = {"Enter":13,"Tab":9,"Backspace":8,"Escape":27,"Delete":46,
                     "ArrowLeft":37,"ArrowUp":38,"ArrowRight":39,"ArrowDown":40,
                     "Home":36,"End":35,"PageUp":33,"PageDown":34,"Shift":16,"Control":17,"Alt":18,"Meta":91}
            code = codes.get(event["key"], ord(event["key"].upper()) if len(event["key"]) == 1 else 0)
            self.call("Input.dispatchKeyEvent", type="keyDown" if event["action"] == "down" else "keyUp",
                      key=event["key"], windowsVirtualKeyCode=code, modifiers=event["modifiers"])
        else:
            self.call("Input.dispatchMouseEvent", type={"down":"mousePressed","up":"mouseReleased",
                      "move":"mouseMoved","wheel":"mouseWheel"}[event["action"]],
                      x=event["x"], y=event["y"], modifiers=event["modifiers"],
                      **({"deltaX":event["deltaX"],"deltaY":event["deltaY"]} if kind == "wheel" else
                         {"button":"left" if event["action"] != "move" else "none",
                          "buttons":event["buttons"],"clickCount":event["clickCount"]}))


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        try:
            result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        except RuntimeError as error:
            # Reads may retry after a document swap; mutations never do.
            if operation == "observe" and navigating(error):
                raise StalePage("Document changed during evaluation") from None
            raise
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            width, height = evaluate("[innerWidth, innerHeight]")
            call("Input.dispatchMouseEvent", type="mouseWheel", x=width / 2, y=height / 2,
                 deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              // A no-opener link to a new tab: headless Chrome holds the click for seconds while it opens.
              const tab=action.kind==='click' && e.tagName==='A' && /^https?:/.test(e.href) && e.target &&
                !['_self','_parent','_top'].includes(e.target.toLowerCase()) && !/(^|\\s)opener(\\s|$)/i.test(e.rel);
              return {x,y,open:tab ? e.href : null};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if target.get("open"):
                return {"executed": action["id"], "open": target["open"]}
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "press_enter":
                    call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter",
                         windowsVirtualKeyCode=13, text="\r")
                    call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter",
                         windowsVirtualKeyCode=13)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        try:
            screenshot = call("Page.captureScreenshot", format="jpeg", quality=72).get("data")
            if screenshot:
                info["screenshot"] = screenshot
        except (RuntimeError, TimeoutError):
            # The image is optional; the structured observation remains usable.
            pass
    return info
