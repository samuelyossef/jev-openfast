"""Real local-browser manual input and privacy checks. No provider calls."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jev_ultrafast import agent, assistant, model
from jev_ultrafast.chat import ChatSession
from jev_ultrafast.conversations import ConversationStore

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = "manual-frame.html" if self.path == "/frame" else "manual.html"
        body = (FIXTURES / name).read_text(encoding="utf-8").replace(
            "__FRAME_ORIGIN__", f"http://localhost:{self.server.server_port}").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    requests = []
    def ask(kind, context, calls):
        requests.append(context)
        calls.append({"kind": kind, "model": "offline", "status": "mocked"})
        if kind == "request":
            return {"intent": "task", "url": "-", "reply": "Verificando a página local.", "task": context["goal"]}
        if kind == "blocker":
            return {"kind": "human", "reason": "LOGIN", "description": "A página exige autenticação.",
                    "evidence": ["Senha"]}
        if kind == "verify":
            return {"satisfied": True, "checks": [{"requirement": "Entrar", "status": "confirmed",
                    "evidence": ["Autenticação concluída"], "reason": "Estado atual observado."}],
                    "reply": "Autenticação confirmada."}
        raise AssertionError("Unexpected helper")
    assistant.ask = ask
    model.CLIENT.post = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Paid APIs forbidden"))
    agent.choose = lambda *_a, **_kw: {"choice": "BLOCKED", "operation": "BLOCKED", "target": None,
                                    "latency_ms": 0, "usage": {}, "confidence": 1, "probabilities": {"BLOCKED": 1}}
    store = ConversationStore(Path("artifacts/manual-check.sqlite3"))
    session = ChatSession(url, on_change=store.save)
    passed = []
    secrets = ["manual_user_9Z", "private_pass_X9!", "7394", "nota protegida ç"]
    try:
        session.message({"message": "Entre na página local", "message_id": "manual-check"})
        session.worker.join(5)
        assert session.phase == "paused" and 2 <= len(requests) <= 3, (session.phase, len(requests), session.progress)
        before_manual = len(requests)
        passed.append("BLOCKED requests intervention only after current login evidence is assessed")
        owner = session.manual.start({})
        browser = session.agent.browser
        def frame():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                capture = session.preview.snapshot()["capture"]
                if capture and capture.get("manual") and capture["context"] == session.manual.context:
                    return capture
                time.sleep(0.02)
            raise AssertionError("No current manual image")
        def emit(event):
            capture = frame()
            session.manual.input({**event, "owner_token": owner, "request_id": session.manual.sequence + 1,
                                  "context": capture["context"]})
        def point(selector):
            return browser.evaluate("(selector=>{const r=document.querySelector(selector).getBoundingClientRect();"
                                    "return {x:r.x+r.width/2,y:r.y+r.height/2}})(" + json.dumps(selector) + ")")
        def click(position, count=1):
            emit({"type": "pointer", "action": "down", **position, "buttons": 1, "clickCount": count})
            emit({"type": "pointer", "action": "up", **position, "buttons": 0, "clickCount": count})
        def text(value):
            emit({"type": "text", "text": value})
        def key(value, modifiers=0):
            for action in ("down", "up"):
                emit({"type": "key", "key": value, "action": action, "modifiers": modifiers})
        frame()
        deadline = time.monotonic() + 5
        while browser.evaluate("(() => {try {return !!document.querySelector('iframe').contentWindow.document} "
                               "catch {return false}})()") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert browser.evaluate("(() => {try {return !!document.querySelector('iframe').contentWindow.document} "
                                "catch {return false}})()") is False
        click(point("#user"))
        text(secrets[0])
        key("Tab")
        text(secrets[1])
        click(point("#digit0"))
        for digit in secrets[2]:
            text(digit)
        assert browser.evaluate("[...document.querySelectorAll('.otp')].every(e=>e.value.length===1)")
        click(point("#note"))
        text(secrets[3])
        for value in ("ArrowLeft", "Home", "End", "Backspace"):
            key(value)
        text("ç")
        click(point("#double"))
        click(point("#double"), 2)
        assert browser.evaluate("window.counts.double") == 1
        passed.append("text, paste/IME insertText, editing keys, double click and auto-advance OTP")
        rect = browser.evaluate("(()=>{const r=document.querySelector('iframe').getBoundingClientRect();"
                                "return {x:r.x+1,y:r.y+1}})()")
        click({"x": rect["x"] + 19, "y": rect["y"] + 20})
        start, end = {"x": rect["x"] + 40, "y": rect["y"] + 90}, {"x": rect["x"] + 270, "y": rect["y"] + 90}
        emit({"type": "pointer", "action": "down", **start, "buttons": 1})
        emit({"type": "pointer", "action": "move", **end, "buttons": 1})
        emit({"type": "pointer", "action": "up", **end, "buttons": 0})
        time.sleep(0.1)
        assert browser.evaluate("window.counts.captcha && window.counts.drag")
        passed.append("cross-origin simulated CAPTCHA and canvas drag use the same CDP tab")
        click(point("#login"))
        assert browser.evaluate("document.querySelector('#status').textContent") == "Autenticação concluída"
        emit({"type": "wheel", "action": "wheel", "x": 900, "y": 500, "deltaY": 800})
        time.sleep(0.1)
        assert browser.evaluate("scrollY") > 0
        emit({"type": "wheel", "action": "wheel", "x": 900, "y": 500, "deltaY": -1000})
        time.sleep(0.1)
        page = browser.observe(screenshot=False)
        rendered = json.dumps(page, ensure_ascii=False)
        assert all(secret not in rendered for secret in secrets), "Protected observation leaked"
        assert page["protected"] and any(a.get("value", "").startswith("[preenchido") for a in page["actions"])
        assert not any(a["node"] in [page["page_key"][6][i][0] for i in (1, 2, 3, 4, 5)]
                       for a in page["actions"] if "node" in a)
        passed.append("protected values redacted in text, guards, actions and opaque field versions")
        assert len(requests) == before_manual, session.messages[-1]
        session.manual.end({"owner_token": owner, "resume": False})
        assert session.phase == "paused" and len(requests) == before_manual
        agent.choose = lambda *_a, **_kw: {"choice": "DONE", "operation": "DONE", "target": None,
                                         "latency_ms": 0, "usage": {}, "confidence": 1, "probabilities": {"DONE": 1}}
        session.resume()
        session.worker.join(5)
        assert session.phase == "completed" and session.messages[-1]["verification"]["satisfied"]
        exported = json.dumps({"full": session.snapshot(), "compact": session.snapshot(compact=True),
                               "model_requests": requests, "stored": store.load(session.id)}, ensure_ascii=False)
        assert all(secret not in exported for secret in secrets)
        assert "screenshot" not in session.snapshot()["page"]
        passed.append("no model calls during manual control; fresh verification after resume; safe storage/export")
        old = browser.manual_context()
        browser.evaluate("history.pushState({},'', '?code=secret-auth-token&next=ok')")
        assert browser.manual_context()["document"] != old["document"]
        assert "secret-auth-token" not in json.dumps(browser.observe(screenshot=False))
        passed.append("SPA navigation invalidates old frames and auth URL values are protected")
    finally:
        session.manual.close()
        session.preview.close()
        session.agent.close()
        server.shutdown()
        server.server_close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} real-browser checks; no paid APIs")


if __name__ == "__main__":
    main()
