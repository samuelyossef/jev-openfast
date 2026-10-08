"""Chat integration is offline: real Agent loop, fake browser and fake provider transport."""

import copy
import json
import re
import threading
import time
from http.server import ThreadingHTTPServer

import httpx
import pytest

from jev_ultrafast import agent, assistant, chat, demo, model
from jev_ultrafast.browser import StalePage


class FakeBrowser:
    instances = []

    def __init__(self, url, *, viewport=None):
        self.instances.append(self)
        self.mutations = []
        self.observations = 0
        self.changed = False
        self.fail_after_action = False
        self.interrupt_action = False
        self.stale_before_action = False
        self.closed = False
        self.resizes = []
        self.page = {
            "url": url,
            "title": "Página de teste",
            "text": "Pronta",
            "fingerprint": "initial",
            "screenshot": "",
            "w": 1120,
            "h": 780,
            "actions": [
                {"id": "e1", "node": 1, "kind": "click", "role": "button", "label": "Buscar"},
                {"id": "e2", "node": 2, "kind": "click", "role": "button", "label": "Publicar"},
                {"id": "e3", "node": 3, "kind": "fill", "role": "textbox", "label": "Título", "value": ""},
            ],
        }
        if viewport:
            self.page.update(w=viewport[0], h=viewport[1])

    def set_viewport(self, width, height):
        self.resizes.append((width, height))
        self.page.update(w=width, h=height)

    def observe(self, screenshot=True):
        self.observations += 1
        if self.fail_after_action and self.mutations:
            self.fail_after_action = False
            raise StalePage("Observation interrupted after execution")
        return copy.deepcopy(self.page)

    def fresh(self, page, action=None):
        return not self.changed

    def navigate(self, url):
        if self.page["url"] != "about:blank":  # leaving the blank tab opened in advance is part of opening
            self.mutations.append(("navigate", url))
        self.page.update(url=url, text="Pronta", fingerprint=f"opened-{url}")

    def act(self, action, page, text=None):
        if self.stale_before_action:
            self.stale_before_action = False
            raise StalePage("Changed before input")
        self.mutations.append((action["id"], text))
        if self.interrupt_action:
            raise RuntimeError("Mutation result unknown")
        self.page.update(text="Resultado disponível", fingerprint=f"result-{len(self.mutations)}")

    def close(self):
        self.closed = True


@pytest.fixture
def offline(monkeypatch):
    FakeBrowser.instances = []
    monkeypatch.setattr(agent, "Browser", FakeBrowser)
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-test-key")
    monkeypatch.setattr(model.CLIENT, "post", lambda *_a, **_k: pytest.fail("Tests cannot call a real provider"))
    transport_calls = []
    decision_calls = []
    config = {"bad_safety": False, "bad_safety_once": False, "bad_evidence": False,
              "false_verdict": False, "force_success_reply": False, "block": None, "route": None,
              "verify_sequence": [], "task": None}

    def choose(page, goal, history, **kwargs):
        decision_calls.append({"goal": goal, "history": copy.deepcopy(history), **kwargs})
        executed = any(item.get("execution", "executed") == "executed" for item in history)
        selected = "DONE" if executed else "e3" if "digite" in goal else "e2" if "publique" in goal.lower() else "e1"
        operation = "DONE" if selected == "DONE" else "TYPE_TEXT" if selected == "e3" else "CLICK"
        return {
            "choice": selected,
            "operation": operation,
            "target": selected.removeprefix("e"),
            "probabilities": {selected: 1},
            "confidence": 1,
            "latency_ms": 1,
            "usage": {},
        }

    def post(_url, _key, body, *, on_attempt=None):
        if on_attempt:
            on_attempt()
        kind = body["response_format"]["json_schema"]["name"].removeprefix("chat_")
        context = json.loads(body["messages"][1]["content"])
        transport_calls.append((kind, context))
        if config["block"] and kind == "request":
            config["block"][0].set()
            assert config["block"][1].wait(5)
        if kind == "request":
            url = re.search(r"https?://[^\s]+", context["goal"])
            output = config["route"] or {
                "url": url.group() if url else "https://example.org" if not context["current_url"] else "-",
                "reply": "Vou verificar o destino.",
            }
            output = {"intent": "answer" if context["goal"].startswith("?") else "task",
                      "task": config["task"] or context["goal"], **output}
        elif kind == "answer":
            output = {
                "reply": context["page"]["text"],
            }
        elif kind == "safety":
            output = {
                "effect": "confirm" if "publique" in context["goal"].lower() else "safe",
                "description": "Publicar este conteúdo?",
            }
            if config["bad_safety"] or config["bad_safety_once"]:
                output["effect"] = "invented"
                config["bad_safety_once"] = False
        elif kind == "verify":
            confirmed = not config["false_verdict"]
            output = config["verify_sequence"].pop(0) if config["verify_sequence"] else {
                "satisfied": confirmed,
                "checks": [{"requirement": context["goal"], "status": "confirmed" if confirmed else "unknown",
                            "evidence": ["Evidência inventada" if config["bad_evidence"] else "Resultado disponível"],
                            "reason": "Resultado observado." if confirmed else "Evidência insuficiente."}],
            }
            output = {**output, "reply": "Encontrei o resultado."}
        else:
            output = {
                "reply": "Encontrei o resultado."
                if context["verification"]["satisfied"] or config["force_success_reply"]
                else "Não consegui confirmar que seu pedido foi concluído."
            }
        return {"choices": [{"message": {"content": json.dumps(output)}}], "usage": {"total_tokens": 10}}

    monkeypatch.setattr(agent, "choose", choose)
    monkeypatch.setattr(assistant, "post_json", post)
    monkeypatch.setattr(
        agent,
        "field_text",
        lambda _context: ("Título aprovado", {"model": "offline", "latency_ms": 1}),
    )
    yield transport_calls, decision_calls, config
    for browser in FakeBrowser.instances:
        browser.close()


def send(session, message="Busque um resultado", message_id="first"):
    session.message({"message": message, "message_id": message_id})
    session.worker.join(5)
    assert not session.worker.is_alive()
    return session.snapshot()


def test_task_removes_two_helpers_and_records_stage_timing(offline):
    session = chat.ChatSession("https://example.org")
    view = send(session)
    assert [call["kind"] for call in view["chat_calls"]] == ["request", "safety", "verify"]
    assert view["messages"][-1]["verification"]["satisfied"]
    timing = view["timing"]
    assert timing["first_action_ms"] is not None
    assert timing["end_to_end_ms"] >= timing["first_action_ms"]
    assert timing["approval_wait_ms"] == 0
    stages = {"helper_request", "helper_safety", "helper_verify", "decision_model", "publication"}
    assert stages <= set(timing["stages"])


