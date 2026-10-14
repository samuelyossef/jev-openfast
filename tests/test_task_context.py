"""Offline requirement, evidence and model-budget regressions."""

import json
import sqlite3
import threading
from unittest.mock import Mock

import pytest

from jev_ultrafast import assistant, model
from jev_ultrafast.assistant import evidence_quotes
from jev_ultrafast.conversations import ConversationStore
from jev_ultrafast.model_runtime import ModelBudgetExceeded, ModelPaused, ModelRuntime, model_options, request_scope
from jev_ultrafast.task_context import model_context, new_task, numeric_evidence, remember, set_requirements


def task(goal="Busque dois livros de R$15 a R$25"):
    result = new_task(goal, "turn")
    set_requirements(result, [{"description": goal, "source_quote": goal, "source_turn_id": ""}])
    return result


def observation():
    return {"url": "https://example.org", "title": "Livros", "elements": [],
            "text": "Livro A R$ 20 Livro B R$ 22", "blocks": [
                {"id": "1", "text": "Livro A R$ 20"}, {"id": "2", "text": "Livro B R$ 22"}]}


def proposed(requirements):
    return {"satisfied": True, "reply": "Encontrei os livros.", "checks": [
        {"requirement_id": item["id"], "requirement": item["description"], "status": "confirmed",
         "evidence": ["Livro A R$ 20", "Livro B R$ 22"], "reason": "Visível."} for item in requirements]}


def install_verifier(monkeypatch, output):
    def ask(kind, context, calls):
        calls.append({"kind": kind})
        return json.loads(json.dumps(output))
    monkeypatch.setattr(assistant, "ask", ask)


def test_initial_inventory_and_reservations_are_recorded_before_execution():
    task = new_task("Busque dois livros", "turn")
    assert task["requirements"][0]["source_quote"] == task["original_goal"]
    assert not task["coverage_valid"]
    recorded = []
    runtime = ModelRuntime(on_change=lambda: recorded.append(runtime.snapshot()))
    record = runtime.reserve({"model": "typesafe/jev-1.13"}, "decisions")
    assert recorded[0]["calls"][0]["status"] == "pending"
    runtime.finish(record, {"usage": {"cost": 0.001}})
    assert recorded[-1]["consumed_usd"] == 0.001


def test_verifier_quotes_are_limited_to_exact_observed_strings():
    page = observation()
    quotes = evidence_quotes(page)
    assert "Livro A R$ 20" in quotes
    assert "text: Livro A R$ 20" not in quotes
    assert "" in quotes


def test_request_allows_whole_lines_with_conjunctions_and_binds_explicit_url(monkeypatch):
    line = "Encontre e abra o artigo mais recente do Canal Aurora."
    url = "http://127.0.0.1:8765/article"
    goal = f"Abra {url}\n{line}"
    output = {"url": url, "intent": "task", "reply": "", "task": goal, "needs_planning": True,
              "requirements": [{"description": line, "source_quote": line, "source_turn_id": ""}]}

    def post(_url, _key, body, **_kwargs):
        properties = body["response_format"]["json_schema"]["schema"]["properties"]
        assert properties["url"]["enum"] == ["-", url]
        assert line in properties["requirements"]["items"]["properties"]["source_quote"]["enum"]
        return {"choices": [{"message": {"content": json.dumps(output)}}]}

    monkeypatch.setattr(assistant, "openrouter_key", lambda: "offline")
    monkeypatch.setattr(assistant, "post_json", post)
    assert assistant.ask("request", {"goal": goal}, []) == output


def test_missing_or_duplicate_requirement_cannot_complete(monkeypatch):
    context = task()
    output = proposed(context["requirements"])
    output["checks"] = output["checks"][:1]
    install_verifier(monkeypatch, output)
    result = assistant.verify({"task_context": model_context(context), "page": observation()}, [])
    assert not result["satisfied"]
    assert len(result["checks"]) == len(context["requirements"])
    assert any(check["status"] == "unknown" for check in result["checks"])
    output["checks"].append(output["checks"][0])
    assert not assistant.verify({"task_context": model_context(context), "page": observation()}, [])["satisfied"]


def test_associated_prices_and_quantities_are_checked_in_code(monkeypatch):
    context = task()
    output = proposed(context["requirements"])
    install_verifier(monkeypatch, output)
    assert assistant.verify({"task_context": model_context(context), "page": observation()}, [])["satisfied"]
    page = observation()
    page["blocks"] = [{"id": "1", "text": "Livro A R$ 30"}, {"id": "2", "text": "Livro B R$ 40"}]
    page["text"] = "Livro A R$ 30 Livro B R$ 40"
    output["checks"] = [{**check, "evidence": ["Livro A R$ 30", "Livro B R$ 40"]} for check in output["checks"]]
    assert not assistant.verify({"task_context": model_context(context), "page": page}, [])["satisfied"]
    page["blocks"][0]["text"] = "Livro A R$ 20"
    page["text"] = "Livro A R$ 20 Livro B R$ 40"
    output["checks"] = [{**check, "evidence": ["Livro A R$ 20", "Livro B R$ 40"]} for check in output["checks"]]
    assert not assistant.verify({"task_context": model_context(context), "page": page}, [])["satisfied"]


