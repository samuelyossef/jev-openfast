"""Evidence-gated handoff and ordinary missing values, with offline transports."""
# ruff: noqa: F811

import copy

import pytest
from test_chat import local_server, offline, send  # noqa: F401
from test_manual import blocked, human_page, session  # noqa: F401

from jev_ultrafast import agent, assistant, chat, demo


def test_idle_state_exposes_no_pending_input(monkeypatch):
    monkeypatch.setattr(demo, "SESSION", None)
    assert demo.response_state()["pending_input"] is None


@pytest.mark.parametrize("reason", ["LOGIN", "CAPTCHA", "VERIFICATION_CODE", "PERSONAL_DATA"])
def test_required_human_reason_has_current_evidence(session, offline, monkeypatch, reason):
    human_page(session, offline, reason)
    monkeypatch.setattr(agent, "choose", blocked(reason))
    view = send(session)
    assert view["messages"][-1]["handoff"]["code"] == reason
    assert view["pending_input"] is None and session.agent.browser.mutations == []


@pytest.mark.parametrize("text,reason", [
    ("Continuar como visitante", "LOGIN"), ("Buscar produtos", "CAPTCHA"),
    ("Aviso dispensável", "OTHER"), ("Escolha a cidade", "PERSONAL_DATA"),
])
def test_unsupported_human_reason_cannot_open_manual_control(session, offline, monkeypatch, text, reason):
    session.agent.browser.page.update(text=text, human_fields=[{"type": "frame", "label": "Publicidade"}])
    offline[2]["blocker"] = {"kind": "human", "reason": reason, "description": "Precisa de você",
                             "evidence": ["Desafio obrigatório inexistente"]}
    monkeypatch.setattr(agent, "choose", blocked(reason))
    view = send(session)
    assert view["chat_status"] == "paused" and not view["manual"]["suggested"]
    assert view["messages"][-1]["kind"] == "technical_pause"
    assert len(session.agent.state["decisions"]) == 2 and session.agent.browser.mutations == []


def ask_city(session, offline, monkeypatch):
    session.agent.browser.page["actions"][2]["label"] = "Cidade"
    offline[2]["blocker"] = {"kind": "input", "reason": "OTHER", "description": "Qual cidade?",
                             "evidence": ["Cidade"]}

    def text(context):
        if not context.get("conversation"):
            raise chat.MissingValue("Required city not supplied")
        return context["conversation"][-1]["content"].split(": ", 1)[1], {"model": "offline", "latency_ms": 1}

    monkeypatch.setattr(agent, "field_text", text)
    return send(session, "digite a cidade para pesquisar hotéis")


def test_missing_ordinary_value_resumes_same_goal_and_observes_new_page(session, offline, monkeypatch):
    view = ask_city(session, offline, monkeypatch)
    assert view["chat_status"] == "awaiting_input" and not view["manual"]["suggested"]
    original_goal, turn = session.goal, session.turn_id
    request = view["pending_input"]["id"]
    previous_observations = session.agent.browser.observations
    session.agent.browser.page["fingerprint"] = "page-changed-while-waiting"
    session.input({"request_id": request, "value": "Belo Horizonte"})
    session.worker.join(3)
    assert session.phase == "completed" and session.pending_input is None
    assert (session.goal, session.turn_id) == (original_goal, turn)
    assert session.agent.browser.observations > previous_observations
    assert session.agent.browser.mutations == [("e3", "Belo Horizonte")]
    assert any(message["kind"] == "input" for message in session.messages if "kind" in message)
    session.input({"request_id": request, "value": "Belo Horizonte"})
    assert len(session.agent.browser.mutations) == 1


def test_cancel_and_stale_input_do_not_mutate_browser(session, offline, monkeypatch):
    view = ask_city(session, offline, monkeypatch)
    request = view["pending_input"]["id"]
    with pytest.raises(ValueError, match="ativa"):
        session.input({"request_id": "wrong", "value": "Recife"})
    with pytest.raises(ValueError, match="valor"):
        session.input({"request_id": request, "value": ""})
    session.input({"request_id": request, "cancel": True})
    assert session.pending_input is None and session.phase == "paused"
    assert session.agent.browser.mutations == []
    session.input({"request_id": request, "value": "Recife"})
    assert session.agent.browser.mutations == []


def test_input_endpoint_rejects_an_old_session(session, offline, monkeypatch):
    view = ask_city(session, offline, monkeypatch)
    monkeypatch.setattr(demo, "SESSION", session)
    with pytest.raises(ValueError, match="conversa"):
        demo.command("input", {"session_id": "old-session", "request_id": view["pending_input"]["id"],
                               "value": "Recife"})
    assert session.pending_input and not session.agent.browser.mutations