def test_selected_locale_is_forwarded_to_every_chat_helper(offline):
    calls, _, _ = offline
    session = chat.ChatSession("https://example.org")
    session.message({"message": "Busque um resultado", "message_id": "localized", "locale": "es"})
    session.worker.join(5)
    assert not session.worker.is_alive()
    assert session.snapshot()["locale"] == "es"
    assert calls
    assert all(context["response_language"] == "Spanish" for _, context in calls)


def test_text_and_safety_overlap_without_early_input(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    text_started, safety_started = threading.Event(), threading.Event()
    original = session._ask

    def generate(_context):
        text_started.set()
        assert safety_started.wait(2)
        assert not session.agent.browser.mutations
        return "Título aprovado", {"model": "offline", "latency_ms": 1}

    def ask(kind, context):
        if kind == "safety":
            safety_started.set()
            assert text_started.wait(2)
            assert not session.agent.browser.mutations
        return original(kind, context)

    monkeypatch.setattr(agent, "field_text", generate)
    monkeypatch.setattr(session, "_ask", ask)
    view = send(session, "digite o título")
    assert view["messages"][-1]["verification"]["satisfied"]
    assert session.agent.browser.mutations == [("e3", "Título aprovado")]
    assert len(view["text_calls"]) == view["timing"]["stages"]["text_model"]["count"] == 1


def test_done_after_page_change_verifies_fresh_observation_once(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    browser = session.agent.browser
    original = agent.choose

    def choose(page, goal, history, **kwargs):
        result = original(page, goal, history, **kwargs)
        if result["choice"] == "DONE":
            browser.page["fingerprint"] = "changed-during-DONE"
        return result

    monkeypatch.setattr(agent, "choose", choose)
    monkeypatch.setattr(browser, "fresh", lambda page, action=None:
                        action is not None or page["fingerprint"] == browser.page["fingerprint"])
    view = send(session)
    assert view["messages"][-1]["verification"]["satisfied"]
    assert len(view["decisions"]) == 2
    assert len(browser.mutations) == 1


def test_changed_negative_verdict_cannot_trigger_a_corrective_action(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    offline[2]["verify_sequence"] = [{"satisfied": False, "checks": [
        {"requirement": "Aplicar filtro", "status": "not_met", "evidence": ["Resultado disponível"],
         "reason": "Filtro ausente."}]}]
    moved = iter(range(1, 10))
    original = assistant.verify

    def verify(context, calls):
        result = original(context, calls)
        session.agent.browser.page["url"] = f"https://example.org/moved-{next(moved)}"
        return result

    monkeypatch.setattr(assistant, "verify", verify)
    view = send(session)
    assert not view["messages"][-1]["verification"]["satisfied"]
    assert "mudou durante" in view["messages"][-1]["content"]
    assert len(session.agent.browser.mutations) == 1 and session.recovery_attempts == 0


def test_blocked_capture_cannot_delay_task_or_compact_state(offline, monkeypatch):
    entered, released = threading.Event(), threading.Event()

    def capture(_browser):
        entered.set()
        assert released.wait(3)
        return "offline-image"

    monkeypatch.setattr(FakeBrowser, "capture", capture, raising=False)
    session = chat.ChatSession("https://example.org")
    try:
        assert entered.wait(2)
        view = send(session)
        assert view["chat_status"] == "completed" and not released.is_set()
        compact = session.snapshot(compact=True)
        omitted = {"decisions", "history", "elements", "text_calls", "chat_calls", "initial_page", "turns"}
        assert not omitted & compact.keys()
        assert "screenshot" not in compact["page"] and "events" not in compact["timing"]
        assert compact["action_count"] == 1
    finally:
        session.close()
        released.set()
        session.preview.worker.join(3)
    assert session.preview.snapshot()["capture"] is None


def test_approval_wait_is_excluded_from_execution_clock(offline):
    session = chat.ChatSession("https://example.org")
    view = send(session, "Publique o conteúdo")
    time.sleep(0.03)
    waiting = session.snapshot()["timing"]
    assert waiting["approval_wait_ms"] >= 20
    session.approve({"approval_id": view["approval"]["id"]})
    session.worker.join(5)
    timing = session.snapshot()["timing"]
    assert timing["end_to_end_ms"] - timing["execution_ms"] == pytest.approx(timing["approval_wait_ms"], abs=1)


def test_compact_and_preview_http_are_read_only_and_session_bound(local_server):
    client = local_server
    sid = client.post("/api/session", json={"url": "https://example.org"}).json()["session_id"]
    compact = client.get("/api/state?compact=1").json()
    full = client.get("/api/state").json()
    assert "decisions" not in compact and "decisions" in full
    assert "actions" not in compact["page"] and "actions" in full["page"]
    assert client.get("/api/preview?session_id=old").status_code == 404
    assert client.get(f"/api/preview?session_id={sid}").json()["session_id"] == sid
    assert not FakeBrowser.instances[0].mutations


def test_repeated_stale_targets_pause_before_more_paid_helpers(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    monkeypatch.setattr(session.agent.browser, "fresh", lambda page, action=None: action is None)
    view = send(session)
    assert view["chat_status"] == "paused"
    assert "três tentativas" in view["progress"]
    assert len(offline[1]) == 3
    assert not any(kind == "safety" for kind, _ in offline[0])
    assert not session.agent.browser.mutations
    monkeypatch.setattr(session.agent.browser, "fresh", lambda page, action=None: True)
    session.resume()
    session.worker.join(5)
    assert session.snapshot()["chat_status"] == "completed"
    assert len(session.agent.browser.mutations) == 1


def test_pause_during_approval_check_prevents_input(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    view = send(session, "Publique o conteúdo")
    def fresh(page, action=None):
        session.pause_requested.set()
        return True
    monkeypatch.setattr(session.agent.browser, "fresh", fresh)
    session.approve({"approval_id": view["approval"]["id"]})
    session.worker.join(5)
    assert session.snapshot()["chat_status"] == "paused"
    assert session.pending is None
    assert not session.agent.browser.mutations


def test_running_snapshot_updates_elapsed_without_another_operation(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    session.agent.state["started_at"] = 5
    session.phase = "running"
    session._publish()
    monkeypatch.setattr(chat.time, "perf_counter", lambda: 6.25)
    assert session.snapshot()["elapsed_ms"] == 1250


def test_environment_loads_only_current_project(tmp_path, monkeypatch):
    repo = tmp_path / "project"
    repo.mkdir()
    (tmp_path / ".env").write_text("JEV_REVIEW_ENV_CHECK=wrong\n")
    (repo / ".env").write_text("JEV_REVIEW_ENV_CHECK=correct\n")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("JEV_REVIEW_ENV_CHECK", raising=False)
    demo.load_environment()
    import os
    assert os.environ["JEV_REVIEW_ENV_CHECK"] == "correct"


@pytest.mark.parametrize("width,height", [(0, 300), (300, 4097), (True, 300), (300.0, 300), ("300", 300), (300, None)])
def test_viewport_rejects_invalid_dimensions(offline, width, height):
    session = chat.ChatSession("https://example.org")
    with pytest.raises(ValueError, match="inteiros"):
        session.resize_viewport(width, height)
    assert not session.agent.browser.resizes


def test_idle_viewport_updates_without_models_and_logs_before_browser_work(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    browser = session.agent.browser
    original = browser.set_viewport

    def resize(width, height):
        assert session.snapshot()["viewport_events"][-1]["status"] == "requested"
        original(width, height)

    monkeypatch.setattr(browser, "set_viewport", resize)
    session.resize_viewport(360, 640)
    session.resize_viewport(360, 640)
    view = session.snapshot()
    assert (view["page"]["w"], view["page"]["h"]) == (360, 640)
    assert browser.resizes == [(360, 640)]
    assert browser.observations == 2
    assert not offline[0] and not offline[1] and not browser.mutations
    assert view["viewport_events"][-1]["status"] == "observed"


def test_viewport_queues_latest_size_during_work(offline):
    started, release = threading.Event(), threading.Event()
    offline[2]["block"] = (started, release)
    session = chat.ChatSession("https://example.org")
    session.message({"message": "Busque", "message_id": "resize-running"})
    assert started.wait(3)
    try:
        session.resize_viewport(400, 600)
        session.resize_viewport(390, 700)
        assert not session.agent.browser.resizes
    finally:
        release.set()
        session.worker.join(5)
    view = session.snapshot()
    assert session.agent.browser.resizes == [(390, 700)]
    assert len(session.agent.browser.mutations) == 1
    assert view["chat_status"] == "completed"
    assert (view["page"]["w"], view["page"]["h"]) == (390, 700)


def test_viewport_waits_until_an_executed_mutation_is_logged(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    browser = session.agent.browser
    original = browser.act

    def act(action, page, text=None):
        session.resize_viewport(320, 500)
        assert not browser.resizes
        return original(action, page, text)

    monkeypatch.setattr(browser, "act", act)
    view = send(session)
    assert len(browser.mutations) == len(view["history"]) == 1
    assert browser.resizes == [(320, 500)]
    assert view["history"][0]["execution"] == "executed"


def test_viewport_discards_pending_confirmation_and_resume_selects_again(offline):
    session = chat.ChatSession("https://example.org")
    old_id = send(session, "Publique o conteúdo")["approval"]["id"]
    session.resize_viewport(360, 640)
    assert session.snapshot()["chat_status"] == "paused"
    assert session.snapshot()["approval"] is None
    with pytest.raises(ValueError, match="confirmação"):
        session.approve({"approval_id": old_id})
    session.resume()
    session.worker.join(5)
    assert session.snapshot()["approval"]["id"] != old_id
    assert not session.agent.browser.mutations


def test_queued_viewport_invalidates_an_already_consumed_approval(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    approval_id = send(session, "Publique o conteúdo")["approval"]["id"]
    started, release = threading.Event(), threading.Event()
    original = session._approved

    def approved(pending):
        started.set()
        assert release.wait(5)
        original(pending)

    monkeypatch.setattr(session, "_approved", approved)
    session.approve({"approval_id": approval_id})
    assert started.wait(3)
    try:
        session.resize_viewport(360, 640)
    finally:
        release.set()
        session.worker.join(5)
    assert session.snapshot()["chat_status"] == "paused"
    assert not session.agent.browser.mutations


def test_viewport_queued_during_final_verification_is_applied_before_worker_exits(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    original = assistant.verify

    def verify(context, calls):
        session.resize_viewport(360, 640)
        assert not session.agent.browser.resizes
        return original(context, calls)

    monkeypatch.setattr(assistant, "verify", verify)
    view = send(session)
    assert session.agent.browser.resizes == [(360, 640)]
    assert len(session.agent.browser.mutations) == 1
    assert view["chat_status"] == "completed"
    assert (view["page"]["w"], view["page"]["h"]) == (360, 640)
    assert not session.browser_busy


def test_uncertain_viewport_is_reported_and_never_retried(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    calls = []

    def resize(*dimensions):
        calls.append(dimensions)
        raise TimeoutError("CDP timeout")

    monkeypatch.setattr(session.agent.browser, "set_viewport", resize)
    session.resize_viewport(360, 640)
    session.resize_viewport(360, 640)
    view = session.snapshot()
    assert calls == [(360, 640)]
    assert "CDP timeout" in view["viewport_error"]
    assert view["viewport_events"][-1]["status"] == "uncertain"
    assert not session.browser_busy


def test_two_tasks_reuse_tab_and_pass_conversation(offline):
    calls, decisions, _ = offline
    session = chat.ChatSession("https://example.org")
    assert send(session)["chat_status"] == "completed"
    view = send(session, "Agora filtre o mesmo resultado", "second")
    assert len(FakeBrowser.instances) == 1
    assert len(session.agent.browser.mutations) == 2
    assert len(view["history"]) == 1
    assert len(view["messages"]) == 4 and len(view["turns"]) == 1
    assert decisions[2]["goal"] == "Agora filtre o mesmo resultado"
    assert decisions[2]["conversation"][0]["content"] == "Busque um resultado"
    assert [kind for kind, _ in calls] == ["request", "safety", "verify"] * 2
    assert all(call["attempts"] == 1 and call["status"] == "ok" for call in view["chat_calls"])
    assert all("screenshot" not in context["page"] for _, context in calls if "page" in context)
    first_verification = next(context for kind, context in calls if kind == "verify")
    assert first_verification["initial_page"]["text"] == "Pronta"
    assert first_verification["page"]["text"] == "Resultado disponível"


def test_first_message_opens_site_and_followup_navigates_same_tab(offline):
    session = chat.ChatSession()
    assert session.snapshot()["page"] is None
    first = send(session, "Busque em https://example.org", "first")
    assert first["chat_status"] == "completed"
    assert first["page"]["url"] == "https://example.org"
    browser = session.agent.browser
    second = send(session, "Busque em https://example.net", "second")
    assert session.agent.browser is browser
    assert len(FakeBrowser.instances) == 1
    assert browser.mutations.count(("navigate", "https://example.net")) == 1
    assert second["page"]["url"] == "https://example.net"
    assert second["navigation"][-1]["status"] == "opened"


def assert_only_blank_tabs_closed():
    """A tab opened in advance on about:blank is closed unused when no destination is accepted."""
    assert all(browser.closed and browser.page["url"] == "about:blank" and not browser.mutations
               for browser in FakeBrowser.instances)


def test_route_can_clarify_without_opening_browser(offline):
    offline[2]["route"] = {"url": "-", "reply": "Qual site devo usar?"}
    session = chat.ChatSession()
    view = send(session, "Faça isso", "first")
    assert view["chat_status"] == "answered"
    assert "Qual site devo abrir?" in view["messages"][-1]["content"]
    assert_only_blank_tabs_closed()


def test_route_cannot_answer_page_question_without_observation(offline):
    offline[2]["route"] = {"mode": "answer", "url": "-", "reply": "O título é conhecido."}
    view = send(chat.ChatSession(), "Qual é o título da página inicial da Wikipédia?", "first")
    assert view["chat_status"] == "error"
    assert_only_blank_tabs_closed()


def test_inferred_local_url_requires_explicit_user_url(offline):
    offline[2]["route"] = {"url": "http://127.0.0.1:9999/", "reply": "Vou abrir."}
    view = send(chat.ChatSession(), "Abra a página de teste", "first")
    assert view["chat_status"] == "error"
    assert_only_blank_tabs_closed()


def test_question_observes_fresh_page_without_acting(offline):
    _, decisions, _ = offline
    session = chat.ChatSession("http://127.0.0.1:8080")
    session.agent.browser.page["text"] = "Conteúdo novo da página"
    view = send(session, "? O que aparece nesta página?")
    assert view["chat_status"] == "answered"
    assert view["messages"][-1]["content"] == "Conteúdo novo da página"
    assert session.agent.browser.observations == 2
    assert not session.agent.browser.mutations and not decisions


def test_approval_is_required_and_consumed_once(offline):
    session = chat.ChatSession("https://example.org")
    view = send(session, "Publique o conteúdo")
    assert view["chat_status"] == "awaiting_confirmation"
    assert not session.agent.browser.mutations and view["decision"] is None
    assert "decision" not in view["approval"]
    body = {"approval_id": view["approval"]["id"]}
    session.approve(body)
    session.worker.join(5)
    assert len(session.agent.browser.mutations) == 1
    assert session.snapshot()["messages"][-1]["verification"]["satisfied"]
    with pytest.raises(ValueError):
        session.approve(body)
    assert len(session.agent.browser.mutations) == 1


def test_reject_stops_without_mutation_or_extra_model_calls(offline):
    calls, _, _ = offline
    session = chat.ChatSession("https://example.org")
    view = send(session, "Publique o conteúdo")
    with pytest.raises(ValueError, match="pendente"):
        session.message({"message": "Outro pedido", "message_id": "second"})
    session.reject({"approval_id": view["approval"]["id"]})
    assert session.snapshot()["chat_status"] == "completed"
    assert not session.agent.browser.mutations
    assert [kind for kind, _ in calls] == ["request", "safety"]


def test_changed_page_invalidates_approval(offline):
    session = chat.ChatSession("https://example.org")
    view = send(session, "Publique o conteúdo")
    old_id = view["approval"]["id"]
    session.agent.browser.changed = True
    session.approve({"approval_id": old_id})
    session.worker.join(5)
    assert session.snapshot()["chat_status"] == "paused"
    assert not session.agent.browser.mutations
    session.agent.browser.changed = False
    session.resume()
    session.worker.join(5)
    assert session.snapshot()["approval"]["id"] != old_id
    assert not session.agent.browser.mutations


def test_approved_fill_shows_and_reuses_exact_prepared_text(offline):
    session = chat.ChatSession("https://example.org")
    view = send(session, "digite e publique o título")
    assert "Título aprovado" in view["approval"]["description"]
    assert len(view["text_calls"]) == 1
    session.approve({"approval_id": view["approval"]["id"]})
    session.worker.join(5)
    assert session.agent.browser.mutations == [("e3", "Título aprovado")]
    assert len(session.snapshot()["text_calls"]) == 1


def test_stale_approved_action_requires_new_confirmation(offline):
    session = chat.ChatSession("https://example.org")
    view = send(session, "Publique o conteúdo")
    session.agent.browser.stale_before_action = True
    session.approve({"approval_id": view["approval"]["id"]})
    session.worker.join(5)
    current = session.snapshot()
    assert current["chat_status"] == "paused"
    assert current["history"][-1]["execution"] == "not_executed"
    assert not session.agent.browser.mutations


def test_stale_observation_never_repeats_executed_mutation(offline):
    session = chat.ChatSession("https://example.org")
    session.agent.browser.fail_after_action = True
    view = send(session)
    assert len(session.agent.browser.mutations) == len(view["history"]) == 1
    assert view["history"][0]["execution"] == "executed"
    assert view["history"][0]["page_changed"] is None
    assert view["chat_status"] == "completed"


def test_unknown_mutation_result_stops_without_retry(offline):
    session = chat.ChatSession("https://example.org")
    session.agent.browser.interrupt_action = True
    view = send(session)
    assert view["chat_status"] == "error"
    assert len(session.agent.browser.mutations) == 1
    assert len(view["history"]) == 1 and view["history"][0]["execution"] == "uncertain"
    assert not view["messages"][-1]["verification"]["satisfied"]


@pytest.mark.parametrize("setting", ["false_verdict", "bad_evidence"])
def test_done_does_not_prove_completion(offline, setting):
    offline[2][setting] = True
    offline[2]["force_success_reply"] = True
    view = send(chat.ChatSession("https://example.org"))
    assert view["status"] == "done"
    assert view["messages"][-1]["verification"]["satisfied"] is False
    assert "Não consegui confirmar" in view["messages"][-1]["content"]
    assert "Encontrei o resultado" not in view["messages"][-1]["content"]
    assert all(call["kind"] != "reply" for call in view["chat_calls"])


@pytest.mark.parametrize("second_satisfied", [True, False])
def test_premature_done_gets_bounded_corrective_rounds(offline, monkeypatch, second_satisfied):
    calls, decisions, config = offline
    # An unknown check no longer prevents correcting a visibly missing requirement.
    missing = {"satisfied": False, "checks": [
        {"requirement": "Abrir resultado", "status": "not_met", "evidence": ["Resultado disponível"],
         "reason": "Resultado não aberto."},
        {"requirement": "Preço", "status": "unknown", "evidence": [], "reason": "Sem preço visível."}]}
    config["verify_sequence"] = [missing, {"satisfied": second_satisfied, "checks": [
        {"requirement": "Abrir resultado", "status": "confirmed" if second_satisfied else "not_met",
         "evidence": ["Resultado disponível"], "reason": "Conferência final."}]}, missing]
    sequence = iter(["e1", "DONE", "e4", "DONE", "DONE"])
    def choose(page, goal, history, **kwargs):
        selected = next(sequence)
        decisions.append({"goal": goal, "history": copy.deepcopy(history), **kwargs})
        return {"choice": selected, "operation": "DONE" if selected == "DONE" else "CLICK",
                "target": selected.removeprefix("e"), "probabilities": {selected: 1},
                "confidence": 1, "latency_ms": 1, "usage": {}}
    monkeypatch.setattr(agent, "choose", choose)
    session = chat.ChatSession("https://example.org")
    session.agent.browser.page["actions"].append({"id": "e4", "node": 4, "kind": "click",
                                                    "role": "link", "label": "Abrir resultado"})
    view = send(session)
    assert len(view["history"]) == 2
    assert len(decisions) == (4 if second_satisfied else 5)
    assert decisions[2]["verification_feedback"] == [
        {"requirement": "Abrir resultado", "reason": "Resultado não aberto."}]
    assert view["messages"][-1]["verification"]["satisfied"] is second_satisfied
    assert len([kind for kind, _ in calls if kind == "verify"]) == (2 if second_satisfied else 3)
    assert session.recovery_attempts == (1 if second_satisfied else 2)


def test_approved_commitment_never_gets_corrective_mutation(offline):
    _, decisions, config = offline
    config["verify_sequence"] = [{"satisfied": False, "checks": [
        {"requirement": "Publicar", "status": "not_met", "evidence": ["Resultado disponível"],
         "reason": "Sem confirmação da publicação."}]}]
    session = chat.ChatSession("https://example.org")
    view = send(session, "Publique o conteúdo")
    session.approve({"approval_id": view["approval"]["id"]})
    session.worker.join(5)
    final = session.snapshot()
    assert len(final["history"]) == 1
    assert len(decisions) == 2
    assert not final["messages"][-1]["verification"]["satisfied"]


def test_enter_requires_confirmation_and_stale_target_is_not_retried(offline, monkeypatch):
    _, decisions, _ = offline
    def choose(page, goal, history, **kwargs):
        selected = "DONE" if history else "e4"
        decisions.append({"goal": goal, "history": copy.deepcopy(history), **kwargs})
        return {"choice": selected, "operation": "DONE" if history else "PRESS_ENTER",
                "target": "1", "probabilities": {selected: 1}, "confidence": 1,
                "latency_ms": 1, "usage": {}}
    monkeypatch.setattr(agent, "choose", choose)
    session = chat.ChatSession("https://example.org")
    session.agent.browser.page["actions"].append({"id": "e4", "node": 3, "kind": "press_enter",
                                                    "role": "textbox", "label": "Press Enter in Título"})
    view = send(session, "Publique o conteúdo com Enter")
    assert view["chat_status"] == "awaiting_confirmation"
    assert not session.agent.browser.mutations
    session.agent.browser.stale_before_action = True
    session.approve({"approval_id": view["approval"]["id"]})
    session.worker.join(5)
    final = session.snapshot()
    assert final["chat_status"] == "paused"
    assert final["history"][-1]["execution"] == "not_executed"
    assert not session.agent.browser.mutations


def test_uncertain_enter_is_logged_once_without_retry(offline, monkeypatch):
    monkeypatch.setattr(agent, "choose", lambda *_a, **_k: {
        "choice": "e4", "operation": "PRESS_ENTER", "target": "1",
        "probabilities": {"e4": 1}, "confidence": 1, "latency_ms": 1, "usage": {},
    })
    session = chat.ChatSession("https://example.org")
    session.agent.browser.page["actions"].append({"id": "e4", "node": 3, "kind": "press_enter",
                                                    "role": "textbox", "label": "Press Enter in Título"})
    session.agent.browser.interrupt_action = True
    view = send(session)
    assert view["chat_status"] == "error"
    assert session.agent.browser.mutations == [("e4", None)]
    assert [item["execution"] for item in view["history"]] == ["uncertain"]


@pytest.mark.parametrize("status,evidence", [
    ("unknown", []), ("not_met", ["Pronta"]), ("confirmed", ["evidence invented"]),
    ("confirmed", ["CLICK"]),
])
def test_positive_verdict_requires_every_check_and_visible_evidence(monkeypatch, status, evidence):
    proposed = {"satisfied": True, "reply": "Encontrei o resultado.", "checks": [
        {"requirement": "Abrir página", "status": "confirmed", "evidence": ["Pronta"], "reason": "Página visível."},
        {"requirement": "Aplicar filtro", "status": status, "evidence": evidence, "reason": "Conferência do filtro."},
    ]}
    def ask(kind, context, calls):
        calls.append({"kind": kind})
        return proposed
    monkeypatch.setattr(assistant, "ask", ask)
    page = {"url": "https://example.org", "title": "Teste", "text": "Pronta",
            "elements": [{"label": "Filtro", "value": "", "operations": ["CLICK"]}]}
    verdict = assistant.verify({"page": page}, [])
    assert verdict["satisfied"] is False
    assert verdict["checks"][0]["status"] == "confirmed"
    if status == "confirmed":
        assert verdict["checks"][1]["status"] == "unknown"
        assert not verdict["checks"][1]["evidence"]


def test_evidence_spanning_adjacent_text_nodes_counts_as_visible(monkeypatch):
    proposed = {"satisfied": True, "reply": "Ok.", "checks": [
        {"requirement": "Resultados", "status": "confirmed",
         "evidence": ['1-48 de mais de 20.000 resultados para "caneta azul"'], "reason": "Visível."}]}
    monkeypatch.setattr(assistant, "ask", lambda kind, context, calls: calls.append({"kind": kind}) or proposed)
    page = {"url": "https://example.org", "title": "Teste", "elements": [],
            "text": 'Amazon\n1-48 de mais de 20.000 resultados para\n"caneta azul"\nTudo'}
    assert assistant.verify({"page": page}, [])["satisfied"] is True
    proposed["checks"][0]["evidence"] = ["É o 39.º presidente desde 2023, tendo"]
    page["text"] = "É o\n39.º\npresidente desde\n2023\n, tendo"
    assert assistant.verify({"page": page}, [])["satisfied"] is True
    proposed["checks"][0]["evidence"] = ["É o 39.º presidente, tendo"]  # skips real page text
    assert assistant.verify({"page": page}, [])["satisfied"] is False
    proposed["checks"][0]["evidence"] = ["https://example.org..."]
    assert assistant.verify({"page": page}, [])["satisfied"] is True
    proposed["checks"][0]["evidence"] = ["https://example.org/other..."]
    assert assistant.verify({"page": page}, [])["satisfied"] is False
    proposed["checks"][0]["evidence"] = ['1-48 de mais de 20.000 resultados para "caneta azul"']
    page["text"] = "Amazon\n1-48 de mais de 20.000 resultados"
    assert assistant.verify({"page": page}, [])["satisfied"] is False


@pytest.mark.parametrize("checks", [[], [{"requirement": "x", "status": "invented", "evidence": [], "reason": "x"}],
                                     [{"requirement": "x", "status": "confirmed", "evidence": "x", "reason": "x"}]])
def test_invalid_verification_checklist_is_rejected(checks):
    result = {"choices": [{"message": {"content": json.dumps({"satisfied": True, "checks": checks})}}]}
    with pytest.raises(ValueError, match="resposta inválida"):
        assistant._validated_output(result, assistant.PROPERTIES["verify"])


def test_page_change_during_verification_cannot_claim_success(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    moved = iter(range(1, 10))
    original = assistant.verify

    def verify(context, calls):
        result = original(context, calls)
        session.agent.browser.page["url"] = f"https://example.org/moved-{next(moved)}"
        return result
    monkeypatch.setattr(assistant, "verify", verify)
    view = send(session)
    assert view["messages"][-1]["verification"]["satisfied"] is False
    assert "mudou durante" in view["messages"][-1]["content"]
    assert "Encontrei o resultado" not in view["messages"][-1]["content"]
    assert len(session.agent.browser.mutations) == 1
    assert len([call for call in view["chat_calls"] if call["kind"] == "verify"]) == 2


def test_unrelated_page_churn_keeps_a_verdict_whose_evidence_is_still_visible(offline, monkeypatch):
    session = chat.ChatSession("https://example.org")
    original = assistant.verify

    def verify(context, calls):
        result = original(context, calls)
        browser = session.agent.browser
        browser.changed = True  # the full marker differs, like a rotating ad
        browser.page["text"] = "Resultado disponível\nOferta relâmpago 00:59"
        return result

    monkeypatch.setattr(assistant, "verify", verify)
    view = send(session)
    assert view["messages"][-1]["verification"]["satisfied"] is True
    assert len([call for call in view["chat_calls"] if call["kind"] == "verify"]) == 1


def test_self_contained_task_drives_agent_and_checks_but_chat_keeps_user_words(offline):
    calls, decisions, config = offline
    config["task"] = "Busque 'Lula presidente do Brasil' na Wikipédia"
    view = send(chat.ChatSession("https://example.org"), "busque na wikipidia")
    assert decisions[0]["goal"] == config["task"]
    assert [ctx["goal"] for kind, ctx in calls if kind in {"safety", "verify"}] == [config["task"]] * 2
    assert next(kind == "request" and ctx["goal"] for kind, ctx in calls) == "busque na wikipidia"
    assert view["messages"][0]["content"] == "busque na wikipidia"
    assert view["task"] == config["task"]


def test_empty_acknowledgment_does_not_stop_the_task(offline):
    offline[2]["route"] = {"url": "-", "reply": ""}
    view = send(chat.ChatSession("https://example.org"))
    assert view["chat_status"] == "completed"
    assert view["messages"][-1]["verification"]["satisfied"] is True


def test_missing_restatement_falls_back_to_the_user_message(offline):
    _, decisions, config = offline
    config["route"] = {"url": "-", "reply": "Ok.", "task": ""}
    view = send(chat.ChatSession("https://example.org"), "Busque um resultado agora")
    assert view["chat_status"] == "completed"
    assert decisions[0]["goal"] == "Busque um resultado agora" == view["task"]


def test_rechecking_reads_again_without_actions_or_decisions(offline):
    offline[2]["false_verdict"] = True
    session = chat.ChatSession("https://example.org")
    view = send(session)
    assert view["can_recheck"]
    decisions, mutations = len(offline[1]), list(session.agent.browser.mutations)
    messages = len(view["messages"])
    offline[2]["false_verdict"] = False
    session.recheck()
    session.worker.join(5)
    view = session.snapshot()
    assert view["messages"][-1]["verification"]["satisfied"] is True
    assert view["messages"][-1]["verification"]["checks"][0]["status"] == "confirmed"
    assert view["messages"][-1]["verification"]["checked_at"] > 0
    assert len(offline[1]) == decisions and session.agent.browser.mutations == mutations
    assert len(view["messages"]) == messages
    assert not view["can_recheck"]
    with pytest.raises(ValueError):
        session.recheck()


def test_recheck_with_visible_missing_requirement_stays_read_only(offline):
    _, decisions, config = offline
    config["false_verdict"] = True
    session = chat.ChatSession("https://example.org")
    send(session)
    mutations = list(session.agent.browser.mutations)
    count = len(decisions)
    config["verify_sequence"] = [{"satisfied": False, "checks": [
        {"requirement": "Abrir resultado", "status": "not_met",
         "evidence": ["Resultado disponível"], "reason": "Resultado não aberto."}]}]
    session.recheck()
    session.worker.join(5)
    assert session.snapshot()["chat_status"] == "completed"
    assert len(decisions) == count
    assert session.agent.browser.mutations == mutations


def test_failed_verifier_explains_failure_and_allows_read_only_recheck(offline, monkeypatch):
    monkeypatch.setattr(assistant, "verify", lambda *args: (_ for _ in ()).throw(RuntimeError("offline failure")))
    view = send(chat.ChatSession("https://example.org"))
    assert not view["messages"][-1]["verification"]["satisfied"]
    assert "Não foi possível executar" in view["messages"][-1]["content"]
    assert view["can_recheck"]


def test_invalid_safety_result_fails_before_mutation(offline):
    offline[2]["bad_safety"] = True
    session = chat.ChatSession("https://example.org")
    view = send(session)
    assert view["chat_status"] == "error"
    assert not session.agent.browser.mutations
    assert view["chat_calls"][-1]["status"] == "error"


def test_invalid_read_only_safety_call_retries_without_repeating_browser_action(offline):
    offline[2]["bad_safety_once"] = True
    session = chat.ChatSession("https://example.org")
    view = send(session)
    safety = next(call for call in view["chat_calls"] if call["kind"] == "safety")
    assert safety["attempts"] == 2 and safety["invalid_responses"] == 1
    assert session.agent.browser.mutations == [("e1", None)]


def test_missing_credential_never_calls_provider_or_mutates(offline, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    session = chat.ChatSession("https://example.org")
    view = send(session)
    assert view["chat_status"] == "error"
    assert not offline[0] and not session.agent.browser.mutations
    assert view["chat_calls"][-1]["attempts"] == 0


def test_duplicate_message_is_idempotent_and_view_is_independent(offline):
    session = chat.ChatSession("https://example.org")
    view = send(session)
    view["messages"].clear()
    assert len(session.snapshot()["messages"]) == 2
    before = len(offline[0])
    session.message({"message": "Busque um resultado", "message_id": "first"})
    assert len(offline[0]) == before and len(session.agent.browser.mutations) == 1


@pytest.mark.parametrize(
    "url",
    [
        "",
        "javascript:alert(1)",
        "file:///C:/secret",
        "https://",
        "https://a:b@host/",
        "https://host:bad",
        "https://host/\npath",
        "http://exa mple.org",
    ],
)
def test_invalid_urls_rejected_before_browser_creation(url, offline):
    with pytest.raises(ValueError):
        chat.ChatSession(url)
    assert not FakeBrowser.instances


@pytest.mark.parametrize("message", [None, "", " ", 123, "a" * 2001])
def test_invalid_messages_rejected_before_model_calls(message, offline):
    session = chat.ChatSession("https://example.org")
    with pytest.raises(ValueError):
        session.message({"message": message, "message_id": "first"})
    assert not offline[0]


@pytest.fixture
def local_server(monkeypatch, offline, tmp_path):
    from jev_ultrafast.conversations import ConversationStore

    monkeypatch.setattr(demo, "STORE", ConversationStore(tmp_path / "history.sqlite3"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), demo.Handler)
    port = server.server_address[1]
    monkeypatch.setattr(demo, "PORT", port)
    monkeypatch.setattr(demo, "ORIGIN", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(demo, "SESSION", None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = httpx.Client(base_url=demo.ORIGIN, headers={"X-Demo-Token": demo.TOKEN}, timeout=3, trust_env=False)
    yield client
    if demo.SESSION and demo.SESSION.worker:
        demo.SESSION.worker.join(5)
    client.close()
    server.shutdown()
    server.server_close()
    demo.close_browser()


def test_http_session_chat_restore_and_reject_raw_actions(local_server):
    client = local_server
    view = client.post("/api/session", json={"url": "https://example.org"}).json()
    session_id = view["session_id"]
    response = client.post("/api/message", json={"session_id": session_id, "message": "? Página?", "message_id": "one"})
    assert response.status_code == 200
    demo.SESSION.worker.join(5)
    restored = client.get("/api/state").json()
    assert len(restored["messages"]) == 2 and restored["session_id"] == session_id
    assert client.post("/api/act", json={"session_id": session_id}).status_code == 400
    assert (
        client.post(
            "/api/message",
            json={
                "session_id": "expired",
                "message": "x",
                "message_id": "two",
            },
        ).status_code
        == 400
    )
    assert client.post("/api/session", json=[]).status_code == 400
    assert "JEV OpenFast Browser · Assistente" in client.get("/").text
    fresh = client.post("/api/session", json={"url": "http://127.0.0.1:8080"}).json()
    assert fresh["session_id"] != session_id and fresh["messages"] == []
    assert FakeBrowser.instances[0].closed


def test_http_viewport_initial_capture_validation_and_session_binding(local_server):
    client = local_server
    response = client.post("/api/session", json={"url": "https://example.org", "width": 375, "height": 700})
    assert response.status_code == 200
    view = response.json()
    assert (view["page"]["w"], view["page"]["h"]) == (375, 700)
    body = {"session_id": view["session_id"], "width": 800, "height": 500}
    assert client.post("/api/viewport", json={**body, "session_id": "old"}).status_code == 400
    assert client.post("/api/viewport", json={**body, "height": 0}).status_code == 400
    assert client.post("/api/viewport", json=body, headers={"X-Demo-Token": "wrong"}).status_code == 403
    assert client.post("/api/viewport", json=body, headers={"Origin": "https://example.org"}).status_code == 403
    response = client.post("/api/viewport", json=body)
    assert response.status_code == 200
    assert response.json()["page"]["w"] == 800
    assert demo.SESSION.agent.browser.resizes == [(800, 500)]


def test_http_recheck_is_bound_to_session_and_does_not_repeat_actions(local_server, offline):
    client = local_server
    offline[2]["false_verdict"] = True
    first = client.post("/api/message", json={"session_id": None, "message": "Busque um resultado",
                                               "message_id": "first"}).json()
    demo.SESSION.worker.join(5)
    body = {"session_id": first["session_id"]}
    assert client.post("/api/verify", json={"session_id": "expired"}).status_code == 400
    assert client.post("/api/verify", json=body, headers={"X-Demo-Token": "wrong"}).status_code == 403
    decisions = len(offline[1])
    offline[2]["false_verdict"] = False
    assert client.post("/api/verify", json=body).status_code == 200
    demo.SESSION.worker.join(5)
    assert client.get("/api/state").json()["messages"][-1]["verification"]["satisfied"]
    assert len(offline[1]) == decisions and len(demo.SESSION.agent.browser.mutations) == 1
    assert client.post("/api/verify", json=body).status_code == 400


def test_first_message_passes_size_before_first_capture(local_server):
    client = local_server
    response = client.post("/api/message", json={"session_id": None, "message_id": "sized-first",
                          "message": "? Abra https://example.org", "width": 360, "height": 620})
    assert response.status_code == 200
    demo.SESSION.worker.join(5)
    view = client.get("/api/state").json()
    assert (view["page"]["w"], view["page"]["h"]) == (360, 620)
    assert demo.SESSION.agent.browser.observations == 1  # the new tab's first observation starts the task
    assert not demo.SESSION.agent.browser.resizes


def test_http_message_starts_conversation_without_url_field_and_reset(local_server):
    client = local_server
    response = client.post("/api/message", json={"session_id": None, "message": "? Veja http://127.0.0.1:8080",
                                                  "message_id": "first"})
    assert response.status_code == 200
    demo.SESSION.worker.join(5)
    view = client.get("/api/state").json()
    assert view["page"]["url"] == "http://127.0.0.1:8080"
    assert view["messages"][-1]["kind"] == "answer"
    assert not demo.SESSION.agent.browser.mutations
    assert client.post("/api/reset", json={"session_id": view["session_id"]}).status_code == 200
    assert client.get("/api/state").json()["session_id"] is None
    assert FakeBrowser.instances[0].closed


def test_http_get_is_available_during_work_and_pause_is_resumable(local_server, offline):
    client = local_server
    entered, released = threading.Event(), threading.Event()
    offline[2]["block"] = (entered, released)
    session_id = client.post("/api/session", json={"url": "https://example.org"}).json()["session_id"]
    client.post("/api/message", json={"session_id": session_id, "message": "Busque", "message_id": "one"})
    assert entered.wait(2)
    try:
        assert client.get("/api/state").json()["chat_status"] == "thinking"
        pause_response = client.post("/api/pause", json={"session_id": session_id})
        assert pause_response.status_code == 200
        assert pause_response.json()["pause_requested"] is True
        assert pause_response.json()["chat_status"] == "thinking"
        assert "Pausa solicitada" in client.get("/api/state").json()["progress"]
        assert client.post("/api/session", json={"url": "https://example.net"}).status_code == 400
    finally:
        released.set()
    demo.SESSION.worker.join(5)
    assert client.get("/api/state").json()["chat_status"] == "paused"
    assert not demo.SESSION.agent.browser.mutations
    client.post("/api/resume", json={"session_id": session_id})
    demo.SESSION.worker.join(5)
    assert len(demo.SESSION.agent.browser.mutations) == 1


def test_http_auth_and_origin_checks(local_server):
    for headers in ({"X-Demo-Token": "wrong"}, {"Origin": "https://evil.example"}, {"Host": "evil.example"}):
        response = local_server.post("/api/session", json={"url": "https://example.org"}, headers=headers)
        assert response.status_code == 403
    assert not FakeBrowser.instances


def test_history_survives_restart_and_continues_in_new_tab(local_server):
    client = local_server
    first = client.post("/api/message", json={"session_id": None, "message": "Busque um resultado",
                                               "message_id": "first"}).json()
    demo.SESSION.worker.join(5)
    conversation_id = first["session_id"]
    assert len(FakeBrowser.instances) == 1
    assert len(FakeBrowser.instances[0].mutations) == 1
    assert client.get("/api/conversations").json()["conversations"][0]["id"] == conversation_id

    demo.close_browser()
    demo.restore_selected()
    restored = client.get("/api/state").json()
    assert restored["session_id"] == conversation_id
    assert len(restored["messages"]) == 2
    assert restored["page"] is None and restored["approval"] is None
    assert len(FakeBrowser.instances) == 1  # Opening history never replays browser work.

    client.post("/api/message", json={"session_id": conversation_id, "message": "Busque outro resultado",
                                      "message_id": "second"})
    demo.SESSION.worker.join(5)
    assert len(FakeBrowser.instances) == 2
    assert len(FakeBrowser.instances[0].mutations) == 1
    assert len(FakeBrowser.instances[1].mutations) == 1
    assert len(client.get("/api/state").json()["messages"]) == 4


def test_history_invalidates_pending_approval_and_can_delete(local_server):
    client = local_server
    first = client.post("/api/message", json={"session_id": None, "message": "Publique o conteúdo",
                                               "message_id": "first"}).json()
    demo.SESSION.worker.join(5)
    pending = client.get("/api/state").json()["approval"]
    assert pending and not FakeBrowser.instances[0].mutations

    demo.close_browser()
    demo.restore_selected()
    restored = client.get("/api/state").json()
    assert restored["session_id"] == first["session_id"]
    assert restored["approval"] is None
    assert restored["messages"][-1]["kind"] == "interrupted"
    assert client.post("/api/approve", json={"session_id": first["session_id"],
                                             "approval_id": pending["id"]}).status_code == 400
    assert not FakeBrowser.instances[0].mutations

    deleted = client.post("/api/delete", json={"conversation_id": first["session_id"]})
    assert deleted.status_code == 200
    assert client.get("/api/state").json()["session_id"] is None
    assert client.get("/api/conversations").json()["conversations"] == []


def test_history_rename_archive_restore_and_validation(local_server):
    client = local_server
    first = client.post("/api/message", json={"session_id": None, "message": "Busque um resultado",
                                               "message_id": "first"}).json()
    demo.SESSION.worker.join(5)
    conversation_id = first["session_id"]
    body = {"conversation_id": conversation_id}
    assert client.post("/api/rename", json={**body, "title": " "}).status_code == 400
    assert client.post("/api/rename", json={**body, "title": "x" * 81}).status_code == 400
    before = demo.STORE.list()[0]["updated_at"]
    assert client.post("/api/rename", json={**body, "title": "  Minha \n busca  "}).status_code == 200
    assert demo.STORE.list()[0]["updated_at"] == before  # renaming must not reorder the history
    demo.SESSION._publish()
    assert client.get("/api/conversations").json()["conversations"][0]["title"] == "Minha busca"
    assert client.post("/api/archive", json=body).status_code == 200
    assert FakeBrowser.instances[0].closed
    assert client.get("/api/state").json()["session_id"] is None
    assert client.get("/api/conversations").json()["conversations"][0]["archived"] is True
    assert client.post("/api/unarchive", json=body).status_code == 200
    restored = client.post("/api/select", json=body).json()
    assert restored["session_id"] == conversation_id
    assert len(restored["messages"]) == 2 and restored["page"] is None
    assert len(FakeBrowser.instances) == 1
