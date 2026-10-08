"""Matched offline chat comparison. Providers are blocked; timings are simulated.

Freeze the previous package under artifacts/performance-baseline/jev_ultrafast before
editing. Run `uv run python scripts/benchmark_chat.py` to compare that copy and this
checkout. --real-browser uses only the local fixture, still with mocked models.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ("search", "navigation", "question", "publication", "dynamic", "slow_capture")
HELPER_DELAY = 0.08
TEXT_DELAY = 0.10
DECISION_DELAY = 0.015


def source_hashes(source):
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in (source / "jev_ultrafast").glob("*.py")}


class SimulatedBrowser:
    def __init__(self, url, *, viewport=None):
        self.case = url.rsplit("/", 1)[-1]
        self.mutations = []
        self.closed = False
        self.terminal_changed = False
        self.page = {"url": url, "title": "Offline benchmark", "text": "Pronta", "fingerprint": "initial",
                     "w": (viewport or (1120, 780))[0], "h": (viewport or (1120, 780))[1],
                     "actions": [
                         {"id": "input", "node": 1, "kind": "fill", "role": "textbox", "value": "",
                          "label": "Busca"},
                         {"id": "search", "node": 2, "kind": "click", "role": "button", "label": "Buscar"},
                         {"id": "publish", "node": 3, "kind": "click", "role": "button", "label": "Publicar"},
                         {"id": "link", "node": 4, "kind": "click", "role": "link", "label": "Artigo"},
                     ]}

    def observe(self, screenshot=True):
        time.sleep(0.005)
        page = copy.deepcopy(self.page)
        if screenshot:
            page["screenshot"] = self.capture()
        return page

    def capture(self):
        time.sleep(0.25 if self.case == "slow_capture" else 0.025)
        return "simulated-image"

    def fresh(self, page, action=None):
        return action is not None or page["fingerprint"] == self.page["fingerprint"]

    def act(self, action, page, text=None):
        self.mutations.append((action["kind"], action["label"], text))
        if action["kind"] == "fill":
            self.page["actions"][0]["value"] = text
            self.page["text"] = "Busca preenchida"
        else:
            self.page["text"] = {"Publicar": "Publicado", "Artigo": "Artigo aberto"}.get(
                action["label"], "Resultado: azul")
        self.page["fingerprint"] = str(len(self.mutations))

    def set_viewport(self, width, height):
        self.page.update(w=width, h=height)

    def navigate(self, url):
        self.page["url"] = url

    def close(self):
        self.closed = True


ARTICLE_LINK = ("""<a href="#article" onclick="document.querySelector('#result').textContent='Artigo aberto'">"""
                "Artigo</a>")
HTML = """<!doctype html><meta charset="utf-8"><title>Offline benchmark</title>
<h1>Fixture de desempenho</h1><p id="result">Pronta</p><p id="clock">Relógio</p>
<form onsubmit="event.preventDefault();document.querySelector('#result').textContent=
'Resultado: '+document.querySelector('input').value">
<label>Busca<input aria-label="Busca"></label><button>Buscar</button></form>
<button onclick="document.querySelector('#result').textContent='Publicado'">Publicar</button>
""" + ARTICLE_LINK + """
<script>let n=0;if(location.pathname.endsWith('/dynamic'))window.clock=setInterval(()=>
document.querySelector('#clock').textContent='Relógio '+(++n),70)</script>"""


def child(source, case, real_browser):
    sys.path.insert(0, str(source))
    if real_browser:
        port = Path(ROOT / "artifacts/chrome-profile/DevToolsActivePort").read_text().splitlines()[0]
        os.environ.update(BU_CDP_URL=f"http://127.0.0.1:{port}", BU_NAME="jev-demo", JEV_HEADLESS_BROWSER="1")
    from jev_ultrafast import agent, assistant, chat, model

    server = None
    if real_browser:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/article":
                    time.sleep(0.3)  # a slow server: the old document stays visible after the click
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(HTML.replace(">Pronta<", ">Artigo aberto<").encode())
                    return
                if self.path == "/delayed-script":
                    time.sleep(0.2)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript")
                    self.end_headers()
                    self.wfile.write(b"window.scriptReady=true;document.querySelector('#result').textContent='Pronta';")
                    return
                if self.path == "/delayed-image":
                    time.sleep(2)
                    self.send_response(200)
                    self.send_header("Content-Type", "image/svg+xml")
                    self.end_headers()
                    self.wfile.write(b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                html = HTML
                if case == "page_navigation":
                    html = html.replace(ARTICLE_LINK, '<a href="/article">Artigo</a>')
                if case == "slow_resource":
                    html += ('<script defer src="/delayed-script"></script>'
                             '<img width="1" height="1" src="/delayed-image" onload="window.assetLoaded=true">')
                self.wfile.write(html.encode())
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/{case}"
        if case == "slow_capture":
            from jev_ultrafast import browser
            original_cdp = browser.cdp
            def delayed_cdp(method, *args, **kwargs):
                if method == "Page.captureScreenshot":
                    time.sleep(0.25)
                return original_cdp(method, *args, **kwargs)
            browser.cdp = delayed_cdp
    else:
        agent.Browser = SimulatedBrowser
        url = f"https://fixture.invalid/{case}"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Paid provider calls are forbidden in this benchmark")
    model.CLIENT.post = forbidden
    model.openrouter_key = assistant.openrouter_key = lambda: "offline"

    calls, choices, generated, decision_pages = [], [], [], []
    terminal_changed = False

    def choose(page, goal, history, **_kwargs):
        nonlocal terminal_changed
        time.sleep(DECISION_DELAY)
        executed = [h for h in history if h["execution"] == "executed"]
        label = None
        if case in {"search", "dynamic", "slow_capture"}:
            label = "Busca" if not executed else "Buscar" if len(executed) == 1 else None
        elif case == "publication":
            label = "Publicar" if not executed else None
        elif case in {"navigation", "slow_resource", "page_navigation"}:
            label = "Artigo" if not executed else None
        action = next((a for a in page["actions"] if a["label"] == label
                       and (label != "Busca" or a["kind"] == "fill")), None)
        operation = {"fill": "TYPE_TEXT", "click": "CLICK"}.get(action["kind"]) if action else "DONE"
        selected = action["id"] if action else "DONE"
        _, targets, _ = model.action_space(page["actions"])
        target = next((k for k, a in targets.get(operation, {}).items() if a["id"] == selected), None)
        choices.append(operation)
        decision_pages.append(f"{page['url'].rsplit('/', 1)[-1]}: {page['text'][:40]}")
        if case == "dynamic" and selected == "DONE" and real_browser and not terminal_changed:
            session.agent.browser.evaluate("clearInterval(window.clock); "
                                           "document.querySelector('#clock').textContent='Atualização final'")
            terminal_changed = True
        if case == "dynamic" and selected == "DONE" and not real_browser:
            browser = session.agent.browser
            if not browser.terminal_changed:
                browser.page["fingerprint"] = "dynamic-update"
                browser.terminal_changed = True
        return {"choice": selected, "operation": operation, "target": target, "probabilities": {selected: 1},
                "confidence": 1, "latency_ms": round(DECISION_DELAY * 1000), "usage": {}}

    def post(_url, _key, body, *, on_attempt=None):
        if on_attempt:
            on_attempt()
        time.sleep(HELPER_DELAY)
        kind = body["response_format"]["json_schema"]["name"].removeprefix("chat_")
        context = json.loads(body["messages"][1]["content"])
        calls.append(kind)
        if kind in {"request", "route"}:
            explicit = re.search(r"https?://\S+", context["goal"])
            output = {"url": explicit.group() if explicit else "-", "reply": "Vou verificar."}
            if kind == "request":
                output["intent"] = "answer" if case == "question" else "task"
                output["task"] = context["goal"]
        elif kind in {"intent", "answer"}:
            output = {"reply": context["page"]["text"]}
            if kind == "intent":
                output["intent"] = "answer" if case == "question" else "task"
        elif kind == "safety":
            output = {"effect": "confirm" if context["action"]["label"] == "Publicar" else "safe",
                      "description": "Publicar conteúdo da fixture?"}
        elif kind == "verify":
            quote = {"publication": "Publicado", "navigation": "Artigo aberto",
                     "slow_resource": "Artigo aberto", "page_navigation": "Artigo aberto"}.get(case, "Resultado: azul")
            present = quote in context["page"]["text"]
            output = {"satisfied": present, "checks": [{"requirement": context["goal"],
                      "status": "confirmed" if present else "unknown", "evidence": [quote] if present else [],
                      "reason": "Verificação independente da fixture."}]}
            if "reply" in assistant.PROPERTIES["verify"]:
                output["reply"] = "Resultado confirmado na fixture."
        else:
            output = {"reply": "Resultado confirmado na fixture."}
        return {"choices": [{"message": {"content": json.dumps(output)}}], "usage": {}}

    def text(context):
        time.sleep(TEXT_DELAY)
        generated.append(context)
        return "azul", {"model": "offline", "latency_ms": round(TEXT_DELAY * 1000)}

    agent.choose, agent.field_text, assistant.post_json = choose, text, post
    session = chat.ChatSession()
    started = time.perf_counter()
    goals = {"question": "Qual é o texto da página", "publication": "Publique o conteúdo",
             "navigation": "Abra o artigo", "search": "Busque azul", "dynamic": "Busque azul",
             "slow_capture": "Busque azul", "slow_resource": "Abra o artigo", "page_navigation": "Abra o artigo"}
    session.message({"message": f"{goals[case]} em {url}", "message_id": "benchmark"})
    session.worker.join(15)
    if session.phase == "awaiting_confirmation":
        session.approve({"approval_id": session.pending["id"]})
        session.worker.join(15)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    if session.worker.is_alive():
        raise AssertionError("Benchmark worker exceeded its offline budget")
    view = session.snapshot()
    verified = (view["chat_status"] == "answered" if case == "question"
                else bool(view["messages"][-1].get("verification", {}).get("satisfied")))
    expected = {"question": "Pronta", "publication": "Publicado", "navigation": "Artigo aberto",
                "slow_resource": "Artigo aberto", "page_navigation": "Artigo aberto"}.get(
        case, "Resultado: azul")
    if real_browser:
        observed = session.agent.browser.evaluate(
            "({text:document.querySelector('#result').textContent,query:document.querySelector('input').value,"
            "readyState:document.readyState,assetLoaded:!!window.assetLoaded,scriptReady:!!window.scriptReady})")
        independent = observed["text"] == expected
        if case in {"slow_resource", "page_navigation"}:
            independent = independent and (case != "slow_resource" or observed["scriptReady"]) and sum(
                h["execution"] == "executed" for h in view["history"]) == 1
    else:
        observed = session.agent.browser.page["text"]
        independent = observed == expected
    result = {"case": case, "elapsed_ms": elapsed, "verified": verified, "independent": independent,
              "helper_calls": calls, "decisions": choices, "decision_pages": decision_pages,
              "text_calls": len(generated),
              "actions": [{k: h[k] for k in ("kind", "action", "text", "execution")} for h in view.get("history", [])],
              "timing": view.get("timing"), "observed": observed}
    session.close()
    if server:
        server.shutdown()
        server.server_close()
    print(json.dumps(result, ensure_ascii=True))


def main():
    global HELPER_DELAY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--case", choices=(*CASES, "slow_resource", "page_navigation"))
    parser.add_argument("--baseline", type=Path, default=ROOT / "artifacts/performance-baseline")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--real-browser", action="store_true")
    parser.add_argument("--helper-delay", type=float, default=HELPER_DELAY,
                        help="simulated latency of each text-helper call, in seconds")
    args = parser.parse_args()
    if args.case in {"slow_resource", "page_navigation"} and not args.real_browser:
        parser.error(f"{args.case} requires --real-browser")
    HELPER_DELAY = args.helper_delay
    if args.source:
        return child(args.source.resolve(), args.case, args.real_browser)
    baseline = args.baseline.resolve()
    if not (baseline / "jev_ultrafast/chat.py").is_file():
        parser.error("Freeze the previous package under artifacts/performance-baseline first")
    initial_hashes = {"before": source_hashes(baseline), "after": source_hashes(ROOT)}
    runs = []
    cases = (args.case,) if args.case else CASES
    for case in cases:
        for repeat in range(args.repeats):
            for arm, source in (("before", baseline), ("after", ROOT)):
                cmd = [sys.executable, str(Path(__file__).resolve()), "--source", str(source), "--case", case]
                if args.real_browser:
                    cmd.append("--real-browser")
                cmd += ["--helper-delay", str(HELPER_DELAY)]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=40, check=True)
                run = {"arm": arm, "repeat": repeat + 1, **json.loads(result.stdout)}
                runs.append(run)
                print(f"{case} {arm} {run['elapsed_ms']} ms "
                      f"verified={run['verified']} independent={run['independent']}", flush=True)
    summary = []
    for case in cases:
        before = statistics.median(r["elapsed_ms"] for r in runs if r["case"] == case and r["arm"] == "before")
        after = statistics.median(r["elapsed_ms"] for r in runs if r["case"] == case and r["arm"] == "after")
        summary.append({"case": case, "before_ms": before, "after_ms": after,
                        "reduction_percent": round(100 * (1 - after / before), 1)})
    hashes = {"before": source_hashes(baseline), "after": source_hashes(ROOT)}
    report = {"models": "simulated; paid APIs blocked", "real_browser": args.real_browser,
              "delays_ms": {"helper": HELPER_DELAY * 1000, "text": TEXT_DELAY * 1000,
                            "decision": DECISION_DELAY * 1000, "slow_capture_extra": 250},
              "summary": summary, "runs": runs, "source_hashes": hashes,
              "source_stable": initial_hashes == hashes}
    output = args.output or ROOT / "artifacts" / (
        "chat-speed-real-browser.json" if args.real_browser else "chat-speed-simulated.json")
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    archive = ROOT / "artifacts/chat-speed-runs"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{time.time_ns()}-{output.name}").write_text(serialized, encoding="utf-8")
    output.write_text(serialized, encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if not report["source_stable"]:
        raise SystemExit("Source changed during comparison; repeat before reporting timings.")
    if not all(r["verified"] and r["independent"] for r in runs):
        raise SystemExit("Some outcomes were unconfirmed; inspect the retained raw attempts.")


if __name__ == "__main__":
    main()