def test_filter_values_need_current_controls_not_offered_ranges():
    context = task("Aplique filtro de R$15 a R$25")
    requirement = next(item for item in context["requirements"] if item.get("constraint"))
    page = observation()
    page["elements"] = [{"label": "R$15 a R$25", "checked": "false", "value": "15 25"}]
    assert not numeric_evidence(requirement, ["Livro A R$ 20"], page)
    page["elements"][0]["checked"] = "true"
    assert numeric_evidence(requirement, ["Livro A R$ 20"], page)


def test_literal_dates_are_compared_without_model_arithmetic():
    context = task("Busque artigos após 2026-01-01")
    requirement = next(item for item in context["requirements"] if item.get("constraint"))
    assert numeric_evidence(requirement, ["Publicado 02/01/2026"], observation())
    assert not numeric_evidence(requirement, ["Publicado 31/12/2025"], observation())
    interval = task("Busque artigos entre 2026-01-01 e 2026-03-01")
    requirements = [item for item in interval["requirements"] if item.get("constraint")]
    assert len(requirements) == 1
    assert numeric_evidence(requirements[0], ["Publicado 01/02/2026"], observation())
    assert not numeric_evidence(requirements[0], ["Publicado 01/04/2026"], observation())


def test_thousands_and_instalment_prices_cannot_be_mistaken_for_cheap_items():
    context = task("Busque livros de R$1 a R$2")
    requirement = next(item for item in context["requirements"] if item.get("constraint"))
    page = observation()
    for text in ("Livro R$ 1.299,99", "Livro R$ 1.299", "Livro R$ 1,99 ou 2 parcelas de R$ 1,00"):
        page["blocks"] = [{"id": "1", "text": text}]
        assert not numeric_evidence(requirement, [text], page)
    context = task("Busque livros de R$15 a R$25.")
    requirement = next(item for item in context["requirements"] if item.get("constraint"))
    for text in ("Livro R$\n25\n,\n99", "Livro R$\n25\n.\n99"):
        page["blocks"] = [{"id": "1", "text": text}]
        assert not numeric_evidence(requirement, [text], page)
    page["blocks"] = [{"id": "1", "text": "Livro R$\n19\n,\n90"}]
    assert numeric_evidence(requirement, ["Livro R$\n19\n,\n90"], page)


def test_invalid_source_prevents_success_even_when_all_checks_confirmed(monkeypatch):
    context = new_task("Busque livros", "turn")
    set_requirements(context, [{"description": "Compre dez livros", "source_quote": "dez", "source_turn_id": ""}])
    install_verifier(monkeypatch, proposed(context["requirements"]))
    assert not assistant.verify({"task_context": model_context(context), "page": observation()}, [])["satisfied"]
    set_requirements(context, [{"description": "Livros", "source_quote": "livros", "source_turn_id": ""}])
    assert context["coverage_valid"]
    context = new_task("Busque artigos do Canal Aurora mais recentes", "turn")
    set_requirements(context, [{"description": "Artigos", "source_quote": "artigos", "source_turn_id": ""}])
    assert not context["coverage_valid"]


def test_long_checklist_is_batched_and_missing_final_batch_is_unknown(monkeypatch):
    goal = "".join(f"item{index}; " for index in range(18))
    context = new_task(goal, "turn")
    set_requirements(context, [{"description": f"item{i}", "source_quote": f"item{i}", "source_turn_id": ""}
                              for i in range(18)])
    sizes = []
    def ask(kind, ctx, calls):
        requirements = ctx["task_context"]["requirements"]
        sizes.append(len(requirements))
        calls.append({"kind": kind})
        return proposed(requirements)
    monkeypatch.setattr(assistant, "ask", ask)
    result = assistant.verify({"task_context": model_context(context), "page": observation()}, [])
    assert result["satisfied"] and sizes == [8, 8, 3]


def test_exact_quotes_and_task_reference_isolation():
    page = observation()
    assert not assistant.shown("Livro A...", assistant.evidence_sources(page))
    current = task()
    remember(current, page)
    helper = model_context(current, observations=True)
    current["requirements"][0]["status"] = "confirmed"
    assert helper["requirements"][0]["status"] == "unknown"
    assert new_task("Outra tarefa", "next")["observations"] == []
    assert current["observations"][0]["url"] == page["url"]
    remember(current, {**page, "protected": True, "text": "private"})
    assert len(current["observations"]) == 1


