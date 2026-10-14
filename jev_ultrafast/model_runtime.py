"""Per-task model accounting and cancellation; this module never operates the browser."""

import json
import math
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

CURRENT_RUNTIME = ContextVar("jev_model_runtime", default=None)
CURRENT_DEADLINE = ContextVar("jev_model_deadline", default=None)


class ModelBudgetExceeded(RuntimeError):
    pass


class ModelPaused(RuntimeError):
    pass


# Provider price ceilings in USD per million tokens, including failover providers.
PROFILES = {
    "typesafe/jev-1.13": (False, 0.042, 0),
    "inception/mercury-2.5": (True, 0.25, 0.75),
    "qwen/qwen3-30b-a3b-instruct-2507": (False, 0.15, 0.50),
    "z-ai/glm-4.7-flash": (True, 0.15, 0.50),
    "deepseek/deepseek-v4.1-flash": (True, 0.30, 1.20),
}


def model_options(model, *, deep=False, client=None):
    profile = PROFILES.get(model)
    if profile is None:
        if client is None:
            raise ValueError("Não foi possível conferir as capacidades deste modelo.")
        try:
            response = client.get(f"https://openrouter.ai/api/v1/models/{model}/endpoints", timeout=8)
            response.raise_for_status()
            endpoints = [endpoint for endpoint in response.json()["data"]["endpoints"]
                         if "structured_outputs" in endpoint.get("supported_parameters", [])]
            if not endpoints:
                raise ValueError("Este modelo não oferece o JSON estruturado exigido pelo JEV.")
            profile = (any("reasoning" in endpoint.get("supported_parameters", []) for endpoint in endpoints),
                       max(float(endpoint["pricing"]["prompt"]) * 1_000_000 for endpoint in endpoints),
                       max(float(endpoint["pricing"]["completion"]) * 1_000_000 for endpoint in endpoints))
            if not all(math.isfinite(rate) and rate >= 0 for rate in profile[1:]):
                raise ValueError("Preço do modelo inválido.")
            PROFILES[model] = profile
        except (KeyError, TypeError, OSError) as error:
            raise ValueError("Não foi possível conferir as capacidades deste modelo.") from error
    reasoning, prompt, completion = profile
    options = {"provider": {"require_parameters": True, "sort": "latency",
                            "max_price": {"prompt": prompt, "completion": completion}}}
    if reasoning:
        options["reasoning"] = ({"enabled": True} if deep else {"enabled": False})
        if not deep and os.environ.get("TEXT_MODEL_REASONING", "none") != "none":
            options["reasoning"] = {"effort": os.environ["TEXT_MODEL_REASONING"]}
    return options


@contextmanager
def request_scope(runtime):
    token = CURRENT_RUNTIME.set(runtime)
    try:
        yield
    finally:
        CURRENT_RUNTIME.reset(token)


@contextmanager
def deadline_scope(seconds):
    deadline = time.monotonic() + seconds
    current = CURRENT_DEADLINE.get()
    token = CURRENT_DEADLINE.set(min(current, deadline) if current is not None else deadline)
    try:
        yield
    finally:
        CURRENT_DEADLINE.reset(token)


class ModelRuntime:
    def __init__(self, pause=None, limit=None, on_change=None):
        self.limit = float(os.environ.get("MODEL_TASK_BUDGET_USD", "0.10") if limit is None else limit)
        if not math.isfinite(self.limit) or self.limit <= 0:
            raise ValueError("MODEL_TASK_BUDGET_USD deve ser positivo e finito.")
        self.pause = pause or threading.Event()
        self.lock = threading.Lock()
        self.records = []
        self.on_change = on_change

    def reserve(self, body, url):
        if self.pause.is_set():
            raise ModelPaused("Pausa solicitada antes da chamada ao modelo.")
        model = body["model"]
        rates = body.get("provider", {}).get("max_price")
        if rates is None:
            _, prompt, completion = PROFILES.get(model, (False, None, None))
            if prompt is None:
                raise ModelBudgetExceeded("Preço não confirmado; a chamada ao modelo foi interrompida.")
            rates = {"prompt": prompt, "completion": completion}
        # Reserve UTF-8 bytes plus template overhead as a conservative token estimate, then reconcile usage.
        input_bound = len(json.dumps(body, ensure_ascii=False).encode()) + 2048
        output_bound = body.get("max_tokens", 0)
        reserved = (input_bound * rates["prompt"] + output_bound * rates["completion"]) / 1_000_000
        kind = "decision" if url.endswith("decisions") else body.get("response_format", {}).get(
            "json_schema", {}).get("name", "text").removeprefix("chat_")
        with self.lock:
            consumed = sum(record["cost_usd"] for record in self.records)
            if consumed + reserved > self.limit:
                raise ModelBudgetExceeded("Limite de modelos desta tarefa atingido; nenhuma nova ação foi executada.")
            record = {"kind": kind, "requested_model": model, "model": model, "status": "pending",
                      "cost_usd": reserved, "estimated": True, "usage": {}, "started": time.perf_counter()}
            self.records.append(record)
        if self.on_change:
            self.on_change()
        return record

    def finish(self, record, result=None, error=None):
        with self.lock:
            record.update(status="error" if error else "ok",
                          latency_ms=round((time.perf_counter() - record.pop("started")) * 1000))
            if isinstance(result, dict):
                record["model"] = result.get("model", record["model"])
                record["request_id"] = result.get("id")
                usage = result.get("usage") or {}
                record["usage"] = {key: usage[key] for key in
                                   ("prompt_tokens", "completion_tokens", "total_tokens", "cost") if key in usage}
                cost = usage.get("cost")
                if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                    record.update(cost_usd=cost, estimated=False)
        if self.on_change:
            self.on_change()

    def snapshot(self):
        with self.lock:
            records = [{key: value for key, value in record.items() if key != "started"}
                       for record in self.records]
        return {"limit_usd": self.limit, "consumed_usd": sum(record["cost_usd"] for record in records),
                "estimated": any(record["estimated"] for record in records), "attempts": len(records),
                "calls": records}
