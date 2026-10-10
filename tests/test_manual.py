"""Manual ownership, handoff and at-most-once input. All model transports are offline."""
# ruff: noqa: F811

import copy
import json
import threading
import time

import pytest
from test_chat import FakeBrowser, local_server, offline, send  # noqa: F401

from jev_ultrafast import agent, assistant, chat, demo
from jev_ultrafast.manual import validate_event


@pytest.fixture
def session(offline, monkeypatch):  # noqa: F811
    instance = chat.ChatSession("https://example.org")
    browser = instance.agent.browser
    browser.human = []
    browser.document = "one"
    monkeypatch.setattr(browser, "manual_mode", lambda _active: None, raising=False)
    monkeypatch.setattr(browser, "manual_context", lambda follow=True: {
        "document": browser.document, "w": browser.page["w"], "h": browser.page["h"],
        "url": browser.page["url"], "title": "Test", "protected": True}, raising=False)
    monkeypatch.setattr(browser, "manual_event", lambda event, group: browser.human.append(copy.deepcopy(event)),
                        raising=False)
    monkeypatch.setattr(instance.preview, "begin_manual", lambda *_args: instance.preview.invalidate())
    yield instance
    instance.manual.close()
    instance.preview.close()


def take(session):
    token = session.manual.start({})
    session.manual.frame(session.agent.browser.manual_context())
    return token


def event(session, token, sequence=1, **extra):
    return {"owner_token": token, "request_id": sequence, "context": copy.deepcopy(session.manual.context),
            "type": "text", "text": "test-private-code", **extra}


def test_owner_is_private_and_duplicate_input_is_never_repeated(session):
    token = take(session)
    for _ in range(2):
        session.manual.input(event(session, token))
    assert len(session.agent.browser.human) == 1
    view = session.snapshot()
    assert token not in json.dumps(view) and "test-private-code" not in json.dumps(view)
    assert view["user_actions"] == [{"control_id": 1, "request_id": 1, "type": "text", "execution": "executed"}]
    with pytest.raises(ValueError, match="outra interface"):
        session.manual.start({})
    with pytest.raises(ValueError, match="outra interface"):
        session.manual.input(event(session, "wrong"))
    assert session.manual.start({"owner_token": token}) == token  # Reload recovery.


def test_uncertain_input_blocks_followups_until_explicit_reconciliation(session, monkeypatch):
    token = take(session)
    def fail(event, group):
        session.agent.browser.human.append(copy.deepcopy(event))
        raise RuntimeError("test-private-code")
    monkeypatch.setattr(session.agent.browser, "manual_event", fail)
    with pytest.raises(RuntimeError, match="incerto"):
        session.manual.input(event(session, token))
    with pytest.raises(ValueError, match="recupere"):
        session.manual.input(event(session, token, 2))
    assert len(session.agent.browser.human) == 1
    assert "test-private-code" not in json.dumps(session.snapshot())
    with pytest.raises(ValueError, match="Recupere"):
        session.manual.end({"owner_token": token, "resume": True})
    monkeypatch.setattr(session.agent.browser, "manual_event", lambda e, g: session.agent.browser.human.append(e))
    session.manual.start({"owner_token": token})
    assert not session.manual.snapshot()["input_ready"]
    session.manual.frame(session.agent.browser.manual_context())
    session.manual.input(event(session, token, 2))
    assert len(session.agent.browser.human) == 2


def test_navigation_and_resize_require_a_new_image_even_after_size_returns(session):
    token = take(session)
    previous = event(session, token)
    session.resize_viewport(800, 600)
    assert session.manual.context is None
    session.resize_viewport(1120, 780)
    session.manual.frame(session.agent.browser.manual_context())
    with pytest.raises(ValueError, match="imagem mudou"):
        session.manual.input(previous)
    session.agent.browser.document = "two"
    with pytest.raises(ValueError, match="página mudou"):
        session.manual.input(event(session, token))
    assert session.agent.browser.human == [] and session.manual.context is None


def test_expiry_leaves_task_paused_and_revokes_owner(session):
    session.goal, session.phase = "Resolve challenge", "paused"
    token = take(session)
    session.manual.deadline = time.monotonic() - 1
    session.manual.expire()
    view = session.snapshot()
    assert view["manual"]["status"] == "off" and view["chat_status"] == "paused"
    assert session.worker is None and session.manual.token is None
    assert view["timing"]["manual_wait_ms"] >= 0
    with pytest.raises(ValueError, match="outra interface"):
        session.manual.input(event(session, token))