def test_existing_sqlite_history_migrates_without_erasing_messages(tmp_path):
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, updated_at INTEGER, "
                   "messages TEXT, phase TEXT, last_url TEXT)")
        db.execute("INSERT INTO conversations VALUES ('old', 'Old', 1, '[]', 'completed', 'https://example.org')")
    store = ConversationStore(path)
    assert store.load("old")["messages"] == []
    assert store.load("old")["task_context"] == {}
    context = task()
    store.save({"session_id": "old", "messages": [], "chat_status": "paused", "task_context": context})
    assert store.load("old")["task_context"] == context


def test_qwen_omits_unsupported_reasoning_and_keeps_schema_routing(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_REASONING", "high")
    options = model_options("qwen/qwen3-30b-a3b-instruct-2507")
    assert "reasoning" not in options
    assert options["provider"]["require_parameters"]
    assert options["provider"]["sort"] == "latency"


def test_budget_blocks_before_http_and_pause_retains_unknown_cost(monkeypatch):
    post = Mock()
    monkeypatch.setattr(model.CLIENT, "post", post)
    body = {"model": "inception/mercury-2.5", "max_tokens": 1600}
    with request_scope(ModelRuntime(limit=0.00000001)), pytest.raises(ModelBudgetExceeded):
        model.post_json(model.CHAT_URL, "offline", body)
    post.assert_not_called()
    pause = threading.Event()
    runtime = ModelRuntime(pause)
    response = Mock(is_error=True, status_code=429, headers={"retry-after": "1"})
    post.side_effect = lambda *args, **kwargs: pause.set() or response
    with request_scope(runtime), pytest.raises(ModelPaused):
        model.post_json(model.CHAT_URL, "offline", body)
    usage = runtime.snapshot()
    assert usage["attempts"] == 1 and usage["estimated"] and usage["consumed_usd"] > 0


def test_retries_record_actual_model_cost_tokens_and_each_attempt(monkeypatch):
    failure = Mock(is_error=True, status_code=503, headers={"retry-after": "0.5"})
    success = Mock(is_error=False, status_code=200)
    success.json.return_value = {"model": "actual-model", "id": "request-id",
                                 "usage": {"cost": 0.001, "prompt_tokens": 10, "completion_tokens": 5}}
    post = Mock(side_effect=[failure, success])
    monkeypatch.setattr(model.CLIENT, "post", post)
    runtime = ModelRuntime()
    monkeypatch.setattr(runtime.pause, "wait", lambda seconds: False)
    with request_scope(runtime):
        model.post_json(model.CHAT_URL, "offline", {"model": "inception/mercury-2.5", "max_tokens": 100})
    usage = runtime.snapshot()
    assert usage["attempts"] == 2 and usage["consumed_usd"] >= 0.001
    assert usage["calls"][1]["model"] == "actual-model"
    assert usage["calls"][1]["usage"]["completion_tokens"] == 5
    assert not usage["calls"][1]["estimated"] and usage["calls"][0]["estimated"]


def test_mercury_error_inside_http_200_uses_open_model_and_keeps_both_attempts(monkeypatch):
    failure = Mock(is_error=False, status_code=200)
    failure.json.return_value = {"error": {"code": 502, "metadata": {"error_type": "provider_unavailable"}}}
    success = Mock(is_error=False, status_code=200)
    success.json.return_value = {"model": "qwen/qwen3-30b-a3b-instruct-2507",
                                 "choices": [{"message": {"content": '{"text":"Anthropic"}'}}],
                                 "usage": {"cost": 0.001}}
    post = Mock(side_effect=[failure, success])
    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(model, "openrouter_key", lambda: "offline")
    monkeypatch.setenv("TEXT_MODEL", "inception/mercury-2.5")
    runtime = ModelRuntime()
    with request_scope(runtime):
        text, metadata = model.field_text({"goal": "Find Anthropic"})
    assert text == "Anthropic" and metadata["model"] == success.json.return_value["model"]
    fallback = post.call_args_list[1].kwargs["json"]
    assert "reasoning" not in fallback and fallback["response_format"]["type"] == "json_schema"
    usage = runtime.snapshot()
    assert usage["attempts"] == 2 and usage["calls"][0]["status"] == "error"
    assert usage["calls"][0]["estimated"] and usage["consumed_usd"] > 0.001
    assert usage["calls"][1]["model"] == metadata["model"]


def test_other_provider_error_does_not_switch_models_or_repeat(monkeypatch):
    failure = Mock(is_error=False, status_code=200)
    failure.json.return_value = {"error": {"code": 400, "metadata": []}}
    post = Mock(return_value=failure)
    monkeypatch.setattr(model.CLIENT, "post", post)
    with pytest.raises(model.ModelProviderError):
        model.post_json(model.CHAT_URL, "offline", {"model": "inception/mercury-2.5"})
    assert post.call_count == 1
