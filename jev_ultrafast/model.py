"""Jev chooses observed browser actions; the text model supplies field values."""

import json
import math
import os
import time
from collections import Counter

import httpx

from .model_runtime import CURRENT_DEADLINE, CURRENT_RUNTIME, ModelPaused, deadline_scope, model_options
from .questions import BLOCKED_REASON, BLOCKED_REASONS, NEXT_ACTION, TARGET, TEXT_VALUE
from .secrets_store import load_openrouter_key, openrouter_key_source

CLIENT = httpx.Client(http2=True, timeout=25)
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_URL = "https://openrouter.ai/api/v1/key"


class MissingValue(ValueError):
    """The text helper reported that the goal does not supply the field value."""


class InvalidOutput(ValueError):
    """A model answered, but not with a usable choice or value. Nothing was executed."""


class ModelProviderError(RuntimeError):
    def __init__(self, error):
        self.code = error.get("code")
        metadata = error.get("metadata")
        self.error_type = metadata.get("error_type") if isinstance(metadata, dict) else None
        super().__init__("O provedor não conseguiu responder; nenhuma ação foi executada.")


def reask_once(call):
    """Model requests never touch the browser, so one malformed answer is asked again before the task stops."""
    with deadline_scope(25):
        try:
            return call()
        except InvalidOutput:
            return call()


def openrouter_key():
    saved_key = load_openrouter_key()
    if saved_key:
        return saved_key
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ValueError("OPENROUTER_API_KEY is required before a model request.")
    return key


def validate_openrouter_key(key):
    """Ask OpenRouter whether the key is accepted; anything unconfirmed is rejected so no bad key is saved."""
    if not key.isascii():
        raise ValueError("A chave OpenRouter tem caracteres inválidos.")
    try:
        response = CLIENT.get(KEY_URL, headers={"Authorization": f"Bearer {key}"}, timeout=8)
    except httpx.HTTPError:
        raise RuntimeError("Não foi possível validar a chave: falha de conexão com a OpenRouter.") from None
    if response.status_code in {401, 403}:
        raise ValueError("A OpenRouter recusou esta chave. Confira se ela está correta e ativa.")
    if response.status_code != 200:
        raise RuntimeError(f"Não foi possível validar a chave (OpenRouter respondeu {response.status_code}).")


def openrouter_key_status():
    source = openrouter_key_source(os.environ.get("OPENROUTER_API_KEY", ""))
    return {"openrouter_key_configured": source != "missing", "openrouter_key_source": source}


RETRY_STATUS = {429, 502, 503, 529}
ATTEMPTS = 2  # Read-only model calls may retry; browser mutations never do.


def retry_delay(response, attempt):
    """Honor the provider's Retry-After (seconds), bounded so a stuck limit cannot hang a task."""
    try:
        return min(10.0, max(0.5, float(response.headers.get("retry-after", ""))))
    except ValueError:
        return 2.0**attempt


def post_json(url, key, body, *, on_attempt=None):
    runtime = CURRENT_RUNTIME.get()
    deep = body.get("model") == os.environ.get("DEEP_MODEL", "deepseek/deepseek-v4.1-flash")
    deadline = CURRENT_DEADLINE.get() or time.monotonic() + (45 if deep else 25)
    for attempt in range(ATTEMPTS):
        if runtime and runtime.pause.is_set():
            raise ModelPaused("Pausa solicitada antes da chamada ao modelo.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("O prazo da chamada ao modelo terminou; nenhuma ação foi executada.")
        record = runtime.reserve(body, url) if runtime else None
        if on_attempt:
            on_attempt()
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=remaining)
            result = response.json() if not response.is_error else None
        except (httpx.HTTPError, ValueError) as error:
            if runtime:
                runtime.finish(record, error=error)
            raise RuntimeError("Falha de conexão com o provedor do modelo; nenhuma ação foi executada") from None
        provider_error = result.get("error") if isinstance(result, dict) else None
        if runtime:
            runtime.finish(record, result, error=response.is_error or bool(provider_error))
        if runtime and runtime.pause.is_set():
            raise ModelPaused("Pausa solicitada durante a chamada ao modelo.")
        if isinstance(provider_error, dict):
            error = ModelProviderError(provider_error)
            if (url == CHAT_URL and body.get("model") == "inception/mercury-2.5"
                    and error.code == 502 and error.error_type == "provider_unavailable"
                    and attempt < ATTEMPTS - 1):
                # Mercury can refuse ordinary browser tasks about other models; retry only the read-only call.
                body = {key: value for key, value in body.items() if key not in {"reasoning", "provider"}}
                fallback = "qwen/qwen3-30b-a3b-instruct-2507"
                body.update(model=fallback, **model_options(fallback, client=CLIENT))
                continue
            raise error
        if response.status_code in RETRY_STATUS and attempt < ATTEMPTS - 1:
            delay = min(retry_delay(response, attempt), max(0, deadline - time.monotonic()))
            if runtime:
                if runtime.pause.wait(delay):
                    raise ModelPaused("Pausa solicitada durante a espera do provedor.")
            else:
                time.sleep(delay)
            continue
        if response.status_code == 429:
            raise RuntimeError("o provedor do modelo está limitando as requisições (HTTP 429). "
                               "Aguarde alguns segundos e envie de novo; nenhuma ação foi executada")
        if response.is_error:
            raise RuntimeError(f"o provedor do modelo retornou HTTP {response.status_code}; "
                               "nenhuma ação foi executada")
        return result


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise InvalidOutput("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls, contexts = [], {}, {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT",
                  "press_enter": "PRESS_ENTER", "press_escape": "PRESS_ESCAPE"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded", "href", "form")
                       if k in action}
            if action.get("context"):
                scope = action["context"]
                owner = contexts.setdefault(scope, index)
                element["context_reference"] = owner
                if owner == index:
                    element["context"] = scope
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def choose(*args, **kwargs):
    return reask_once(lambda: _choose(*args, **kwargs))


