"""One in-memory conversation and one owned browser tab, with a server-owned worker."""

import copy
import ipaddress
import json
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from . import assistant
from .agent import Agent, open_blank
from .browser import NoHistory, StalePage, validate_viewport
from .manual import Manual
from .model import MissingValue
from .preview import Preview
from .questions import MAX_STEPS
from .timing import Timings, timed

LANGUAGES = {"pt-BR": "Brazilian Portuguese", "en": "English", "es": "Spanish", "fr": "French"}


def validate_locale(value):
    if value is None:
        return "pt-BR"
    if value not in LANGUAGES:
        raise ValueError("Idioma não suportado.")
    return value


def validate_navigation(body):
    """(action, url) of an address-bar request, validated before anything is created or recorded."""
    action = body.get("action")
    if action not in {"url", "back", "forward", "reload"}:
        raise ValueError("Navegação desconhecida.")
    return action, validate_url(body.get("url")) if action == "url" else None


def validate_url(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("Informe uma URL http ou https válida.")
    value = value.strip()
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("A URL não pode conter espaços ou caracteres de controle.")
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Use uma URL http ou https sem credenciais embutidas.")
    return value


def routed_url(value, goal):
    """Reject model-inferred local destinations unless the user supplied their URL."""
    url = validate_url(value)
    host = urlsplit(url).hostname
    try:
        local = not ipaddress.ip_address(host).is_global
    except ValueError:
        local = host == "localhost" or host.endswith(".localhost")
    if local and url not in goal:
        raise ValueError("Informe a URL local completa para abrir esse destino.")
    return url


def validate_message(body):
    value, message_id = body.get("message"), body.get("message_id")
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2000:
        raise ValueError("Envie uma mensagem de 1 a 2.000 caracteres.")
    if not isinstance(message_id, str) or not 1 <= len(message_id) <= 80:
        raise ValueError("Identificador da mensagem inválido.")
    return value.strip(), message_id



# Portuguese fallback; the interface translates by code.
HANDOFF = {
    "LOGIN": "Esta página pede login ou senha. Assuma o controle, entre na conta e escolha Continuar com Jev.",
    "CAPTCHA": "Apareceu uma verificação humana (CAPTCHA). Assuma o controle, resolva e escolha Continuar com Jev.",
    "VERIFICATION_CODE": "A página pede um código de verificação. Assuma o controle, informe o código e escolha "
    "Continuar com Jev.",
    "PERSONAL_DATA": "Preciso de uma informação que só você pode fornecer. Assuma o controle, preencha e escolha "
    "Continuar com Jev.",
    "STUCK": "Não estou conseguindo avançar nesta página. Assuma o controle, resolva o bloqueio e escolha "
    "Continuar com Jev.",
    "OTHER": "Preciso da sua ajuda nesta página. Assuma o controle, resolva o bloqueio e escolha Continuar com Jev.",
}

# Reasons only the user can resolve; any other BLOCKED is reconsidered once per turn.
HUMAN_ONLY = {"LOGIN", "CAPTCHA", "VERIFICATION_CODE", "PERSONAL_DATA"}
RECONSIDER_BLOCKED = {
    "requirement": "Make progress without the user when the page allows it",
    "reason": "BLOCKED was chosen without a login, CAPTCHA, verification code or personal data. Use a visible "
              "control that continues, dismisses, closes or retries. Choose BLOCKED again only if none can.",
}


def frozen(view):
    """Deep copy of a view. The decision log is shared: entries are never changed after they are appended, and each
    carries the whole request (page text, element table), so copying them made every publish slower per step."""
    copied = copy.deepcopy({key: value for key, value in view.items() if key != "decisions"})
    if "decisions" in view:
        copied["decisions"] = list(view["decisions"])
    return copied


class ChatSession:
    def __init__(self, url=None, on_change=None, viewport=None):
        self.id = secrets.token_urlsafe(18)
        self.locale = "pt-BR"
        self.on_change = on_change
        self.persisted = None
        self.storage_error = None
        self.last_url = None
        self.viewport = validate_viewport(*viewport) if viewport is not None else None
        self.pending_viewport = None
        self.viewport_error = None
        self.viewport_events = []
        self.browser_busy = False
        self.timings = Timings()
        self.turn_started = None
        self.turn_finished = None
        self.approval_started = None
        self.approval_wait_ms = 0
        self.preview = Preview(self.id)
        self.agent = (
            Agent(validate_url(url), None, screenshots=False, viewport=self.viewport) if url is not None else None
        )
        if self.agent:
            self._attach_timings()
        self.lock = threading.RLock()
        self.worker = None
        self.pause_requested = threading.Event()
        self.messages = []
        self.calls = []
        self.turns = []
        self.message_ids = set()
        self.turn_id = None
        self.goal = ""
        self.task = ""
        self.initial_page = None
        self.conversation = []
        self.phase = "idle"
        self.progress = (
            "Página aberta. Envie uma tarefa ou uma pergunta." if self.agent else "Descreva o que deseja fazer."
        )
        self.pending = None
        self.pending_input = None
        self.input_ids = set()
        self.approved_commitment = False
        self.recovery_attempts = 0
        self.reconsidered_block = False
        self.navigation = []
        self.resume_stage = "begin"
        self.reply_index = None
        self.view = {}
        self.manual = Manual(self)
        self._publish()

    @classmethod
    def restore(cls, record, on_change=None):
        session = cls()
        session.id = record["id"]
        session.preview.session_id = session.id
        session.messages = record["messages"]
        session.message_ids = {message["turn_id"] for message in session.messages if message.get("turn_id")}
        session.last_url = record["last_url"]
        if record["phase"] in {"thinking", "running", "verifying", "awaiting_confirmation",
                               "awaiting_input", "paused", "manual"}:
            interruption = (
                "A execução foi interrompida. Envie um novo pedido para continuar; "
                "ações anteriores não serão repetidas."
            )
            if session.messages and session.messages[-1]["role"] == "assistant":
                session.messages[-1].update(content=interruption, kind="interrupted",
                                            verification={"satisfied": False, "evidence": []})
            else:
                session.messages.append({"role": "assistant", "content": interruption, "kind": "interrupted"})
        session.progress = "Histórico restaurado. Um novo pedido abrirá uma nova aba quando necessário."
        session.on_change = on_change
        session._publish()
        return session

    def _attach_timings(self):
        self.timings.extend(self.agent.timings.snapshot()["events"])
        self.agent.timings = self.agent.browser.timings = self.timings

    def snapshot(self, compact=False):
        with self.manual.lock:
            return self._snapshot(compact)

    def _snapshot(self, compact=False):
        with self.lock:
            source = self.view
            if compact:
                omitted = {"page", "decisions", "history", "text_calls", "elements", "chat_calls",
                           "turns", "initial_page", "navigation", "viewport_events", "conversation", "plan"}
                source = {k: v for k, v in source.items() if k not in omitted}
                page = self.view.get("page")
                source["page"] = {k: page[k] for k in ("url", "title", "w", "h")} if page else None
                decision = source.get("decision")
                source["decision"] = ({k: decision.get(k) for k in ("choice", "operation", "target")}
                                      if decision else None)
            view = frozen(source)
            view["viewport"] = dict(zip(("width", "height"), self.viewport)) if self.viewport else None
        view["preview_revision"] = self.preview.revision
        if not compact and view.get("page"):
            capture = self.preview.snapshot()["capture"]
            if capture and capture["revision"] == view["preview_revision"] and not (
                capture.get("manual") or capture["page"].get("protected")):
                view["page"]["screenshot"] = capture["page"]["screenshot"]
        now = time.perf_counter()
        waiting = self.approval_wait_ms
        if self.approval_started is not None:
            waiting += (now - self.approval_started) * 1000
        total = ((self.turn_finished or now) - self.turn_started) * 1000 if self.turn_started else 0
        manual_wait = self.manual.waiting()
        view["manual"] = self.manual.snapshot()
        if not compact:
            view["user_actions"] = copy.deepcopy(self.manual.actions)
        view["timing"] = {**self.timings.snapshot(self.turn_started or 0, details=not compact),
                          "end_to_end_ms": round(total), "approval_wait_ms": round(waiting),
                          "manual_wait_ms": round(manual_wait),
                          "execution_ms": round(
                              max(0, total - waiting - manual_wait)
                          ),
                          "first_action_ms": view.get("first_action_ms")}
        view["pause_requested"] = self.pause_requested.is_set() and view["chat_status"] in {
            "thinking", "running"
        }
        if view["pause_requested"]:
            view["progress"] = "Pausa solicitada. Aguardando a operação em andamento terminar…"
        if view["chat_status"] in {"thinking", "running", "verifying"} and view.get("started_at") is not None:
            view["elapsed_ms"] = round((time.perf_counter() - view["started_at"]) * 1000)
        last = view["messages"][-1] if view["messages"] else {}
        view["can_recheck"] = bool(
            self.agent and not self.browser_busy and view["chat_status"] == "completed"
            and last.get("kind") == "result" and last.get("verification")
            and (not last["verification"]["satisfied"] or last["verification"].get("stale"))
        )
        return view

    @timed("publication")
    def _publish(self):
        approval = None
        if self.pending:
            approval = {key: self.pending[key] for key in ("id", "description", "label", "url", "operation")}
        browser_state = self.agent.snapshot() if self.agent else {
            "page": None, "status": "idle", "history": [], "decisions": [],
            "decision": None, "text_calls": [], "elements": [], "elapsed_ms": 0,
            "started_at": None, "stop_reason": None,
        }
        if browser_state.get("page"):
            self.last_url = browser_state["page"].get("url") or self.last_url
            if self.pending_viewport is None:
                self.preview.request(self.agent.browser, browser_state["page"])
        history = browser_state["history"]
        first = next((h for h in history if h.get("executed_ms") is not None), None)
        first_ms = None
        if first and self.turn_started is not None and (browser_state["started_at"] or 0) >= self.turn_started:
            first_ms = round((browser_state["started_at"] - self.turn_started) * 1000 + first["executed_ms"])
        view = {
            **browser_state,
            "goal": self.goal,
            "task": self.task,
            "locale": self.locale,
            "session_id": self.id,
            "turn_id": self.turn_id,
            "messages": self.messages,
            "chat_status": self.phase,
            "progress": self.progress,
            "approval": approval,
            "pending_input": self.pending_input,
            "chat_calls": self.calls,
            "turns": self.turns,
            "initial_page": self.initial_page,
            "navigation": self.navigation,
            "last_url": self.last_url,
            "viewport": dict(zip(("width", "height"), self.viewport)) if self.viewport else None,
            "viewport_error": self.viewport_error,
            "viewport_events": self.viewport_events,
            "action_count": len(history),
            "decision_count": len(browser_state.get("decisions", [])),
            "first_action_ms": first_ms,
        }
        with self.lock:
            self.view = frozen(view)
        if self.on_change:
            projection = json.dumps((view["messages"], view["chat_status"], view["last_url"]), ensure_ascii=False)
            if projection != self.persisted:
                try:
                    self.on_change(view)
                    self.persisted = projection
                    self.storage_error = None
                except (OSError, RuntimeError, sqlite3.Error) as error:
                    self.storage_error = f"Não foi possível salvar o histórico: {error}"
            with self.lock:
                self.view["storage_error"] = self.storage_error

    def _reply(self, content, **extra):
        if self.reply_index is not None:
            self.messages[self.reply_index].update(content=content, **extra)

    def _approval_reply(self, content):
        self.messages.append({"role": "user", "content": content, "turn_id": self.turn_id, "kind": "confirmation"})
        self.messages.append({"role": "assistant", "content": "", "turn_id": self.turn_id, "kind": "pending"})
        self.reply_index = len(self.messages) - 1

    def _launch(self, target, *args):
        self.pause_requested.clear()
        with self.lock:
            self.browser_busy = True
        self.worker = threading.Thread(target=self._work, args=(target, args), daemon=True)
        self.worker.start()

    def _work(self, target, args):
        try:
            target(*args)
        except Exception as error:
            # An interrupted mutation is never replayed, even if its result could not be observed.
            if self.agent:
                self.agent.state["decision"] = None
            uncertain = self.agent and any(h["execution"] in {"requested", "uncertain"}
                                           for h in self.agent.state["history"])
            text = (f"A tarefa parou: {str(error).rstrip('.')}. "
                    "As ações já executadas não serão repetidas automaticamente.")
            if self.agent and self.phase in {"running", "verifying"} and not uncertain:
                stage = "finish" if self.phase == "verifying" else "run"
                self.agent.state["status"] = "ready"
                self.agent.state["stop_reason"] = str(error)
                self._paused(text, stage=stage, kind="technical_pause", pause_code="failure",
                             verification={"satisfied": False, "evidence": []})
            else:
                self.phase = "error"
                self.turn_finished = time.perf_counter()
                self.progress = "A tarefa parou e precisa de atenção."
                self._reply(text, kind="error", verification={"satisfied": False, "evidence": []})
                self._publish()
        finally:
            self._release_browser()
            self.manual.activate()

    def resize_viewport(self, width, height):
        with self.manual.lock:
            self._resize_viewport(width, height)

    def _resize_viewport(self, width, height):
        viewport = validate_viewport(width, height)
        with self.lock:
            if viewport == self.viewport:
                return
            self.viewport = self.pending_viewport = viewport
            self.manual.invalidate()
            if self.browser_busy:
                return
            self.browser_busy = True
        self._release_browser()

    def _release_browser(self):
        # Atomically hand ownership back only once the last queued size is handled.
        # HTTP requests can queue while a worker is finishing its final observation.
        while True:
            with self.lock:
                if self.pending_viewport is None:
                    self.browser_busy = False
                    return
            self._apply_viewport()

    def _apply_viewport(self):
        with self.lock:
            viewport, self.pending_viewport = self.pending_viewport, None
        if viewport is None or self.agent is None:
            return
        page = self.agent.state["page"]
        if viewport == (page["w"], page["h"]):
            self.viewport_error = None
            self._publish()
            return
        self.agent.state["decision"] = None
        if self.agent.state["status"] == "predicted":
            self.agent.state["status"] = "ready"
        if self.pending:
            self.pending = None
            self._stop_approval_clock()
            self._paused("A prévia mudou de tamanho. A confirmação foi descartada; continue para confirmar novamente.")
        event = {"width": viewport[0], "height": viewport[1], "status": "requested"}
        self.viewport_events.append(event)
        self.viewport_error = None
        self._publish()  # Record the browser mutation before inspecting its result.
        try:
            self.agent.browser.set_viewport(*viewport)
            event["status"] = "applied"
            self.agent.state["page"] = self.agent.browser.observe(screenshot=False)
            event["status"] = "observed"
        except Exception as error:
            event["status"] = "uncertain"
            self.viewport_error = f"Não foi possível atualizar a prévia: {error}"
        self._publish()

    def _check_idle(self):
        with self.manual.lock:
            if self.manual.status == "active" and not self.manual.can_resume:
                # Browsing between tasks ends by itself; the next request starts from the page the user left.
                self.manual.finish(resume=False)
                if self.manual.needs_reconcile:
                    raise ValueError("Assuma e recupere o controle manual antes de usar o assistente.")
        if self.manual.status != "off":
            raise ValueError("Encerre o controle manual antes de usar o assistente.")
        if self.worker and self.worker.is_alive():
            raise ValueError("Aguarde a operação atual terminar.")

    def message(self, body):
        if self.manual.needs_reconcile:
            raise ValueError("Assuma e recupere o controle manual antes de enviar uma nova tarefa.")
        value, message_id = validate_message(body)
        locale = validate_locale(body.get("locale"))
        if message_id in self.message_ids:
            return
        self._check_idle()
        if self.pending_input:
            raise ValueError("Responda ou cancele a pergunta pendente antes de enviar outra tarefa.")
        if self.pending:
            raise ValueError("Confirme ou recuse a ação pendente antes de enviar outra mensagem.")
        if self.turn_id:
            self.turns.append(
                {
                    "turn_id": self.turn_id,
                    "goal": self.goal,
                    "task": self.task,
                    "history": copy.deepcopy(self.agent.state["history"]) if self.agent else [],
                    "decisions": copy.deepcopy(self.agent.state["decisions"]) if self.agent else [],
                    "text_calls": copy.deepcopy(self.agent.state["text_calls"]) if self.agent else [],
                    "initial_page": self.initial_page,
                    "timing": self.snapshot()["timing"],
                }
            )
        self.conversation = [{"role": msg["role"], "content": msg["content"][:4000]} for msg in self.messages[-12:]]
        self.goal = self.task = value
        self.locale = locale
        self.turn_started = time.perf_counter()
        self.timings.reset(self.turn_started)
        self.turn_finished = self.approval_started = None
        self.approval_wait_ms = 0
        self.approved_commitment = False
        self.manual.wait_ms = 0
        self.manual.can_resume = False
        self.recovery_attempts = 0
        self.reconsidered_block = False
        self.turn_id = message_id
        self.message_ids.add(message_id)
        self.messages.append({"role": "user", "content": self.goal, "turn_id": message_id})
        self.messages.append({"role": "assistant", "content": "", "turn_id": message_id, "kind": "pending"})
        self.reply_index = len(self.messages) - 1
        self.phase, self.progress = "thinking", "Entendendo seu pedido e identificando o destino…"
        self._publish()
        self._launch(self._begin)

    def _context(self):
        return {
            "goal": self.task,
            "response_language": LANGUAGES[self.locale],
            "conversation": self.conversation,
            "page": assistant.page_context(self.agent.state["page"]),
        }

    def _ask(self, kind, context):
        before = len(self.calls)
        try:
            with self.timings.measure(f"helper_{kind}"):
                return assistant.ask(kind, context, self.calls)
        finally:
            for call in self.calls[before:]:
                call["turn_id"] = self.turn_id

    def _begin(self):
        # Opening a tab takes about a second; do it on about:blank while the request helper runs.
        # The destination is navigated only after the helper's URL passes routed_url.
        opening = None
        if self.agent is None:
            pool = ThreadPoolExecutor(max_workers=1)  # not a `with` block: leaving it would wait for the tab
            opening = pool.submit(open_blank, self.viewport)
            pool.shutdown(wait=False)
        opened = False
        try:
            route = self._ask(
                "request",
                {
                    "goal": self.goal,
                    "response_language": LANGUAGES[self.locale],
                    "conversation": self.conversation,
                    "current_url": self.agent.state["page"]["url"] if self.agent else None,
                },
            )
            # The self-contained restatement drives the agent and the checks; the chat keeps the user's words.
            self.task = route["task"].strip() or self.goal  # page questions often come back without a restatement
            if route["url"] != "-":
                url = routed_url(route["url"], self.goal)
                if self.pause_requested.is_set():
                    self._paused(stage="begin")
                    return
                if self.agent is None or self.agent.state["page"]["url"] != url:
                    self.navigation.append({"turn_id": self.turn_id, "url": url, "status": "requested"})
                    self._publish()
                    if self.agent is None:
                        browser, opening = opening.result(), None
                        self.agent = Agent(url, None, screenshots=False, viewport=self.viewport, browser=browser)
                        self._attach_timings()
                        opened = True
                    else:
                        self.agent.browser.navigate(url)
                    self.navigation[-1]["status"] = "opened"
            elif self.agent is None:
                self._reply(
                    "Qual site devo abrir? Diga o nome do site ou envie a URL junto com seu pedido.", kind="clarify"
                )
                self.phase, self.progress = "answered", "Aguardando o destino."
                self.turn_finished = time.perf_counter()
                self._publish()
                return
        finally:
            if opening is not None:
                try:
                    opening.result().close()
                except Exception:
                    pass  # The tab never opened, or already closed with its error.
        if self.pause_requested.is_set():
            self._paused(stage="begin")
            return
        self._apply_viewport()
        # A tab opened for this goal was observed a moment ago; a resize re-observes it itself.
        self.agent.start_task(self.task, self.conversation, observe=not opened or self.viewport_error is not None)
        self.initial_page = assistant.page_context(self.agent.state["page"])
        self._publish()
        if self.pause_requested.is_set():
            self._paused(stage="begin")
            return
        result = route
        if route["intent"] == "answer":
            result = {**route, **self._ask("answer", self._context())}
        # The acknowledgment is cosmetic; an empty one must not stop the task.
        self._reply(result["reply"].strip() or "Entendido. Vou começar.",
                    kind="answer" if result["intent"] == "answer" else "progress")
        if self.pause_requested.is_set():
            self._paused(stage="run" if result["intent"] == "task" else "begin")
            return
        if result["intent"] == "answer":
            self.phase, self.progress = "answered", "Resposta baseada na página atual."
            self.turn_finished = time.perf_counter()
            self._publish()
            return
        self._run()

    def _paused(self, text="Execução pausada. Você pode continuar ou enviar um novo pedido.", stage="run",
                kind="paused", **extra):
        if self.agent:
            self.agent.state["decision"] = None
        self.resume_stage = stage
        self.phase, self.progress = "paused", text
        self._reply(text, kind=kind, **extra)
        self._publish()

    def _handoff(self, code):
        """Ask the user to take control; Continuar com Jev resumes this same run."""
        code = code if code in HANDOFF else "OTHER"
        self.agent.state["status"] = "ready"
        self.agent.state["stop_reason"] = None
        self.manual.reason = HANDOFF[code]
        self._paused(HANDOFF[code], kind="handoff", handoff={"code": code})

    def _resolve_blocker(self, page, action=None):
        context = self._context()
        if action is not None:
            context["missing_field"] = {key: action.get(key) for key in ("label", "role")}
        before = len(self.calls)
        with self.timings.measure("helper_blocker"):
            try:
                assessment = assistant.assess_blocker(context, self.calls)
            finally:
                for call in self.calls[before:]:
                    call["turn_id"] = self.turn_id
        if not self.agent.browser.fresh(page, action):
            assessment = {**assessment, "kind": "recover"}
        if self.pause_requested.is_set():
            self._paused()
            return True
        if assessment["kind"] == "human":
            self._handoff(assessment["reason"])
            return True
        if assessment["kind"] == "input":
            self.pending_input = {"id": secrets.token_urlsafe(24), "field": action["label"],
                                  "description": assessment["description"]}
            self.agent.state["decision"] = None
            self.agent.state["status"] = "ready"
            self.agent.pending_text = None
            self.phase, self.progress = "awaiting_input", assessment["description"]
            self._reply(assessment["description"], kind="input")
            self._publish()
            return True
        if self.reconsidered_block:
            self.agent.state["decision"] = None
            self.agent.state["status"] = "ready"
            self._paused("Não encontrei uma ação segura para avançar. A tarefa está pausada; "
                         "use Continuar para observar a página novamente.",
                         kind="technical_pause", pause_code="no_action")
            return True
        self.reconsidered_block = True
        self.agent.state["verification_feedback"] = [
            *(self.agent.state.get("verification_feedback") or []), RECONSIDER_BLOCKED]
        self.agent.pending_text = None
        self.agent.state["decision"] = None
        self.agent.state["page"] = self.agent.browser.observe(screenshot=False)
        self.agent.state["status"] = "ready"
        return False

    def input(self, body):
        with self.manual.lock:
            with self.lock:
                self._input(body)

    def _input(self, body):
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Identificador da pergunta inválido.")
        if request_id in self.input_ids:
            return
        self._check_idle()
        if not self.pending_input or self.pending_input["id"] != request_id or self.phase != "awaiting_input":
            raise ValueError("Esta pergunta não está mais ativa.")
        cancel = body.get("cancel", False)
        if type(cancel) is not bool:
            raise ValueError("Cancelamento inválido.")
        value = body.get("value")
        if not cancel and (not isinstance(value, str) or not 0 < len(value.strip()) <= 2000 or "\x00" in value):
            raise ValueError("Informe um valor válido para o campo.")
        field = self.pending_input["field"]
        self.input_ids.add(request_id)
        self.pending_input = None
        if cancel:
            self._approval_reply("Pergunta cancelada.")
            self._paused("Pergunta cancelada. A tarefa está pausada.")
            return
        value = value.strip()
        supplied = {"role": "user", "content": f"{field}: {value}"}
        self.conversation.append(supplied)
        self._approval_reply(supplied["content"])
        self.messages[-2].update(kind="input_reply", input_request_id=request_id)
        self.agent.state["conversation"] = copy.deepcopy(self.conversation)
        self.agent.pending_text = None
        self.agent.state["decision"] = None
        self.agent.state["status"] = "ready"
        self.reconsidered_block = False
        self.phase, self.progress = "running", "Continuando com a informação fornecida…"
        self._publish()
        self._launch(self._resume_run)

    def _resume_run(self):
        self.agent.state["page"] = self.agent.browser.observe(screenshot=False)
        self._run()

    def _run(self):
        self.phase = "running"
        stale_attempts = 0
        ambiguous_attempts = 0
        while self.agent.state["status"] not in {"done", "blocked"}:
            self._apply_viewport()
            if self.pause_requested.is_set():
                self._paused()
                return
            self.progress = "Escolhendo a próxima ação…"
            self._publish()
            executed_before = sum(item["execution"] == "executed" for item in self.agent.state["history"])
            try:
                self.agent.command("predict")
                decision, page = self.agent.state["decision"], self.agent.state["page"]
                if decision["choice"] != "BLOCKED":
                    # The nudge applied to one decision only; it must not bias the rest of the turn.
                    feedback = self.agent.state.get("verification_feedback") or []
                    if RECONSIDER_BLOCKED in feedback:
                        self.agent.state["verification_feedback"] = [
                            item for item in feedback if item != RECONSIDER_BLOCKED] or None
                if self.pause_requested.is_set():
                    self._paused()
                    return
                if decision["choice"] == "DONE":
                    # DONE requests a fresh independent check, never proves completion.
                    self.agent.state["decision"] = None
                    self.agent.state["status"] = "done"
                    break
                if decision["choice"] == "BLOCKED":
                    if self._resolve_blocker(page):
                        return
                    continue
                if decision.get("ambiguous"):
                    self.agent.state["decision"] = None
                    self.agent.state["status"] = "ready"
                    if ambiguous_attempts:
                        self._paused("As alternativas de operação ou alvo continuam empatadas. "
                                     "A tarefa foi pausada antes de executar uma ação. "
                                     "Use Continuar para tentar novamente.",
                                     kind="technical_pause", pause_code="ambiguous_decision")
                        return
                    ambiguous_attempts += 1
                    self.agent.state["page"] = self.agent.browser.observe(screenshot=False)
                    continue
                if decision["choice"] not in {"DONE", "BLOCKED"}:
                    action = next(a for a in page["actions"] if a["id"] == decision["choice"])
                    if action["kind"] in {"click", "fill", "select", "press_enter"}:
                        if not self.agent.browser.fresh(page, action):
                            raise StalePage("Target changed before safety assessment")
                        self.progress = f"Verificando o efeito de: {action['label']}"
                        self._publish()
                        context = {**self._context(), "action": {
                            k: action[k] for k in ("kind", "label", "role", "value", "current_value",
                                                  "checked", "selected", "expanded") if k in action
                        }}
                        if action["kind"] == "fill":
                            with ThreadPoolExecutor(max_workers=1) as pool:
                                generated = pool.submit(self.agent.prepare_text, action, page)
                                safety = self._ask("safety", context)
                                generated.result()
                        else:
                            safety = self._ask("safety", context)
                        if self.pause_requested.is_set():
                            self._paused()
                            return
                        if not self.agent.browser.fresh(page, action):
                            raise StalePage("Target changed during safety assessment")
                        if safety["effect"] != "safe":
                            description = safety["description"]
                            if action["kind"] == "fill":
                                text, _ = self.agent.prepare_text(action, page)
                                description += f"\nTexto a inserir: {text}"
                            self.pending = {
                                "id": secrets.token_urlsafe(24),
                                "description": description,
                                "label": action["label"],
                                "url": page["url"],
                                "operation": decision["operation"],
                                "fingerprint": page["fingerprint"],
                                "viewport": (page["w"], page["h"]),
                                "decision": decision,
                                "action": action,
                            }
                            self.approval_started = time.perf_counter()
                            self.agent.state["decision"] = None
                            self.phase, self.progress = "awaiting_confirmation", "Aguardando sua confirmação."
                            self._reply(description, kind="approval")
                            self._publish()
                            return
                with self.manual.lock:
                    if self.pause_requested.is_set():
                        self._paused()
                        return
                    # Reserve this operation before a control request can suspend the next one.
                    self.progress = "Executando a ação escolhida…"
                self._publish()
                self.agent.command("act", {"fingerprint": page["fingerprint"]})
                stale_attempts = 0
                ambiguous_attempts = 0
            except MissingValue:
                if self._resolve_blocker(page, action):
                    return
                continue
            except StalePage:
                # Reobserve and choose anew. An already logged mutation is never repeated.
                self.agent.state["decision"] = None
                self.agent.state["page"] = self.agent.browser.observe(screenshot=False)
                self.agent.state["status"] = "ready"
                executed_after = sum(item["execution"] == "executed" for item in self.agent.state["history"])
                stale_attempts = stale_attempts + 1 if executed_after == executed_before else 0
                if stale_attempts >= 3:
                    self._paused(
                        "A página mudou antes da ação em três tentativas seguidas. "
                        "A execução foi pausada para evitar novas chamadas sem progresso. "
                        "Use Continuar quando estiver pronta."
                    )
                    return
            self._publish()
        if self.pause_requested.is_set():
            self._paused()
            return
        if self.agent.state["status"] == "blocked":
            reason = self.agent.state.get("stop_reason") or "Não foi possível avançar nesta página."
            self.agent.state["decision"] = None
            self.agent.state["status"] = "ready"
            self._paused(f"A tarefa foi pausada por falta de progresso: {reason}. Use Continuar para tentar novamente.",
                         kind="technical_pause", pause_code="no_progress")
            return
        self._finish()

    def _finish(self, allow_recovery=True):
        if self.manual.status == "requested":
            self._paused(stage="finish")
            return
        self._apply_viewport()
        self.phase, self.progress = "verifying", "Conferindo o resultado na página…"
        self._publish()
        # Live pages (ads, carousels, counters) change constantly. A verdict stands while its evidence is
        # still shown on the same URL; otherwise check once more against the newer page.
        for _ in range(2):
            observed = self.agent.state["page"] = self.agent.browser.observe(screenshot=False)
            checked_at = time.time()
            verdict = self._verify()
            if not verdict.get("checks") or self._still_shows(observed, verdict):
                break
            verdict = {"satisfied": False, "evidence": [], "checks": [],
                       "reason": "A página mudou durante a conferência. Verifique o resultado novamente."}
        checks = verdict.get("checks", [])
        if self.manual.status == "requested":
            self._paused(stage="finish")
            return
        history = self.agent.state["history"]
        if (
            allow_recovery and not verdict["satisfied"] and self.agent.state["status"] == "done"
            and self.recovery_attempts < 2 and not self.approved_commitment
            and all(item["execution"] != "uncertain" for item in history)
            and any(check["status"] == "not_met" for check in checks)
            and len(history) < MAX_STEPS and len(self.agent.state["decisions"]) < MAX_STEPS * 2
        ):
            self.recovery_attempts += 1
            self.agent.state["verification_feedback"] = [
                {"requirement": check["requirement"], "reason": check["reason"]}
                for check in checks if check["status"] == "not_met"
            ]
            self.agent.state["status"] = "ready"
            self.progress = "Resultado incompleto; escolhendo uma ação corretiva…"
            self._publish()
            self._run()
            return
        if verdict["satisfied"]:
            response = verdict["reply"]
            self.agent.state["plan_index"] = 1
        if not verdict["satisfied"]:
            response = "Não consegui confirmar que seu pedido foi concluído na página. Confira o resultado observado."
            if verdict.get("reason"):
                response += f" {verdict['reason']}"
            if self.agent.state.get("stop_reason"):
                response += f" A execução parou: {self.agent.state['stop_reason']}."
        verdict.update(checked_at=checked_at, url=observed["url"])
        self._reply(response, kind="result", verification=verdict)
        self.phase = "completed"
        self.turn_finished = time.perf_counter()
        self.progress = "Resultado confirmado na página." if verdict["satisfied"] else "Conclusão não confirmada."
        self._publish()

    def _verify(self):
        context = {**self._context(), "initial_page": self.initial_page,
                   "execution_status": self.agent.state["status"], "stop_reason": self.agent.state.get("stop_reason")}
        before = len(self.calls)
        try:
            with self.timings.measure("helper_verify"):
                return assistant.verify(context, self.calls)
        except Exception:
            return {"satisfied": False, "evidence": [], "checks": [],
                    "reason": "Não foi possível executar a conferência do resultado. Verifique novamente."}
        finally:
            for call in self.calls[before:]:
                call["turn_id"] = self.turn_id

    def _still_shows(self, observed, verdict):
        """Same URL and every cited quote still visible; unrelated page churn does not matter."""
        if self.pending_viewport is not None:
            return False
        try:
            current = self.agent.browser.observe(screenshot=False)
        except (StalePage, RuntimeError, TimeoutError):
            return False
        self.agent.state["page"] = current
        sources = assistant.evidence_sources(assistant.page_context(current))
        return current["url"] == observed["url"] and all(
            assistant.shown(quote, sources) for quote in verdict["evidence"])

    def navigate_user(self, body):
        """The preview's address bar and Back/Forward/Reload, between tasks. The user is the one acting."""
        self._check_idle()
        if self.pending_input:
            raise ValueError("Responda ou cancele a pergunta pendente antes de navegar.")
        action, url = validate_navigation(body)
        if self.agent is None and action != "url":
            raise ValueError("Nenhuma página aberta para navegar.")
        entry = {"turn_id": self.turn_id, "url": url or action, "status": "requested", "source": "user"}
        self.navigation.append(entry)  # recorded before the browser is touched, never replayed
        self.progress = "Abrindo a página…"
        self._publish()
        self._launch(self._navigate_user, action, url, entry)

    def _navigate_user(self, action, url, entry):
        refresh = False
        try:
            if self.agent is None:
                self.agent = Agent(url, None, screenshots=False, viewport=self.viewport)
                self._attach_timings()
            else:
                browser = self.agent.browser
                {"url": lambda: browser.navigate(url), "reload": browser.reload,
                 "back": lambda: browser.history_step(-1), "forward": lambda: browser.history_step(1)}[action]()
                refresh = True
        except NoHistory as error:  # nothing to go back/forward to: the page did not change
            entry["status"] = "not_executed"
            self.progress = str(error)
        except Exception as error:
            # Not a task failure: the conversation's last result stays as it was. The navigation may have
            # happened (a net error page, a slow load), so it is recorded as uncertain and never replayed.
            entry["status"] = "uncertain"
            self.progress = f"Não foi possível abrir a página: {str(error).rstrip('.')}."
            refresh = True
        else:
            entry["status"] = "opened"
            self.progress = "Página aberta. Envie uma tarefa ou uma pergunta."
        if refresh:
            try:  # only refreshes what the preview shows; it cannot undo a navigation that already happened
                self.agent.state["page"] = self.agent.browser.observe(screenshot=False)
            except Exception:
                pass  # the next task observes again
        self._publish()

    def recheck(self):
        self._check_idle()
        if not self.snapshot()["can_recheck"]:
            raise ValueError("Não há um resultado desta tarefa disponível para conferir novamente.")
        self.phase, self.progress = "verifying", "Conferindo novamente, sem repetir as ações…"
        self.turn_finished = None
        self._publish()
        self._launch(self._finish, False)

    def approve(self, body):
        self._check_idle()
        pending = self.pending
        if not pending or body.get("approval_id") != pending["id"]:
            raise ValueError("Esta confirmação não está mais disponível.")
        # Consume the authorization before starting any browser work.
        self.pending = None
        self._stop_approval_clock()
        self.approved_commitment = True
        self.phase, self.progress = "running", "Conferindo a página antes de executar…"
        self._approval_reply(f"Confirmar ação: {pending['label']}")
        self._reply("Ação autorizada. Conferindo a página…", kind="progress")
        self._publish()
        self._launch(self._approved, pending)

    def _approved(self, pending):
        self._apply_viewport()
        page = self.agent.state["page"]
        if (
            page["fingerprint"] != pending["fingerprint"]
            or (page["w"], page["h"]) != pending["viewport"]
            or not self.agent.browser.fresh(page)
            or not self.agent.browser.fresh(page, pending["action"])
        ):
            self.agent.state["page"] = self.agent.browser.observe(screenshot=False)
            self._paused("A página mudou. A confirmação foi descartada; continue para escolher e confirmar novamente.")
            return
        self.agent.state["decision"] = pending["decision"]
        if self.pause_requested.is_set():
            self._paused("Execução pausada. Continue para escolher e confirmar novamente.")
            return
        before = len(self.agent.state["history"])
        try:
            self.agent.command("act", {"fingerprint": pending["fingerprint"]})
        except StalePage:
            self.agent.state["page"] = self.agent.browser.observe(screenshot=False)
            not_executed = (
                len(self.agent.state["history"]) == before
                or self.agent.state["history"][-1]["execution"] == "not_executed"
            )
            if not_executed:
                self._paused("A página mudou antes da ação. Confirme novamente após continuar.")
                return
            self.agent.state["status"] = "ready"
        self._run()

    def reject(self, body):
        self._check_idle()
        if not self.pending or body.get("approval_id") != self.pending["id"]:
            raise ValueError("Esta confirmação não está mais disponível.")
        self._approval_reply(f"Recusar ação: {self.pending['label']}")
        self.pending = None
        self._stop_approval_clock()
        self.agent.state["decision"] = None
        self.agent.state["status"] = "blocked"
        self.phase, self.progress = "completed", "Ação recusada."
        self.turn_finished = time.perf_counter()
        self._reply("A ação foi recusada e não foi executada. Envie outro pedido quando quiser.", kind="result")
        self._publish()

    def pause(self):
        if self.phase not in {"thinking", "running"}:
            raise ValueError("Não há uma execução para pausar.")
        self.pause_requested.set()

    def resume(self):
        if self.manual.needs_reconcile:
            raise ValueError("Assuma e recupere o controle manual antes de continuar a tarefa.")
        self._check_idle()
        if self.phase != "paused":
            raise ValueError("Não há uma tarefa pausada para continuar.")
        self.reconsidered_block = False
        self.phase, self.progress = "running", "Continuando a tarefa…"
        self._publish()
        self._launch({"begin": self._begin, "finish": self._finish}.get(self.resume_stage, self._resume_run))

    def close(self):
        self._check_idle()
        self.manual.close()
        self.preview.close()
        if self.agent:
            self.agent.close()

    def _stop_approval_clock(self):
        if self.approval_started is not None:
            self.approval_wait_ms += (time.perf_counter() - self.approval_started) * 1000
            self.approval_started = None
