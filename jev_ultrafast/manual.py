"""Exclusive human input into the owned tab. No model calls or input retries."""

import math
import secrets
import threading
import time

KEYS = {"Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp",
        "ArrowDown", "Home", "End", "PageUp", "PageDown", "Shift", "Control", "Alt", "Meta"}


def validate_event(body, context):
    kind = body.get("type")
    allowed = {"session_id", "owner_token", "request_id", "context", "type", "modifiers"} | {
        "text": {"text"}, "key": {"key", "action"},
        "pointer": {"action", "x", "y", "buttons", "clickCount"},
        "wheel": {"action", "x", "y", "deltaX", "deltaY"},
    }.get(kind, set())
    if set(body) - allowed:
        raise ValueError("Propriedades de entrada não permitidas.")
    modifiers = body.get("modifiers", 0)
    if type(modifiers) is not int or not 0 <= modifiers <= 15:
        raise ValueError("Modificadores inválidos.")
    event = {"type": kind, "modifiers": modifiers}
    if kind == "text":
        text = body.get("text")
        if not isinstance(text, str) or not 0 < len(text) <= 4000 or "\x00" in text:
            raise ValueError("Texto inválido ou muito longo.")
        return {**event, "text": text}
    if kind == "key":
        key, action = body.get("key"), body.get("action")
        if not isinstance(key, str) or action not in {"down", "up"} or not (
            key in KEYS or (key.lower() in {"a", "c", "x", "z", "y"} and modifiers & 6)
        ):
            raise ValueError("Tecla não suportada.")
        return {**event, "key": key, "action": action}
    if kind not in {"pointer", "wheel"}:
        raise ValueError("Entrada manual desconhecida.")
    action = body.get("action")
    if action not in ({"wheel"} if kind == "wheel" else {"down", "up", "move"}):
        raise ValueError("Evento de ponteiro inválido.")
    for key, limit in (("x", context["w"]), ("y", context["h"])):
        value = body.get(key)
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value < limit:
            raise ValueError("Posição fora da imagem atual.")
        event[key] = value
    event["action"] = action
    if kind == "wheel":
        for key in ("deltaX", "deltaY"):
            value = body.get(key, 0)
            if type(value) not in {int, float} or not math.isfinite(value) or abs(value) > 2000:
                raise ValueError("Rolagem inválida.")
            event[key] = value
    else:
        buttons, count = body.get("buttons", 0), body.get("clickCount", 1)
        if type(buttons) is not int or buttons not in {0, 1} or type(count) is not int or count not in {0, 1, 2}:
            raise ValueError("Botão inválido.")
        event.update(buttons=buttons, clickCount=count)
    return event