def choice_margin(answer):
    probabilities = sorted(answer["probabilities"].values(), reverse=True)
    return probabilities[0] - probabilities[1] if len(probabilities) > 1 else 1.0


def _choose(state, goal, history, *, conversation=None, verification_feedback=None, task_context=None):
    # A self-link without a fragment cannot advance navigation; keep real anchors and pagination.
    actions = [a for a in state["actions"] if not (
        a.get("navigation") is not False and a.get("href") == state["url"] and "#" not in a["href"])]
    # Reconsider another operation instead of repeating an ineffective click on the same observation.
    last_manual = max((i for i, h in enumerate(history) if h.get("kind") == "manual"), default=-1)
    ineffective = {h["node"] for h in history[last_manual + 1:] if h.get("execution") == "executed"
                   and h.get("kind") == "click" and h.get("page_changed") is False
                   and h.get("fingerprint") == state["fingerprint"] and h.get("node") is not None}
    recent = [h for h in history[last_manual + 1:] if h.get("execution") == "executed"][-2:]
    if (len(recent) == 2 and all(h.get("kind") == "click" for h in recent)
            and recent[0].get("node") is not None
            and recent[0].get("node") != recent[1].get("node")
            and recent[0].get("fingerprint") == state["fingerprint"]):
        ineffective.add(recent[0].get("node"))  # The second click returned to the first page.
    actions = [a for a in actions if not (a["kind"] == "click" and a.get("node") in ineffective)]
    navigations = Counter(h.get("href") for h in history if h.get("execution") == "executed"
                          and h.get("source_url") == state["url"] and h.get("href"))
    actions = [a for a in actions if not (a.get("href") and a.get("navigation") is not False
               and "#" not in a["href"] and navigations[a["href"]] >= 2)]
    for previous in reversed(history):
        if previous.get("execution") == "executed" and previous.get("search_submit"):
            actions = [a for a in actions if not (
                a.get("search_submit") and previous.get("url") == state["url"]
                and a.get("form_values") == previous.get("form_values"))]
            break
    elements, targets, controls = action_space(actions)
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text using the text model for the selected field.",
        "SELECT": "Select an observed dropdown value.",
        "PRESS_ENTER": "Press Enter in an observed single-line field to submit or accept its current value.",
        "PRESS_ESCAPE": "Dismiss an observed dialog with Escape to reach the content behind it.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    focus = (task_context or {}).get("guidance") or {}
    objective = focus.get("next_subgoal") or goal
    instructions = (f"Goal: {objective}\nFollow state.policy and state.task_context. "
                    "Choose one operation from the CURRENT page.")
    if verification_feedback:
        instructions += "\nObtain current evidence for these unmet requirements before DONE: " + json.dumps(
            verification_feedback, ensure_ascii=False)
    questions = {
        "operation": {
            "type": "choice",
            "criteria": operations,
            "instructions": instructions,
        }
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: json.dumps(
                    {
                        "element": f"[{index}] {a['label']}",
                        "current_value": a.get("current_value", a.get("value", "")),
                        **{k: a[k] for k in ("role", "checked", "selected", "expanded", "href") if k in a},
                    },
                    ensure_ascii=False,
                )
                for index, a in candidates.items()
            },
            "instructions": f"Current objective: {objective}\n"
                            f"Follow state.policy, state.goal, state.task_context and state.verification_feedback.\n"
                            f"Operation: {operation}\n{TARGET}",
        }
    # Speculative like the target heads: consumed only when the operation is BLOCKED.
    questions["blocked_reason"] = {
        "type": "choice",
        "criteria": BLOCKED_REASONS,
        "instructions": f"Goal: {goal}\n{BLOCKED_REASON}",
    }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "typesafe/jev-1.13"),
        "state": {
            "goal": goal,
            "policy": NEXT_ACTION,
            "page": {**{k: state[k] for k in ("url", "title", "text")},
                     "human_fields": state.get("human_fields", []),
                     "observation_limits": {"text_truncated": state.get("text_truncated", False),
                                            "omitted_actions": state.get("omitted_actions", 0)}},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed", "execution", "skip_reason", "url")}
                for h in history if h.get("execution", "executed") == "executed" or h.get("skip_reason")
            ][-10:],
        },
        "questions": questions,
    }
    if conversation:
        body["state"]["conversation"] = conversation[-3:] if task_context else conversation
    if task_context:
        body["state"]["task_context"] = task_context
    if verification_feedback:
        body["state"]["verification_feedback"] = verification_feedback
    # Long tracking URLs must not evict useful results later in document order. Compact only the
    # model's display; target mappings and the browser's original observed actions stay untouched.
    if len(json.dumps(body, ensure_ascii=False).encode()) > 30000:
        def compact(value):
            if isinstance(value, str):
                return value if len(value) <= 180 else value[:180] + " [truncated]"
            if isinstance(value, dict):
                return {key: compact(child) for key, child in value.items()}
            if isinstance(value, list):
                return [compact(child) for child in value]
            return value

        body["state"]["elements"] = compact(elements)
        body["state"]["page"]["observation_limits"]["element_details_truncated"] = True
        for operation, candidates in targets.items():
            body["questions"][operation.lower() + "_target"]["criteria"] = {
                index: json.dumps(compact({
                    "element": f"[{index}] {action['label']}",
                    "current_value": action.get("current_value", action.get("value", "")),
                    **{key: action[key] for key in ("role", "checked", "selected", "expanded", "href")
                       if key in action},
                }), ensure_ascii=False) for index, action in candidates.items()
            }
    # UTF-8 bytes are a conservative token bound; reduce candidates only after compacting details.
    if len(json.dumps(body, ensure_ascii=False).encode()) > 30000:
        targeted = [action for action in actions if action["kind"] in
                    {"click", "fill", "select", "press_enter", "press_escape"}]
        if not targeted:
            raise ValueError("O contexto excede a janela do modelo de decisão; nenhuma ação foi executada.")
        keep = len(targeted) // 2
        trimmed = [*targeted[:keep], *(action for action in actions if action not in targeted)]
        return _choose({**state, "actions": trimmed,
                        "omitted_actions": state.get("omitted_actions", 0) + len(targeted) - keep},
                       goal, history, conversation=conversation, verification_feedback=verification_feedback,
                       task_context=task_context)
    started = time.perf_counter()
    result = post_json(DECISIONS_URL, openrouter_key(), body)
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict):
        raise InvalidOutput("Invalid TypeSafe response; no action executed.")
    operation_answer = validate_choice(answers.get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(answers.get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    blocked_reason = None
    if operation == "BLOCKED":
        try:
            blocked_reason = validate_choice(answers.get("blocked_reason", {}), BLOCKED_REASONS)["choice"]
        except ValueError:
            # The reason only words the request for help; it can never cause an action.
            blocked_reason = "OTHER"
    operation_margin = choice_margin(operation_answer)
    target_margin = choice_margin(target_answer) if target_answer else None
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "operation_margin": operation_margin,
        "target_margin": target_margin,
        # Only numerical ties are gated until task-specific confidence thresholds are calibrated.
        "ambiguous": operation_margin <= 1e-6 or (target_margin is not None and target_margin <= 1e-6),
        "blocked_reason": blocked_reason,
        "raw_answers": answers,
        "model": result.get("model", "unknown"),
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal, action, page, history, *, task_context=None):
    context = {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value", "context")},
        "page": {"url": page["url"], "title": page["title"], "text": page["text"][:6000],
                 "observation_limits": {"text_truncated": page.get("text_truncated", False),
                                        "omitted_actions": page.get("omitted_actions", 0)}},
        "recent_actions": [
            {k: h.get(k) for k in ("action", "text")}
            for h in history if h.get("execution", "executed") == "executed"
        ][-6:],
    }
    if task_context:
        context["task_context"] = task_context
    return context


def field_text(context):
    return reask_once(lambda: _field_text(context))


def _field_text(context):
    key = openrouter_key()
    model = os.environ.get("TEXT_MODEL", "inception/mercury-2.5")
    options = model_options(model, client=CLIENT)
    started = time.perf_counter()
    result = post_json(
        CHAT_URL,
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "field_text",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"text": {"type": ["string", "null"]}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            },
            **options,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        if output == {"text": None}:
            raise MissingValue("Goal does not supply this field's value; nothing typed.")
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except MissingValue:
        raise
    except (ValueError, KeyError, TypeError, IndexError):
        raise InvalidOutput("Text helper returned no valid field value; nothing typed.") from None
    return value, {
        "model": result.get("model", model),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
