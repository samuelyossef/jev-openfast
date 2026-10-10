"""Real local-page intervention scenarios with every model transport replaced offline."""

import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jev_ultrafast import agent, assistant, demo, model
from jev_ultrafast.browser import Browser
from jev_ultrafast.chat import ChatSession
from jev_ultrafast.conversations import ConversationStore

FORM = """<!doctype html><meta charset=utf-8><title>Pesquisa local</title>
<label>Cidade <input aria-label="Cidade" id="city"></label>
<button onclick="document.querySelector('#result').textContent='Resultados para '+
document.querySelector('#city').value">Pesquisar</button><p id="result"></p>"""
PAGES = {"/": FORM, "/optional": """<!doctype html><title>Login opcional</title>
<p>Entrar é opcional</p><label>Senha <input type=password autocomplete=current-password></label>
<iframe title="Publicidade" src="/frame"></iframe>
<button onclick="document.body.innerHTML='<h1>Catálogo disponível</h1>'">Continuar como visitante</button>""",
         "/frame": "<!doctype html><title>Publicidade</title><p>Anúncio</p>",
         "/captcha": "<!doctype html><title>Desafio local</title><h1>CAPTCHA obrigatório</h1>",
         "/fields": """<!doctype html><title>Tipos de campos</title>
<label>Código postal <input id="postal_code" autocomplete="postal-code"></label>
<label>Código de desconto <input id="code"></label>
<form><label>E-mail de login <input type=email autocomplete=username value="secret-login@example.org"></label>
<label>Senha <input type=password value="secret-pass-local"></label></form>
<label>Cartão <input autocomplete=cc-number value="secret-card-local"></label>
<label>CPF <input id=cpf value="secret-document-local"></label>
<label>Documento <input type=file></label>
<label>Código de verificação <input autocomplete=one-time-code value="secret-code-local"></label>"""}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        content = PAGES.get(self.path, FORM).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_args):
        pass


