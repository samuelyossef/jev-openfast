"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from .browser import Browser, StalePage
from .model import MissingValue, action_space, choose, field_context, field_text
from .questions import MAX_STEPS
from .timing import Timings, timed


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False, viewport=None):
        task = "" if goals is None else goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if goals is not None and not task:
            raise ValueError("Supply a task")
        plan = [task] if task else []
        self.pending_text = None
        self.browser = Browser(url, viewport=viewport) if viewport is not None else Browser(url)
        self.timings = getattr(self.browser, "timings", Timings())
        self.browser.timings = self.timings
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready" if task else "idle",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            stop_reason=None,
            verification_feedback=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            if page.get("screenshot"):
                (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def start_task(self, goal, conversation=None):
        """Start another goal in the owned tab, with fresh per-task budgets and caches."""
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("Supply a task")
        page = self.browser.observe(screenshot=self.screenshots)
        self.pending_text = None
        self.state.update(
            goal=goal.strip(), page=page, decision=None, history=[], status="ready",
            plan=[goal.strip()], plan_index=0, decisions=[], text_calls=[], elapsed_ms=0,
            started_at=None, stop_reason=None, conversation=conversation or [],
            verification_feedback=None,
        )

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def prepare_text(self, action, page):
        """Prepare once; reuse only while every part of the helper context stays identical."""
        if not self.state["browser"].fresh(page, action):
            raise StalePage("Page changed before text generation. Choose again.")
        context = field_context(self.state["goal"], action, page, self.state["history"])
        if self.state.get("conversation"):
            context["conversation"] = self.state["conversation"]
        if self.pending_text and self.pending_text[0] == context:
            _, text, helper = self.pending_text
        else:
            with self.timings.measure("text_model"):
                text, helper = field_text(context)
            self.pending_text = (context, text, helper)
            self.state["text_calls"].append({**helper, "field": action["label"], "value": text})
        return text, helper

    def command(self, name, body=None):
        body = body or {}
        if name == "tick":
            try:
                self._predict()
                self._act({"fingerprint": self.state["page"]["fingerprint"]})
            except StalePage:
                state = self.state
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        elif name == "predict":
            self._predict()
        elif name == "act":
            self._act(body)
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    @timed("decision")
    def _predict(self):
        state = self.state
        if not state["browser"]:
            raise ValueError("Start a demo first")
        if state["started_at"] is None:
            state["started_at"] = time.perf_counter()
        if not state["browser"].fresh(state["page"]):
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
        state["decision"] = None
        if state["status"] in {"done", "blocked"}:
            raise ValueError("This run has stopped. Start a fresh demo.")
        if len(state["decisions"]) >= MAX_STEPS * 2:
            raise ValueError("Reached the demo's model-call budget")
        context = {"conversation": state["conversation"]} if state.get("conversation") else {}
        if state.get("verification_feedback"):
            context["verification_feedback"] = state["verification_feedback"]
        with self.timings.measure("decision_model"):
            state["decision"] = choose(state["page"], state["goal"], state["history"], **context)
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        state["decisions"].append(
            {
                **state["decision"],
                "fingerprint": state["page"]["fingerprint"],
                "elapsed_ms": state["elapsed_ms"],
            }
        )
        state["status"] = "predicted"

    def _act(self, body):
        state = self.state
        decision, page = state["decision"], state["page"]
        if not decision or body.get("fingerprint") != page["fingerprint"]:
            raise ValueError("Observe and choose before acting")
        # Consume once, before any mutation or model call. A retry cannot double-click.
        state["decision"] = None
        selected = decision["choice"]
        if selected in {"DONE", "BLOCKED"}:
            if not state["browser"].fresh(page):
                state["status"] = "ready"
                raise StalePage("Page changed since the decision. Choose again.")
            state["status"] = "done" if selected == "DONE" else "blocked"
            if selected == "BLOCKED":
                state["stop_reason"] = "Jev chose BLOCKED"
            state["plan_index"] = int(selected == "DONE")
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            return
        action = next(a for a in page["actions"] if a["id"] == selected)
        if len(state["history"]) >= MAX_STEPS:
            state["status"] = "blocked"
            raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
        text, helper = None, None
        if action["kind"] == "fill":
            try:
                text, helper = self.prepare_text(action, page)
            except MissingValue as error:
                # Nothing was typed; only the user can supply this value.
                state["status"], state["stop_reason"] = "blocked", str(error)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return
        # Record the attempt before browser input, including an uncertain transport result.
        entry = {
            "step": len(state["history"]) + 1,
            "action": action["label"],
            "kind": action["kind"],
            "choice": selected,
            "probability": decision["probabilities"][selected],
            "confidence": decision["confidence"],
            "latency_ms": decision["latency_ms"],
            "text": text,
            "text_helper": helper["model"] if helper else None,
            "text_latency_ms": helper["latency_ms"] if helper else 0,
            "operation": decision["operation"],
            "target": decision["target"],
            "execution": "requested",
            "page_changed": None,
            "url": page["url"],
            "usage": decision["usage"],
            "executed_ms": None,
            "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
        }
        state["history"].append(entry)
        try:
            # Browser.act checks freshness immediately before input, including after text generation.
            state["browser"].act(action, page, text=text)
            # Only mark as executed if the action succeeded
            entry["execution"] = "executed"
            self.pending_text = None
            entry["executed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        except StalePage:
            entry["execution"] = "not_executed"
            raise
        except Exception:
            entry["execution"] = "uncertain"
            raise
        finally:
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            entry["elapsed_ms"] = state["elapsed_ms"]
        # An interrupted post-action observation must not erase a completed action.
        state["page"] = state["browser"].observe(screenshot=self.screenshots)
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        entry.update(
            page_changed=state["page"]["fingerprint"] != page["fingerprint"],
            url=state["page"]["url"],
            elapsed_ms=state["elapsed_ms"],
        )
        if state["record"] and state["page"].get("screenshot"):
            (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                base64.b64decode(state["page"]["screenshot"])
            )
        executed = [item for item in state["history"] if item["execution"] == "executed"]
        repeated = executed[-3:]
        # A manual-control return restarts the window: a human may have changed what the next action does.
        unchanged = [h["page_changed"] is False and h["kind"] not in {"wait", "manual"} for h in repeated]
        state["status"] = "blocked" if len(repeated) == 3 and all(unchanged) else "ready"
        if state["status"] == "blocked":
            state["stop_reason"] = "No page change after 3 actions"
        recent = executed[-6:]
        if (
            len(recent) == 6
            and all(h["kind"] == "click" for h in recent)
            and recent[0]["action"] != recent[1]["action"]
            and all(h["action"] == recent[i % 2]["action"] for i, h in enumerate(recent))
        ):
            state["status"] = "blocked"
            state["stop_reason"] = "repeated two-action loop"

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