def test_completed_task_does_not_restart_and_verification_becomes_stale(session):
    send(session)
    old_calls = len(session.calls)
    token = take(session)
    assert not session.manual.can_resume
    session.manual.input(event(session, token))
    assert session.snapshot()["messages"][-1]["verification"]["stale"]
    session.manual.end({"owner_token": token, "resume": True})
    assert session.phase == "completed" and len(session.calls) == old_calls
    assert session.snapshot()["can_recheck"]


def test_browsing_between_tasks_keeps_the_phase_and_the_next_request_ends_it(session):
    send(session)
    token = take(session)
    view = session.snapshot()
    assert view["chat_status"] == "completed" and view["manual"]["status"] == "active"
    assert view["manual"]["reason"] == "" and not view["manual"]["can_resume"]
    session.manual.input({"owner_token": token, "request_id": 1, "context": copy.deepcopy(session.manual.context),
                          "type": "wheel", "action": "wheel", "x": 10, "y": 10, "deltaX": 0, "deltaY": 300})
    assert session.agent.browser.human[-1]["type"] == "wheel"
    observed = session.agent.browser.observations
    send(session, "Busque outro resultado", "second")
    assert session.manual.status == "off" and session.manual.token is None
    assert session.agent.browser.observations > observed  # the new task starts from the current page
    assert session.snapshot()["messages"][-1]["verification"]["satisfied"]


def test_handoff_control_still_blocks_a_new_request(session):
    session.goal, session.phase = "Resolve challenge", "paused"
    take(session)
    assert session.manual.can_resume
    with pytest.raises(ValueError, match="Encerre o controle manual"):
        session.message({"message": "Outra coisa", "message_id": "other"})


def test_closing_or_switching_conversation_ends_browsing(session):
    send(session)
    take(session)
    session.close()
    assert session.manual.status == "off"


def human_page(session, offline, reason):
    text = {"LOGIN": "Login obrigatório", "CAPTCHA": "Resolva o CAPTCHA",
            "VERIFICATION_CODE": "Código de verificação obrigatório", "PERSONAL_DATA": "CPF obrigatório"}[reason]
    session.agent.browser.page["text"] = text
    offline[2]["blocker"] = {"kind": "human", "reason": reason, "description": text, "evidence": [text]}
    return text


def test_blocked_checks_evidence_and_resume_observes_current_page(session, monkeypatch, offline):
    initial = human_page(session, offline, "CAPTCHA")
    monkeypatch.setattr(agent, "choose", lambda *_a, **_kw: {
        "choice": "BLOCKED", "operation": "BLOCKED", "target": None, "latency_ms": 1,
        "probabilities": {"BLOCKED": 1}, "confidence": 1, "usage": {}})
    view = send(session)
    assert view["chat_status"] == "paused" and view["manual"]["suggested"]
    assert [c["kind"] for c in session.calls] == ["request", "blocker"]
    token = take(session)
    calls = len(session.calls)
    session.manual.input(event(session, token))
    session.agent.browser.page.update(text="Resultado disponível", fingerprint="human-completed")
    assert len(session.calls) == calls and session.agent.browser.mutations == []
    monkeypatch.setattr(agent, "choose", lambda *_a, **_kw: {
        "choice": "DONE", "operation": "DONE", "target": None, "latency_ms": 1,
        "probabilities": {"DONE": 1}, "confidence": 1, "usage": {}})
    session.manual.end({"owner_token": token, "resume": True})
    session.worker.join(3)
    assert session.phase == "completed" and session.initial_page["text"] == initial
    assert session.agent.state["page"]["fingerprint"] == "human-completed"
    assert session.messages[-1]["verification"]["satisfied"] and session.agent.browser.mutations == []
    before = session.snapshot()["timing"]
    assert abs(before["execution_ms"] - (before["end_to_end_ms"] - before["manual_wait_ms"])) <= 1
    take(session)  # A completed task's later inspection must not reclassify its earlier human wait.
    after = session.snapshot()["timing"]
    assert after["execution_ms"] == before["execution_ms"] and after["manual_wait_ms"] == before["manual_wait_ms"]
    assert len(session.manual.actions) == 1