def test_pending_question_cannot_be_displaced_by_navigation_or_manual_control(session, offline, monkeypatch):
    view = ask_city(session, offline, monkeypatch)
    with pytest.raises(ValueError, match="pergunta pendente"):
        session.navigate_user({"action": "reload"})
    with pytest.raises(ValueError, match="pergunta pendente"):
        session.manual.start({})
    assert session.pending_input == view["pending_input"] and session.phase == "awaiting_input"
    assert not session.agent.browser.mutations and session.manual.status == "off"


def test_restored_input_is_interrupted_without_replay(session, offline, monkeypatch):
    ask_city(session, offline, monkeypatch)
    restored = chat.ChatSession.restore({"id": session.id, "messages": copy.deepcopy(session.messages),
                                        "phase": "awaiting_input", "last_url": "https://example.org"})
    try:
        assert restored.agent is None and restored.pending_input is None
        assert restored.messages[-1]["kind"] == "interrupted"
    finally:
        restored.close()


def test_human_field_metadata_can_support_a_handoff_without_values(session, offline, monkeypatch):
    session.agent.browser.page["human_fields"] = [{"label": "Senha obrigatória", "type": "password",
                                                  "purpose": "password", "autocomplete": "current-password"}]
    offline[2]["blocker"] = {"kind": "human", "reason": "LOGIN", "description": "Entre na conta",
                             "evidence": ["Senha obrigatória"]}
    monkeypatch.setattr(agent, "choose", blocked("LOGIN"))
    view = send(session)
    assert view["messages"][-1]["kind"] == "handoff"
    assert assistant.page_context(session.agent.state["page"])["human_fields"][0]["type"] == "password"


def test_stale_human_evidence_is_reconsidered_without_handoff(session, offline, monkeypatch):
    human_page(session, offline, "CAPTCHA")
    monkeypatch.setattr(session.agent.browser, "fresh", lambda page, action=None:
                        page["fingerprint"] == session.agent.browser.page["fingerprint"])
    original = assistant.ask
    changed = []

    def ask(kind, context, calls):
        result = original(kind, context, calls)
        if kind == "blocker":
            session.agent.browser.page.update(text="Resultado disponível", fingerprint="challenge-dismissed")
            changed.append(True)
        return result

    def choose(*_a, **_k):
        if not changed:
            return blocked("CAPTCHA")()
        return {"choice": "DONE", "operation": "DONE", "latency_ms": 0, "usage": {},
                "confidence": 1, "probabilities": {"DONE": 1}}

    monkeypatch.setattr(assistant, "ask", ask)
    monkeypatch.setattr(agent, "choose", choose)
    view = send(session)
    assert view["chat_status"] == "completed" and not view["manual"]["suggested"]
    assert not any(m.get("kind") == "handoff" for m in session.messages)


def test_sensitive_reason_cannot_be_asked_as_ordinary_chat_input(session, offline, monkeypatch):
    session.agent.browser.page["actions"][2]["label"] = "E-mail de login"
    offline[2]["blocker"] = {"kind": "input", "reason": "LOGIN", "description": "Informe seu login",
                             "evidence": ["E-mail de login"]}
    monkeypatch.setattr(agent, "field_text", lambda *_a: (_ for _ in ()).throw(chat.MissingValue("missing login")))
    view = send(session, "digite meu e-mail de login")
    assert view["pending_input"] is None and view["chat_status"] == "paused"
    assert not any(m.get("kind") == "input" for m in session.messages)


def test_input_http_requires_session_token_and_same_origin(local_server, offline, monkeypatch):
    client = local_server
    view = client.post("/api/session", json={"url": "https://example.org"}).json()
    session = demo.SESSION
    view = ask_city(session, offline, monkeypatch)
    body = {"session_id": session.id, "request_id": view["pending_input"]["id"], "cancel": True}
    assert client.post("/api/input", json=body, headers={"X-Demo-Token": "wrong"}).status_code == 403
    assert client.post("/api/input", json=body, headers={"Origin": "https://other.example"}).status_code == 403
    assert client.post("/api/input", json={**body, "session_id": "old"}).status_code == 400
    assert session.pending_input
    result = client.post("/api/input", json=body)
    assert result.status_code == 200 and result.json()["pending_input"] is None
    assert session.agent.browser.mutations == []


def test_technical_failure_can_resume_from_a_fresh_page_without_handoff(session, offline):
    offline[2]["bad_safety"] = True
    view = send(session)
    assert view["chat_status"] == "paused" and not view["manual"]["suggested"]
    assert session.agent.browser.mutations == []
    observations = session.agent.browser.observations
    offline[2]["bad_safety"] = False
    session.resume()
    session.worker.join(3)
    assert session.phase == "completed" and session.agent.browser.observations > observations
    assert len(session.agent.browser.mutations) == 1
