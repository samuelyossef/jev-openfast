"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from datetime import date
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint
from jev_ultrafast.questions import NEXT_ACTION, TARGET


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_enter_uses_its_own_observed_target_and_verification_feedback(monkeypatch):
    p = page()
    p["actions"].insert(2, {"id": "e4", "kind": "press_enter", "label": "Press Enter in Search",
                            "role": "textbox", "value": "books", "node": 10})
    elements, targets, _ = model.action_space(p["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK", "PRESS_ENTER"]
    assert targets["PRESS_ENTER"]["1"]["id"] == "e4"

    def post(_url, _key, body):
        assert body["state"]["verification_feedback"] == [{"requirement": "Show results", "reason": "Missing"}]
        return {"model": "test", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "PRESS_ENTER"),
            "press_enter_target": choice(body["questions"]["press_enter_target"]["criteria"], "1"),
            "click_target": {"choice": "invalid"},
        }}

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose(p, "Show results", [],
                            verification_feedback=[{"requirement": "Show results", "reason": "Missing"}])
    assert decision["choice"] == "e4" and decision["target"] == "1"


def test_enter_dispatches_once_to_observed_field(monkeypatch):
    import jev_ultrafast.browser as browser

    calls = []
    def cdp(method, **params):
        calls.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": {"x": 30, "y": 40}}}
        return {}

    monkeypatch.setattr(browser, "cdp", cdp)
    result = browser_operation({"operation": "act", "session": "test",
                                "action": {"id": "e4", "kind": "press_enter", "node": 10}})
    assert result == {"executed": "e4"}
    keys = [params for method, params in calls if method == "Input.dispatchKeyEvent"]
    assert [key["type"] for key in keys] == ["keyDown", "keyUp"]
    assert all(key["key"] == "Enter" for key in keys)
    assert len([1 for method, _ in calls if method == "Input.dispatchMouseEvent"]) == 2


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        assert (_url, _key) == (model.DECISIONS_URL, "test")
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target", "blocked_reason"}
    assert d["blocked_reason"] is None
    assert calls[0]["model"] == "typesafe/jev-1.13"
    assert isinstance(calls[0]["questions"]["operation"]["instructions"], str)


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        selected = json.loads(target["criteria"]["1"])
        assert selected["checked"] == "true"
        assert selected["selected"] is False
        assert NEXT_ACTION in target["instructions"]
        assert TARGET in target["instructions"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_uses_text_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    def complete_text(url, key, body):
        assert (url, key, body["model"]) == (model.CHAT_URL, "test", "inception/mercury-2.5")
        assert 'Fly from \\"Zurich\\" to London' in body["messages"][1]["content"]
        return {"choices": [{"message": {"content": '{"text":"Zurich"}'}}]}

    post = Mock(side_effect=complete_text)
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    value, details = model.field_text(context)
    assert value == "Zurich" and details["model"] == "inception/mercury-2.5"
    assert post.call_count == 1


def test_text_model_handles_nonliteral_value(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.delenv("TEXT_MODEL", raising=False)
    calls = []

    def post(url, _key, body):
        calls.append(url)
        assert url == model.CHAT_URL
        assert body["model"] == "inception/mercury-2.5"
        return {"choices": [{"message": {"content": '{"text":"tomorrow"}'}}]}

    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context("Find a flight tomorrow", page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "tomorrow"
    assert calls == [model.CHAT_URL]


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


def test_missing_openrouter_credential_stops_decision_request(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    post = Mock()
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        model.choose(page(), "Find a book", [])
    post.assert_not_called()


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.timings = loop.Timings()
    a.screenshots = False
    a.pending_text = None
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_text_generation_checks_the_selected_target(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 1}))
    monkeypatch.setattr(loop, "field_text", helper)
    observed = runner.state["page"]
    action = next(a for a in observed["actions"] if a["kind"] == "fill")
    runner.state["browser"].fresh.side_effect = lambda page, target=None: target is action
    runner.prepare_text(action, observed)
    helper.assert_called_once()


@pytest.mark.parametrize("kind", ["click", "fill", "select"])
def test_target_guard_rejects_missing_observed_guard(kind):
    from jev_ultrafast.browser import Browser

    browser = Browser.__new__(Browser)
    browser.evaluate = Mock(return_value=["document", None])
    assert not browser.fresh({"page_key": "document", "guards": {}}, {"node": 1, "kind": kind})
    browser.evaluate.assert_not_called()


@pytest.mark.parametrize("readiness", [[False, True], [StalePage("redirect"), False, True]])
def test_navigation_waits_for_dom_readiness_without_repeating_request(readiness, monkeypatch):
    from jev_ultrafast import browser

    instance = browser.Browser.__new__(browser.Browser)
    instance.call = Mock(return_value={})
    instance.evaluate = Mock(side_effect=readiness)
    monkeypatch.setattr(browser.time, "sleep", Mock())
    instance.navigate("https://example.test/")
    instance.call.assert_called_once_with("Page.navigate", url="https://example.test/")
    assert instance.evaluate.call_count == len(readiness)


def test_navigation_failure_never_retries_request():
    from jev_ultrafast.browser import Browser

    instance = Browser.__new__(Browser)
    instance.call = Mock(return_value={"errorText": "net::ERR_NAME_NOT_RESOLVED"})
    instance.evaluate = Mock()
    with pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"):
        instance.navigate("https://example.test/")
    assert instance.call.call_count == 1
    instance.evaluate.assert_not_called()


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert [item["execution"] for item in runner.state["history"]] == ["not_executed", "executed"]
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_repeated_two_action_cycle_stops_early(runner):
    first = page()
    second = deepcopy(first)
    second["text"] = "Other calendar month"
    second["fingerprint"] = fingerprint(second)
    runner.state["page"] = first
    runner.state["browser"].observe.side_effect = [second, first] * 3
    for index, action in enumerate(["e2", "e3"] * 3):
        runner.state["decision"] = decision(action)
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
        if index < 5:
            assert runner.state["status"] == "ready"
    assert runner.state["status"] == "blocked"
    assert runner.state["stop_reason"] == "repeated two-action loop"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    assert runner.state["history"][-1]["execution"] == "executed"
    runner.state["browser"].act.assert_called_once()


def test_interrupted_mutation_is_logged_as_uncertain(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].act.side_effect = TimeoutError("Result unknown")
    with pytest.raises(TimeoutError):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 1
    assert runner.state["history"][0]["execution"] == "uncertain"
    assert runner.state["history"][0]["executed_ms"] is None
    assert runner.state["browser"].act.call_count == 1


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_screenshot_timeout_keeps_structured_observation(monkeypatch):
    import jev_ultrafast.browser as browser

    observed = page()

    def cdp(method, **_params):
        if method == "Page.captureScreenshot":
            raise TimeoutError("Screenshot timed out")
        return {"result": {"value": observed}}

    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": True})
    assert actual["fingerprint"] == observed["fingerprint"]
    assert "screenshot" not in actual


def test_optional_post_input_wait_timeout_still_observes(monkeypatch):
    import jev_ultrafast.browser as browser

    instance = browser.Browser.__new__(browser.Browser)
    instance.session = "test"
    instance.after_input = {"kind": "click", "node": 1}
    instance.call = Mock(side_effect=TimeoutError("wait timed out"))
    monkeypatch.setattr(browser, "browser_operation", Mock(return_value=page()))
    assert instance.observe(screenshot=False)["title"] == "Search"


def test_recording_skips_missing_optional_frame(runner, tmp_path):
    runner.record_dir = tmp_path
    runner.state["record"] = True
    runner.state["decision"] = decision("e3")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["execution"] == "executed"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("size", [(360, 640), (800, 400), (1, 1)])
def test_scroll_coordinates_stay_inside_current_viewport(monkeypatch, size):
    import jev_ultrafast.browser as browser

    cdp = Mock(return_value={"result": {"value": size}})
    monkeypatch.setattr(browser, "cdp", cdp)
    browser_operation({"operation": "act", "session": "test",
                       "action": {"id": "scroll_down", "kind": "scroll", "delta": 560}})
    event = cdp.call_args.kwargs
    assert cdp.call_args.args[0] == "Input.dispatchMouseEvent"
    assert 0 <= event["x"] < size[0] and 0 <= event["y"] < size[1]


def test_browser_setup_timeout_closes_its_tab(monkeypatch):
    import jev_ultrafast.browser as browser

    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        if method == "Target.createTarget":
            return {"targetId": "owned"}
        if method == "Target.attachToTarget":
            return {"sessionId": "session"}
        if method == "Emulation.setDeviceMetricsOverride":
            raise TimeoutError("slow browser")
        return {}

    monkeypatch.setattr(browser, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser, "cdp", cdp)
    monkeypatch.setenv("JEV_HEADLESS_BROWSER", "1")
    with pytest.raises(TimeoutError, match="slow browser"):
        browser.Browser("https://example.test/")
    assert [method for method, _ in calls] == [
        "Target.createTarget", "Target.attachToTarget",
        "Emulation.setDeviceMetricsOverride", "Target.closeTarget",
    ]
    assert all(params["_response_timeout"] == 15 for _, params in calls)
    assert calls[0][1]["background"] is False


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from examples.flights import verify

    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-09-20",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sun, Sep 20"),
                ("Nonstop flight on Sunday, September 20. Select flight", ""),
            ]
        ],
    }
    travel_date = date(2026, 9, 20)
    assert verify(actual, travel_date)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual, travel_date)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


@pytest.mark.parametrize("selected", ["e3", "DONE"])
def test_tick_builds_one_response_after_deciding_and_acting(runner, monkeypatch, selected):
    runner.state["status"] = "ready"
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision(selected)))
    runner.snapshot = Mock(wraps=runner.snapshot)
    result = runner.command("tick")
    assert result["status"] == ("done" if selected == "DONE" else "ready")
    runner.snapshot.assert_called_once_with()


@pytest.mark.parametrize("answer, expected", [(None, "OTHER"), ("CAPTCHA", "CAPTCHA"), ("invented", "OTHER")])
def test_blocked_reason_is_consumed_only_for_blocked(monkeypatch, answer, expected):
    def post(_url, _key, body):
        answers = {"operation": choice(body["questions"]["operation"]["criteria"], "BLOCKED")}
        if answer:
            answers["blocked_reason"] = choice(body["questions"]["blocked_reason"]["criteria"], answer)
        return {"model": "test", "answers": answers}

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Log in and find a book", [])
    assert d["choice"] == "BLOCKED" and d["blocked_reason"] == expected