@pytest.mark.parametrize("stage", ["request", "decision", "safety", "verify"])
def test_handoff_waits_for_inflight_work_and_starts_no_new_action(session, monkeypatch, stage):
    entered, released = threading.Event(), threading.Event()
    original = agent.choose if stage == "decision" else assistant.verify if stage == "verify" else session._ask
    def gate(*args, **kwargs):
        if stage in {"decision", "verify"} or args[0] == stage:
            entered.set()
            assert released.wait(3)
        return original(*args, **kwargs)
    if stage == "decision":
        monkeypatch.setattr(agent, "choose", gate)
    elif stage == "verify":
        monkeypatch.setattr(assistant, "verify", gate)
    else:
        monkeypatch.setattr(session, "_ask", gate)
    session.message({"message": "Busque um resultado", "message_id": stage})
    assert entered.wait(2)
    count = len(session.agent.browser.mutations)
    token = session.manual.start({})
    assert session.manual.status == "requested" and session.pause_requested.is_set()
    with pytest.raises(ValueError, match="imagem atual"):
        session.manual.input(event(session, token))
    released.set()
    session.worker.join(3)
    assert not session.worker.is_alive() and session.manual.status == "active"
    assert len(session.agent.browser.mutations) == count


def test_pending_confirmation_and_prepared_text_are_invalidated(session):
    send(session, "publique o resultado")
    approval = session.pending["id"]
    session.agent.pending_text = {"text": "old"}
    token = take(session)
    assert session.pending is None and session.agent.pending_text is None
    with pytest.raises(ValueError):
        session.approve({"approval_id": approval})
    assert session.agent.browser.mutations == []
    session.manual.end({"owner_token": token, "resume": False})
    assert session.phase == "paused" and session.pending is None


def test_restart_keeps_history_without_input_authorization(session):
    token = take(session)
    restored = chat.ChatSession.restore({"id": session.id, "messages": copy.deepcopy(session.messages),
                                         "phase": "manual", "last_url": session.last_url})
    assert restored.manual.status == "off" and restored.manual.token is None
    assert restored.messages[-1]["kind"] == "interrupted"
    assert restored.agent is None and restored.worker is None
    assert token not in json.dumps(restored.snapshot())


@pytest.mark.parametrize("bad", [
    {"type": "javascript", "script": "alert(1)"}, {"type": "key", "key": "F5", "action": "down"},
    {"type": "pointer", "action": "down", "x": -1, "y": 1},
    {"type": "pointer", "action": "down", "x": float("nan"), "y": 1},
    {"type": "text", "text": ""}, {"type": "text", "text": "x", "modifiers": True},
])
def test_manual_boundary_rejects_unsupported_events(bad):
    with pytest.raises(ValueError):
        validate_event(bad, {"w": 500, "h": 500})


def test_http_manual_requires_local_auth_session_owner_and_current_frame(local_server, monkeypatch):  # noqa: F811
    client = local_server
    view = client.post("/api/session", json={"url": "https://example.org"}).json()
    session, browser = demo.SESSION, demo.SESSION.agent.browser
    monkeypatch.setattr(browser, "manual_mode", lambda _active: None, raising=False)
    monkeypatch.setattr(browser, "manual_context", lambda follow=True: {"document": "one", "w": 1120, "h": 780},
                        raising=False)
    inputs = []
    monkeypatch.setattr(browser, "manual_event", lambda event, group: inputs.append(event), raising=False)
    monkeypatch.setattr(session.preview, "begin_manual", lambda *_a: session.preview.invalidate())
    body = {"session_id": view["session_id"]}
    assert client.post("/api/manual/start", json={"session_id": "old"}).status_code == 400
    for headers in ({"X-Demo-Token": "wrong"}, {"Origin": "https://other.test"}):
        assert client.post("/api/manual/start", json=body, headers=headers).status_code == 403
    taken = client.post("/api/manual/start", json=body).json()
    token = taken["manual_owner_token"]
    assert token not in client.get("/api/state").text and token not in client.get("/api/state?compact=1").text
    assert not client.get("/api/state?compact=1").json()["manual"]["owned"]
    assert client.get("/api/state?compact=1", headers={"X-Manual-Control": token}).json()["manual"]["owned"]
    resize = {**body, "width": 800, "height": 600}
    assert client.post("/api/viewport", json=resize).status_code == 400
    assert client.post("/api/manual/start", json=body).status_code == 400
    session.manual.frame(browser.manual_context())
    request = {**body, **event(session, token)}
    assert client.post("/api/manual/input", json={**request, "selector": "#secret"}).status_code == 400
    assert client.post("/api/manual/input", json=request).status_code == 200
    assert client.post("/api/manual/input", json=request).status_code == 200
    assert len(inputs) == 1 and "test-private-code" not in client.get("/api/state").text
    assert client.post("/api/manual/end", json={**body, "owner_token": "wrong"}).status_code == 400
    assert client.post("/api/manual/end", json={**body, "owner_token": token}).status_code == 200