class Manual:
    def __init__(self, session):
        self.session = session
        self.lock = threading.RLock()
        self.status = "off"
        self.token = None
        self.reason = ""
        self.sequence = 0
        self.control_id = 0
        self.context = None
        self.generation = 0
        self.can_resume = False
        self.original_phase = "idle"
        self.deadline = 0
        self.wait_started = None
        self.wait_ms = 0
        self.actions = []
        self.group = secrets.token_urlsafe(12)
        self.held_keys = set()
        self.pointer = None
        self.closed = False
        self.needs_reconcile = False

    def snapshot(self):
        with self.lock:
            return {"status": self.status, "reason": self.reason, "sequence": self.sequence,
                    "control_id": self.control_id,
                    "can_resume": self.can_resume, "input_ready": self.status == "active" and self.context is not None,
                    "context": dict(self.context) if self.context else None,
                    "suggested": bool(self.reason), "available": bool(self.session.agent),
                    "needs_reconcile": self.needs_reconcile}

    def waiting(self):
        with self.lock:
            return self.wait_ms + (
                (time.perf_counter() - self.wait_started) * 1000 if self.wait_started and self.can_resume else 0)

    def heartbeat(self, token):
        with self.lock:
            if self.token and secrets.compare_digest(str(token or ""), self.token):
                self.deadline = time.monotonic() + 60

    def require_owner(self, body):
        if not self.is_owner(body.get("owner_token")):
            raise ValueError("O controle manual pertence a outra interface.")

    def is_owner(self, token):
        return bool(self.token and secrets.compare_digest(str(token or ""), self.token))

    def start(self, body):
        self.expire()
        with self.lock:
            if self.status != "off":
                self.require_owner(body)
                if self.status == "uncertain":
                    # Explicit reconciliation releases held input, never repeats the previous event.
                    self.reconcile()
                    self.status, self.context = "active", None
                    self.session.preview.invalidate()
                    self.session._publish()
                self.heartbeat(body.get("owner_token"))
                return self.token
            if not self.session.agent:
                raise ValueError("Abra uma página antes de assumir o controle.")
            self.token = secrets.token_urlsafe(32)
            self.sequence = 0
            self.control_id += 1
            self.original_phase = self.session.phase
            self.can_resume = bool(self.session.goal and self.session.phase in {
                "thinking", "running", "verifying", "paused", "awaiting_confirmation"})
            self.deadline = time.monotonic() + 60
            self.status = "requested"
            self.reason = "Preparando controle manual…"
            self.session.pause_requested.set()
            self.session.pending = None
            self.session._stop_approval_clock()
            self.session._publish()
            if not self.session.browser_busy:
                self.activate()
            return self.token

    def activate(self):
        with self.lock:
            if self.status != "requested" or self.closed:
                return
            self.session.agent.pending_text = None
            self.session.agent.state["decision"] = None
            if time.monotonic() >= self.deadline:
                self.status, self.token = "off", None
                self.reason = "Controle manual expirado; a tarefa permanece pausada."
                self.session._publish()
                return
            try:
                if self.needs_reconcile:
                    self.reconcile()
                self.session.agent.browser.manual_mode(True)
            except Exception:
                self.status, self.token = "off", None
                self.reason = "Não foi possível assumir esta página."
                raise RuntimeError(self.reason) from None
            if self.session.phase == "error":
                self.can_resume = False
                self.original_phase = "error"
            self.status, self.context = "active", None
            self.wait_started = time.perf_counter()
            if self.can_resume:
                self.reason = "Você está no controle. Resolva o bloqueio e escolha Continuar com Jev."
                self.session.phase, self.session.progress = "manual", self.reason
            else:
                # Free browsing between tasks: the conversation keeps its phase and nothing waits for the user.
                self.reason = ""
            self.session.preview.begin_manual(self.session.agent.browser, self)
            self.session._publish()

    def frame(self, context):
        with self.lock:
            if self.status not in {"active", "uncertain"}:
                return
            current = {k: context[k] for k in ("document", "w", "h")}
            if not self.context or any(self.context[k] != current[k] for k in current):
                self.generation += 1
            self.context = {**current, "generation": self.generation}
            if self.status == "active":
                self.needs_reconcile = False
            return dict(self.context)

    def invalidate(self):
        with self.lock:
            self.context = None
            self.session.preview.invalidate()

    def expire(self):
        with self.lock:
            if self.status not in {"off", "requested"} and time.monotonic() >= self.deadline:
                self.finish(resume=False, expired=True)

    def input(self, body):
        with self.lock:
            self.expire()
            self.require_owner(body)
            if self.status != "active" or self.context is None:
                raise ValueError("Aguarde uma imagem atual ou recupere o controle manual.")
            sequence = body.get("request_id")
            if type(sequence) is not int or sequence < 1:
                raise ValueError("Identificador da entrada inválido.")
            if sequence <= self.sequence:
                return
            if sequence != self.sequence + 1:
                raise ValueError("Entradas fora de ordem. Atualize o estado antes de continuar.")
            if body.get("context") != self.context:
                raise ValueError("A imagem mudou. Aguarde a prévia atualizada.")
            browser = self.session.agent.browser
            live = browser.manual_context()
            if any(live[k] != self.context[k] for k in ("document", "w", "h")):
                self.invalidate()
                raise ValueError("A página mudou. Aguarde uma nova imagem.")
            event = validate_event(body, self.context)
            self.sequence = sequence
            self.heartbeat(body.get("owner_token"))
            entry = {"control_id": self.control_id, "request_id": sequence, "type": event["type"],
                     "execution": "requested"}
            self.actions.append(entry)
            del self.actions[:-200]
            for message in self.session.messages:
                if message.get("verification"):
                    message["verification"]["stale"] = True
            self.session._publish()
            if event["type"] == "pointer":
                if event["action"] == "down":
                    self.group = secrets.token_urlsafe(12)
                    self.pointer = dict(event)
                elif event["action"] == "up":
                    # Never repeat a release that may already have committed a drag/click.
                    self.pointer = None
            if event["type"] == "key":
                if event["action"] == "down":
                    self.held_keys.add(event["key"])
                    if event["key"] in {"Tab", "Enter"} or (event["key"].lower() == "a" and event["modifiers"] & 6):
                        self.group = secrets.token_urlsafe(12)
                else:
                    self.held_keys.discard(event["key"])
            try:
                browser.manual_event(event, self.group)
                entry["execution"] = "executed"
                # Human input must never trigger automatic corrective mutations.
                self.session.approved_commitment = True
            except Exception:
                entry["execution"] = "uncertain"
                self.needs_reconcile = True
                self.status = "uncertain"
                self.reason = "Resultado da entrada incerto. Atualize e recupere o controle antes de continuar."
                self.session._publish()
                raise RuntimeError(self.reason) from None
            self.session._publish()

    def release_input(self):
        browser = self.session.agent.browser
        for key in list(self.held_keys):
            self.held_keys.discard(key)  # A failed release is never repeated.
            entry = {"type": "key_release", "execution": "requested"}
            self.actions.append(entry)
            self.session._publish()
            try:
                browser.manual_event({"type": "key", "key": key, "action": "up", "modifiers": 0}, self.group)
            except Exception:
                entry["execution"] = "uncertain"
                raise RuntimeError("Não foi possível liberar o teclado.") from None
            entry["execution"] = "executed"
        if self.pointer:
            # A second mouseUp could commit a drag twice. Leave it for explicit reconciliation.
            raise RuntimeError("O ponteiro ficou pressionado. Reconcilie a página antes de retomar.")

    def reconcile(self):
        if self.pointer:
            pointer, self.pointer = self.pointer, None
            entry = {"type": "pointer_release", "execution": "requested"}
            self.actions.append(entry)
            self.session._publish()
            try:
                self.session.agent.browser.manual_event({**pointer, "action": "up", "buttons": 0}, self.group)
            except Exception:
                entry["execution"] = "uncertain"
                raise RuntimeError("Não foi possível liberar o ponteiro.") from None
            entry["execution"] = "executed"
        self.release_input()

    def end(self, body):
        with self.lock:
            self.require_owner(body)
            if self.status == "requested":
                raise ValueError("A transferência de controle ainda está em andamento.")
            if type(body.get("resume", False)) is not bool:
                raise ValueError("Retomada inválida.")
            if self.status == "uncertain":
                raise ValueError("Recupere o controle antes de encerrar.")
            self.finish(resume=body.get("resume", False))

    def finish(self, resume=False, expired=False):
        session = self.session
        session.preview.end_manual()
        self._stop_waiting()
        self.token, self.context, self.status = None, None, "off"
        self.reason = ""
        if session.agent and not self._reconcile_page():
            resume = False
        session.agent.pending_text = None
        session.agent.state["decision"] = None
        self._settle_phase()
        self.reason = "Controle manual expirado; a tarefa não foi retomada." if expired else self.reason
        session.progress = self.reason if expired else "Controle manual encerrado."
        session._publish()
        if resume and self.can_resume:
            session._approval_reply("Continuar com Jev")
            session.resume()

    def _stop_waiting(self):
        """Paused time counts as manual wait only for a task that will resume."""
        if self.wait_started:
            if self.can_resume:
                self.wait_ms += (time.perf_counter() - self.wait_started) * 1000
            self.wait_started = None

    def _reconcile_page(self):
        """Release input, leave manual mode and re-observe. False means the task must stay paused."""
        agent = self.session.agent
        try:
            self.release_input()
            agent.browser.manual_mode(False)
            before = agent.state["page"].get("fingerprint")
            page = agent.state["page"] = agent.browser.observe(screenshot=False)
            if self.can_resume:
                self._note_return(page, changed=page.get("fingerprint") != before)
            return True
        except Exception:
            self.needs_reconcile = True
            self.reason = "Não foi possível reconciliar a página. A tarefa permanece pausada."
            try:
                agent.browser.manual_mode(False)
            except Exception:
                pass
            return False

    def _note_return(self, page, changed):
        """Tell the model a human acted; this also restarts the no-progress window."""
        history = self.session.agent.state["history"]
        history.append({"step": len(history) + 1, "action": "User took manual control and returned",
                        "kind": "manual", "execution": "executed", "text": None,
                        "page_changed": changed, "url": page.get("url"), "executed_ms": None})

    def _settle_phase(self):
        session = self.session
        if self.can_resume:
            session.agent.state["status"] = "done" if session.resume_stage == "finish" else "ready"
            session.phase = "paused"
        else:
            session.phase = self.original_phase

    def close(self):
        with self.lock:
            self.closed = True
            self.token = None
