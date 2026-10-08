"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from .timing import timed

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
PRIVACY = Path(__file__).with_name("privacy.js").read_text(encoding="utf-8")
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


def navigating(error):
    """CDP reports a document swap mid-evaluation as a protocol error, not a JS exception."""
    message = str(error).lower()
    return "context" in message or "navigat" in message


def validate_viewport(width, height):
    if any(type(value) is not int or not 1 <= value <= 4096 for value in (width, height)):
        raise ValueError("A largura e a altura devem ser inteiros entre 1 e 4096 pixels.")
    return width, height


class Browser:
    @timed("startup")
    def __init__(self, url, *, viewport=None):
        width, height = validate_viewport(*(viewport or (1120, 780)))
        ensure_daemon()
        self.target = None
        try:
            self.target = cdp(
                "Target.createTarget", url="about:blank",
                background=os.environ.get("JEV_HEADLESS_BROWSER") != "1", _response_timeout=15,
            )["targetId"]
            self.session = cdp(
                "Target.attachToTarget", targetId=self.target, flatten=True, _response_timeout=15
            )["sessionId"]
            self.set_viewport(width, height)
            self.call("Page.enable")
            self.call("Page.addScriptToEvaluateOnNewDocument", source=PRIVACY, runImmediately=True)
            # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            self.navigate(url)
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def set_viewport(self, width, height):
        width, height = validate_viewport(width, height)
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)

    @timed("navigation")
    def navigate(self, url):
        """Navigate the owned tab once; callers record the request before observing."""
        result = self.call("Page.navigate", url=url)
        if result.get("errorText"):
            raise RuntimeError(f"Navegação falhou: {result['errorText']}")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                # DOMContentLoaded includes deferred scripts, but does not wait for images.
                # The tab starts on about:blank, which is already complete; wait for the requested document.
                if self.evaluate("location.href!=='about:blank' && (document.readyState==='complete' || "
                                 "performance.getEntriesByType('navigation')[0]?.domContentLoadedEventEnd>0)"
                                 if url != "about:blank" else "document.readyState==='complete'"):
                    break
            except StalePage:
                pass
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, _response_timeout=15, **params)

    def evaluate(self, expression):
        try:
            response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        except RuntimeError as error:
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
        # Real navigations take longer than one frame; only a changing page pays for this wait.
        deadline = time.monotonic() + 5
        while True:
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
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
        return result

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
        if self.target:
            cdp("Target.closeTarget", targetId=self.target, _response_timeout=15)
            self.target = None

    def manual_context(self):
        return self.evaluate("(() => { window.__jevPrivacy?.collect(); const p=window.__jevPrivacy; "
                             "return {document:String(performance.timeOrigin)+':'+(p?.locationVersion() || 0),"
                             "w:innerWidth,h:innerHeight,url:p ? p.url(location.href) : location.href,"
                             "title:p ? p.scrub(document.title) : document.title,"
                             "protected:p?.protected() || false}; })()")

    def manual_mode(self, active):
        self.evaluate(f"window.__jevPrivacy.manual={json.dumps(active)}")

    def protect_manual_text(self, text, group):
        # Chrome retains the redaction data across navigation; Python retains no values.
        source = f"window.__jevPrivacy.remember({json.dumps(text)},{json.dumps(group)});"
        self.call("Page.addScriptToEvaluateOnNewDocument", source=source)
        self.evaluate(source + "window.__jevPrivacy.protect();")

    def manual_event(self, event, group):
        kind = event["type"]
        if kind == "text":
            self.protect_manual_text(event["text"], group)
            self.call("Input.insertText", text=event["text"])
        elif kind == "key":
            if event["key"] == "Backspace" and event["action"] == "down":
                source = f"window.__jevPrivacy.backspace({json.dumps(group)});"
                self.call("Page.addScriptToEvaluateOnNewDocument", source=source)
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
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
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