def test_expired_uncertain_input_cannot_resume_without_explicit_recovery(session, monkeypatch):
    session.goal, session.phase = "Task", "paused"
    token = take(session)
    monkeypatch.setattr(session.agent.browser, "manual_event", lambda *_a: (_ for _ in ()).throw(RuntimeError("lost")))
    with pytest.raises(RuntimeError):
        session.manual.input(event(session, token))
    session.manual.deadline = time.monotonic() - 1
    session.manual.expire()
    assert session.manual.token is None and session.manual.needs_reconcile
    with pytest.raises(ValueError, match="recupere"):
        session.resume()
    with pytest.raises(ValueError, match="recupere"):
        session.message({"message": "Another task", "message_id": "next"})
    take(session)
    assert not session.manual.needs_reconcile


def test_slow_manual_capture_does_not_block_input_and_discards_old_viewport(session, monkeypatch):
    from jev_ultrafast.preview import MANUAL_INTERVAL, Preview

    camera = session.agent.browser
    entered, released = threading.Event(), threading.Event()
    starts = []
    def capture():
        starts.append(time.monotonic())
        entered.set()
        assert released.wait(3)
        return "manual-private-image"
    monkeypatch.setattr(camera, "capture", capture, raising=False)
    monkeypatch.setattr(session.preview, "begin_manual", lambda *args: Preview.begin_manual(session.preview, *args))
    try:
        token = take(session)
        assert entered.wait(1)
        started = time.monotonic()
        session.manual.input(event(session, token))
        assert time.monotonic() - started < 0.3 and not released.is_set()
        session.resize_viewport(800, 600)
        released.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            captured = session.preview.snapshot()["capture"]
            if captured:
                break
            time.sleep(0.01)
        assert captured["page"]["w"] == 800 and captured["manual"] and not captured["elements"]
        assert len(starts) >= 2 and starts[1] - starts[0] >= MANUAL_INTERVAL - 0.01
        assert not session.snapshot()["page"].get("screenshot")
    finally:
        released.set()
        session.preview.close()
        session.preview.worker.join(3)



def test_manual_preview_streams_screencast_frames_and_stops_on_exit(session, monkeypatch):
    from jev_ultrafast.preview import Preview

    camera = session.agent.browser
    frames = {"number": 1}
    stopped = []
    monkeypatch.setattr(camera, "screencast", lambda: (f"frame-{frames['number']}", frames["number"]), raising=False)
    monkeypatch.setattr(camera, "stop_screencast", lambda: stopped.append(True), raising=False)
    monkeypatch.setattr(camera, "capture", lambda: pytest.fail("polled screenshot while streaming"), raising=False)
    monkeypatch.setattr(session.preview, "begin_manual", lambda *args: Preview.begin_manual(session.preview, *args))

    def shown(expected):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            captured = session.preview.snapshot()["capture"]
            if captured and captured["page"]["screenshot"] == expected:
                return captured
            time.sleep(0.01)
        raise AssertionError(f"{expected} was not shown")

    try:
        token = take(session)
        first = shown("frame-1")
        time.sleep(0.2)  # no repaint: the cached frame and its revision stay as they are
        assert session.preview.snapshot()["capture"]["revision"] == first["revision"]
        frames["number"] = 2
        assert shown("frame-2")["revision"] > first["revision"]
        session.manual.end({"owner_token": token, "resume": False})
        assert stopped
    finally:
        session.preview.close()
        session.preview.worker.join(3)


def blocked(reason):
    return lambda *_a, **_kw: {
        "choice": "BLOCKED", "operation": "BLOCKED", "target": None, "latency_ms": 1, "blocked_reason": reason,
        "probabilities": {"BLOCKED": 1}, "confidence": 1, "usage": {}}


