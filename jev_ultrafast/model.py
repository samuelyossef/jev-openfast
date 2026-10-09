"""Jev chooses observed browser actions; the text model supplies field values."""

import json
import math
import os
import time

import httpx

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


def reask_once(call):
    """Model requests never touch the browser, so one malformed answer is asked again before the task stops."""
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
ATTEMPTS = 5  # waits about 1+2+4+8 s; model requests never touch the browser, so retrying them is safe


def retry_delay(response, attempt):
    """Honor the provider's Retry-After (seconds), bounded so a stuck limit cannot hang a task."""
    try:
        return min(10.0, max(0.5, float(response.headers.get("retry-after", ""))))
    except ValueError:
        return 2.0**attempt


def post_json(url, key, body, *, on_attempt=None):
    for attempt in range(ATTEMPTS):
        if on_attempt:
            on_attempt()
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Falha de conexão com o provedor do modelo; nenhuma ação foi executada") from None
        if response.status_code in RETRY_STATUS and attempt < ATTEMPTS - 1:
            time.sleep(retry_delay(response, attempt))
            continue
        if response.status_code == 429:
            raise RuntimeError("o provedor do modelo está limitando as requisições (HTTP 429). "
                               "Aguarde alguns segundos e envie de novo; nenhuma ação foi executada")
        if response.is_error:
            raise RuntimeError(f"o provedor do modelo retornou HTTP {response.status_code}; "
                               "nenhuma ação foi executada")
        return response.json()


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
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT",
                  "press_enter": "PRESS_ENTER"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
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


def _choose(state, goal, history, *, conversation=None, verification_feedback=None):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text using the text model for the selected field.",
        "SELECT": "Select an observed dropdown value.",
        "PRESS_ENTER": "Press Enter in an observed single-line field to submit or accept its current value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {
            "type": "choice",
            "criteria": operations,
            "instructions": f"Goal: {goal}\n{NEXT_ACTION}",
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
                        **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                    },
                    ensure_ascii=False,
                )
                for index, a in candidates.items()
            },
            "instructions": f"Goal: {goal}\nOperation: {operation}\n{NEXT_ACTION}\n{TARGET}",
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
            "page": {**{k: state[k] for k in ("url", "title", "text")},
                     "human_fields": state.get("human_fields", [])},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed", "execution")}
                for h in history if h.get("execution", "executed") == "executed"
            ][-10:],
        },
        "questions": questions,
    }
    if conversation:
        body["state"]["conversation"] = conversation
    if verification_feedback:
        body["state"]["verification_feedback"] = verification_feedback
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
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "blocked_reason": blocked_reason,
        "raw_answers": answers,
        "model": result.get("model", "unknown"),
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [
            {k: h.get(k) for k in ("action", "text")}
            for h in history if h.get("execution", "executed") == "executed"
        ][-6:],
    }


def field_text(context):
    return reask_once(lambda: _field_text(context))


def _field_text(context):
    key = openrouter_key()
    model = os.environ.get("TEXT_MODEL", "inception/mercury-2.5")
    reasoning = {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING", "none") == "none":
        reasoning = {"reasoning": {"enabled": False}}
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
            "provider": {"require_parameters": True},
            **reasoning,
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
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