def check_ui(root, scenario, passed):
    """Serve the rebuilt UI against an isolated conversation and offline helpers."""
    with tempfile.TemporaryDirectory(prefix="jev-intervention-") as folder:
        demo.STORE = ConversationStore(Path(folder) / "history.sqlite3")
        server = ThreadingHTTPServer(("127.0.0.1", 0), demo.Handler)
        demo.PORT = server.server_port
        demo.ORIGIN = f"http://127.0.0.1:{demo.PORT}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        scenario.update(mode="city", calls=0)
        session = ChatSession(root, on_change=demo.STORE.save)
        demo.SESSION = session
        session.message({"message": "Pesquisar hotéis na cidade que eu informar", "message_id": "ui-question"})
        session.worker.join(20)
        assert session.phase == "awaiting_input", session.progress
        browser = Browser(demo.ORIGIN)

        def wait(expression):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if browser.evaluate(expression):
                    return
                time.sleep(0.05)
            raise AssertionError(f"UI condition not met: {expression}")

        try:
            for locale, title, cancel in [
                ("pt-BR", "Jev precisa de uma informação", "Cancelar pergunta"),
                ("en", "Jev needs some information", "Cancel question"),
                ("es", "Jev necesita un dato", "Cancelar pregunta"),
                ("fr", "Jev a besoin d’une information", "Annuler la question"),
            ]:
                browser.evaluate(f"localStorage.setItem('jev.locale.v1',{json.dumps(locale)});location.reload()")
                wait(f"document.body.innerText.includes({json.dumps(title)})")
                assert browser.evaluate(f"[...document.querySelectorAll('button')].some(b=>"
                                        f"b.textContent==={json.dumps(cancel)} && !b.disabled)")
                assert not browser.evaluate("document.querySelector('.handoff-card')!==null")
            passed.append("PASS rebuilt UI: ordinary question and cancel button in all four languages")
            browser.evaluate("""(()=>{const e=document.querySelector('textarea');
              Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,'Belo Horizonte');
              e.dispatchEvent(new Event('input',{bubbles:true}));})()""")
            wait("!!document.querySelector('form.composer button[type=submit]:not(:disabled)')")
            browser.evaluate("document.querySelector('form.composer').requestSubmit()")
            wait("document.body.innerText.includes('Resultados para Belo Horizonte')")
            assert session.phase == "completed" and session.goal == "Pesquisar hotéis na cidade que eu informar"
            passed.append("PASS rebuilt UI: chat reply resumes the original task and displays verified result")
            session.close()
            scenario.update(mode="city", calls=0)
            session = ChatSession(root, on_change=demo.STORE.save)
            demo.SESSION = session
            session.message({"message": "Pesquisar hotéis na cidade que eu informar", "message_id": "ui-cancel"})
            session.worker.join(20)
            assert session.phase == "awaiting_input"
            browser.evaluate("location.reload()")
            wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='Annuler la question'&&!b.disabled)")
            browser.evaluate("[...document.querySelectorAll('button')].find(b=>"
                             "b.textContent==='Annuler la question').click()")
            wait("![...document.querySelectorAll('button')].some(b=>b.textContent==='Annuler la question')")
            assert session.phase == "paused" and session.pending_input is None
            assert not session.agent.state["history"]
            passed.append("PASS rebuilt UI: cancel clears the pending question without browser input")
        finally:
            browser.close()
            session.close()
            server.shutdown()
            server.server_close()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{server.server_port}"
    scenario = {"mode": "city", "calls": 0}
    passed = []

    def ask(kind, context, calls):
        calls.append({"kind": kind, "model": "offline", "status": "mocked", "attempts": 0})
        if kind == "request":
            return {"intent": "task", "url": "-", "reply": "Verificando a página local.", "task": context["goal"]}
        if kind == "safety":
            return {"effect": "safe", "description": "Pesquisa local"}
        if kind == "blocker":
            if scenario["mode"] == "captcha":
                return {"kind": "human", "reason": "CAPTCHA", "description": "Resolva o desafio",
                        "evidence": ["CAPTCHA obrigatório"]}
            if context.get("missing_field"):
                return {"kind": "input", "reason": "OTHER", "description": "Qual cidade?", "evidence": ["Cidade"]}
            return {"kind": "recover", "reason": "OTHER", "description": "Continue como visitante", "evidence": []}
        if kind == "verify":
            quote = "Catálogo disponível" if scenario["mode"] == "optional" else "Resultados para Belo Horizonte"
            return {"satisfied": True, "reply": quote, "checks": [{"requirement": context["goal"],
                    "status": "confirmed", "evidence": [quote], "reason": "Resultado atual observado"}]}
        raise AssertionError(f"Unexpected helper {kind}")

    def choose(page, goal, history, **_kwargs):
        scenario["calls"] += 1
        mode = scenario["mode"]
        action = None
        operation, reason = "DONE", None
        if mode in {"captcha", "stuck"} or (mode == "optional" and scenario["calls"] == 1):
            operation = "BLOCKED"
            reason = {"captcha": "CAPTCHA", "optional": "LOGIN"}.get(mode, "OTHER")
        elif mode == "optional" and "Catálogo disponível" not in page["text"]:
            action = next(a for a in page["actions"] if a["label"] == "Continuar como visitante")
        elif mode == "city" and "Resultados para Belo Horizonte" not in page["text"]:
            field = next(a for a in page["actions"] if a["kind"] == "fill" and a["label"] == "Cidade")
            action = field if field.get("value") != "Belo Horizonte" else next(
                a for a in page["actions"] if a["kind"] == "click" and a["label"] == "Pesquisar")
        if action:
            operation = "TYPE_TEXT" if action["kind"] == "fill" else "CLICK"
        choice = action["id"] if action else operation
        return {"choice": choice, "operation": operation, "target": None, "blocked_reason": reason,
                "latency_ms": 0, "usage": {}, "confidence": 1, "probabilities": {choice: 1}}

    def text(context):
        if "Belo Horizonte" not in json.dumps(context, ensure_ascii=False):
            raise model.MissingValue("Missing ordinary city")
        return "Belo Horizonte", {"model": "offline", "latency_ms": 0}

    assistant.ask, agent.choose, agent.field_text = ask, choose, text
    model.CLIENT.post = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Paid APIs forbidden"))
    try:
        fields = Browser(root + "/fields")
        try:
            observation = fields.observe(screenshot=False)
            ordinary = {a["label"] for a in observation["actions"] if a["kind"] == "fill"}
            assert {"Código postal", "Código de desconto"} <= ordinary
            assert not {"E-mail de login", "Senha", "Cartão", "CPF", "Documento", "Código de verificação"} & ordinary
            assert all(value not in json.dumps(observation) for value in
                       ["secret-login@example.org", "secret-pass-local", "secret-card-local",
                        "secret-document-local", "secret-code-local"])
            passed.append("PASS real snapshot: postal/coupon codes are ordinary; "
                          "credentials/payment/identity are protected")
        finally:
            fields.close()
        for mode, path, goal in [("city", "/", "Pesquisar hotéis em Belo Horizonte"),
                                 ("city", "/", "Pesquisar hotéis na cidade que eu informar"),
                                 ("optional", "/optional", "Abrir o catálogo"),
                                 ("captcha", "/captcha", "Acessar o conteúdo"),
                                 ("stuck", "/", "Abrir um resultado indisponível")]:
            scenario.update(mode=mode, calls=0)
            session = ChatSession(root + path)
            try:
                session.message({"message": goal, "message_id": "local-scenario"})
                session.worker.join(20)
                assert not session.worker.is_alive(), "Scenario did not stop"
                if session.phase == "awaiting_input":
                    request = session.pending_input["id"]
                    session.agent.browser.evaluate(
                        "(()=>{const e=document.querySelector('#city');e.replaceWith(e.cloneNode(true))})()")
                    session.input({"request_id": request, "value": "Belo Horizonte"})
                    session.worker.join(20)
                    assert session.goal == goal
                    session.input({"request_id": request, "value": "Belo Horizonte"})
                expected = "paused" if mode in {"captcha", "stuck"} else "completed"
                assert session.phase == expected, (mode, session.phase, session.progress)
                handoff = any(m.get("kind") == "handoff" for m in session.messages)
                assert handoff == (mode == "captcha")
                if expected == "completed":
                    assert session.messages[-1]["verification"]["satisfied"]
                passed.append(f"PASS {goal}: {session.phase}, handoff={handoff}, "
                              f"decisions={len(session.agent.state['decisions'])}, helpers={len(session.calls)}")
            finally:
                session.close()
        check_ui(root, scenario, passed)
    finally:
        server.shutdown()
        server.server_close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} real local-page scenarios; no paid APIs")


if __name__ == "__main__":
    main()