@pytest.mark.parametrize("answers, phase", [
    (["OTHER", "DONE"], "completed"),  # a vague block that had a way forward: the user is never asked
    ([None, "DONE"], "completed"),
    (["OTHER", "OTHER"], "paused"),  # pause technically, without asking for control
    (["CAPTCHA"], "paused"),  # only the user can solve it: ask at once
])
def test_vague_block_is_reconsidered_once_before_asking_the_user(session, monkeypatch, offline, answers, phase):
    if answers == ["CAPTCHA"]:
        human_page(session, offline, "CAPTCHA")
    feedback = []

    def choose(*_a, verification_feedback=None, **_kw):
        feedback.append(verification_feedback)
        answer = answers[len(feedback) - 1]
        if answer == "DONE":
            return {"choice": "DONE", "operation": "DONE", "target": None, "latency_ms": 1,
                    "probabilities": {"DONE": 1}, "confidence": 1, "usage": {}}
        return blocked(answer)()

    monkeypatch.setattr(agent, "choose", choose)
    send(session)
    session.worker.join(3)
    assert session.phase == phase and len(feedback) == len(answers)
    assert any(m.get("kind") == "handoff" for m in session.messages) is (answers == ["CAPTCHA"])
    assert feedback[0] is None and all(item[-1] == chat.RECONSIDER_BLOCKED for item in feedback[1:])
    assert session.agent.browser.mutations == []


def test_handoff_card_names_the_reason_and_continue_resumes_automatically(session, monkeypatch, offline):
    human_page(session, offline, "CAPTCHA")
    monkeypatch.setattr(agent, "choose", blocked("CAPTCHA"))
    view = send(session)
    card = view["messages"][-1]
    assert view["chat_status"] == "paused" and card["kind"] == "handoff"
    assert card["handoff"] == {"code": "CAPTCHA"} and "CAPTCHA" in card["content"]
    assert view["manual"]["suggested"]
    token = take(session)
    session.manual.input(event(session, token))
    session.agent.browser.page.update(text="Resultado disponível", fingerprint="human-solved")
    monkeypatch.setattr(agent, "choose", lambda *_a, **_kw: {
        "choice": "DONE", "operation": "DONE", "target": None, "latency_ms": 1,
        "probabilities": {"DONE": 1}, "confidence": 1, "usage": {}})
    session.manual.end({"owner_token": token, "resume": True})
    session.worker.join(3)
    assert session.phase == "completed"
    returned = session.agent.state["history"][-1]
    assert returned["kind"] == "manual" and returned["execution"] == "executed" and returned["page_changed"]
    kinds = [(m["role"], m.get("kind")) for m in session.messages]
    assert kinds[-3:] == [("assistant", "handoff"), ("user", "confirmation"), ("assistant", "result")]
    assert session.messages[-2]["content"] == "Continuar com Jev"


def test_exit_manual_keeps_the_task_paused(session, monkeypatch, offline):
    human_page(session, offline, "LOGIN")
    monkeypatch.setattr(agent, "choose", blocked("LOGIN"))
    send(session)
    token = take(session)
    session.manual.end({"owner_token": token, "resume": False})
    assert session.phase == "paused" and session.messages[-1]["kind"] == "handoff"
    assert session.agent.state["history"][-1]["kind"] == "manual"


def test_value_only_the_user_has_opens_the_personal_data_handoff(session, monkeypatch, offline):
    human_page(session, offline, "PERSONAL_DATA")
    from jev_ultrafast import agent as loop
    monkeypatch.setattr(loop, "field_text", lambda _context: (_ for _ in ()).throw(
        chat.MissingValue("Goal does not supply this field's value; nothing typed.")))
    view = send(session, "digite meu CPF no campo")
    assert view["chat_status"] == "paused"
    assert view["messages"][-1]["kind"] == "handoff"
    assert view["messages"][-1]["handoff"]["code"] == "PERSONAL_DATA"
    assert session.agent.browser.mutations == []


def test_no_progress_asks_for_help_instead_of_finishing(session, monkeypatch):
    monkeypatch.setattr(agent, "choose", blocked(None))
    send(session)
    session.agent.state["status"] = "blocked"
    session.agent.state["stop_reason"] = "repeated two-action loop"
    session._run()
    card = session.messages[-1]
    assert session.phase == "paused" and card["kind"] == "technical_pause" and "handoff" not in card
    assert session.agent.state["status"] == "ready"

