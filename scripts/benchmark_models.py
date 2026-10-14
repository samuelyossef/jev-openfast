"""Explicit paid comparison: real Chrome, local fixtures and an independent outcome oracle.

Run with --live. It never changes model defaults and is never invoked by pytest.
"""

import argparse
import hashlib
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import mean

MODELS = ("inception/mercury-2.5", "qwen/qwen3-30b-a3b-instruct-2507", "z-ai/glm-4.7-flash")
CASES = {
    "books": {
        "goal": "Busque dois livros de colorir infantis de R$15 a R$25. Pare nos resultados e não compre.",
        "html": """<h1>Loja de livros</h1><form role="search" onsubmit="event.preventDefault();
          document.querySelector('#results').hidden=false">
          <label>Busca<input type="search"></label><button>Buscar</button></form>
          <section id="results" hidden><h2>Resultados para livros de colorir infantis</h2>
          <ul><li><a href="#livro-a">Livro de colorir infantil A</a> R$ 20,00</li>
          <li><a href="#livro-b">Livro de colorir infantil B</a> R$ 22,00</li></ul></section>""",
        "expected": (
            "Resultados para livros de colorir infantis",
            "Livro de colorir infantil A",
            "R$ 20,00",
            "Livro de colorir infantil B",
            "R$ 22,00",
        ),
    },
    "filter": {
        "goal": "Aplique um filtro de R$15 a R$25 e pare nos resultados.",
        "html": """<h1>Filtro de preço</h1><form onsubmit="event.preventDefault();
          document.querySelector('#results').textContent=Number(this.low.value)===15 &&
          Number(this.high.value)===25 ? 'Filtro aplicado: R$ 15 a R$ 25. Resultados filtrados.' : 'Faixa incorreta'">
          <label>Preço mínimo<input name="low" type="number"></label>
          <label>Preço máximo<input name="high" type="number"></label>
          <button>Aplicar filtro</button></form><p id="results"></p>""",
        "expected": ("Filtro aplicado: R$ 15 a R$ 25", "Resultados filtrados"),
    },
    "article": {
        "goal": "Encontre e abra o artigo mais recente do Canal Aurora sobre o Modelo Azul.",
        "html": """<h1>Canal Aurora</h1><p>Publicações em ordem da mais recente para a mais antiga</p>
          <a href="/detail">Modelo Azul — artigo mais recente do Canal Aurora</a>""",
        "expected": ("Página do artigo", "Artigo mais recente do Canal Aurora", "Modelo Azul"),
    },
}
DETAIL = "<h1>Página do artigo: Modelo Azul</h1><p>Artigo mais recente do Canal Aurora. Publicado em 2026-10-14.</p>"


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        case = CASES.get(self.path.lstrip("/"))
        html = DETAIL if self.path == "/detail" else case["html"] if case else None
        if html is None:
            self.send_error(404)
            return
        body = (
            "<!doctype html><meta charset='utf-8'><title>JEV model benchmark</title>"
            "<style>body{font:18px sans-serif;margin:24px}input,button{margin:8px;padding:8px}</style>" + html
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def compare(rows, repeats):
    summaries = {}
    expected_samples = len(CASES) * repeats
    for name in MODELS:
        samples = [row for row in rows if row["model"] == name]
        if not samples:
            continue
        times = sorted(row["elapsed_ms"] for row in samples)
        successful = sum(row["correct"] for row in samples)
        cost = sum(row["usage"]["consumed_usd"] for row in samples)
        summaries[name] = {
            "samples": len(samples),
            "complete_sample": len(samples) == expected_samples,
            "correct": successful,
            "false_successes": sum(row["false_success"] for row in samples),
            "unsafe": sum(row["unsafe"] for row in samples),
            "p95_ms": times[math.ceil(len(times) * 0.95) - 1],
            "mean_actions": mean(row["actions"] for row in samples),
            "cost_usd": cost,
            "cost_per_success": cost / successful if successful else None,
        }
    baseline = summaries.get(MODELS[0])
    eligible = []
    if baseline and baseline["complete_sample"]:
        for name, candidate in summaries.items():
            if (
                name != MODELS[0]
                and candidate["complete_sample"]
                and candidate["correct"] >= baseline["correct"]
                and candidate["correct"] > 0
                and candidate["false_successes"] == candidate["unsafe"] == 0
                and candidate["p95_ms"] <= baseline["p95_ms"] * 1.2
            ):
                eligible.append(name)
    eligible.sort(key=lambda name: summaries[name]["cost_per_success"])
    return {
        "models": summaries,
        "eligible_open_models": eligible,
        "recommendation": eligible[0] if eligible else MODELS[0],
        "default_changed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--total-budget-usd", type=float, default=1)
    parser.add_argument("--task-budget-usd", type=float, default=0.10)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not args.live:
        parser.error("Use --live explicitamente: esta comparação chama APIs pagas.")
    if args.repeats < 1 or not all(
        math.isfinite(value) and value > 0 for value in (args.total_budget_usd, args.task_budget_usd, args.timeout)
    ):
        parser.error("Repetições, prazos e orçamentos devem ser positivos.")
    port = Path("artifacts/chrome-profile/DevToolsActivePort").read_text().splitlines()[0]
    os.environ.update(BU_CDP_URL=f"http://127.0.0.1:{port}", BU_NAME="jev-model-benchmark", JEV_HEADLESS_BROWSER="1")
    source_files = [*Path("jev_ultrafast").glob("*.py"), Path("jev_ultrafast/snapshot.js"),
                    Path("jev_ultrafast/privacy.js")]
    sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    from jev_ultrafast.chat import ChatSession
    from jev_ultrafast.demo import load_environment

    load_environment()
    output = Path("artifacts/model-benchmark") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    rows = []
    print(f"Report: {output}", flush=True)
    try:
        for model in MODELS:
            os.environ["TEXT_MODEL"] = model
            for repeat in range(args.repeats):
                for name, case in CASES.items():
                    remaining = args.total_budget_usd - sum(row["usage"]["consumed_usd"] for row in rows)
                    if remaining <= 0.005:
                        return
                    os.environ["MODEL_TASK_BUDGET_USD"] = str(min(args.task_budget_usd, remaining))
                    url = f"http://127.0.0.1:{server.server_port}/{name}"
                    trace = output / f"{len(rows) + 1:02d}-{name}.json"

                    def save_trace(state):
                        temporary = trace.with_suffix(".tmp")
                        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                        temporary.replace(trace)

                    session = ChatSession(url, on_change=save_trace)
                    started = time.perf_counter()
                    try:
                        session.message({"message": f"Abra {url}\n{case['goal']}", "message_id": f"{name}-{repeat}"})
                        session.worker.join(args.timeout)
                        if session.worker.is_alive():
                            session.pause_requested.set()
                            session.worker.join(50)
                        if session.worker.is_alive():
                            raise RuntimeError("O caso excedeu o prazo e não liberou o navegador.")
                        state = session.snapshot()
                        page = session.agent.browser.observe(screenshot=False)
                        valid = all(quote in page["text"] for quote in case["expected"])
                        claimed = state["messages"][-1].get("verification", {}).get("satisfied", False)
                        unsafe = any(
                            item["execution"] == "uncertain" for item in state["history"]
                        )
                        row = {
                            "model": model,
                            "case": name,
                            "repeat": repeat,
                            "correct": valid and claimed,
                            "false_success": claimed and not valid,
                            "unsafe": unsafe,
                            "unnecessary_confirmations": int(bool(session.pending)),
                            "elapsed_ms": round((time.perf_counter() - started) * 1000),
                            "actions": state["action_count"],
                            "decisions": state["decision_count"],
                            "usage": state["model_usage"],
                            "phase": state["chat_status"],
                        }
                        rows.append(row)
                        (output / f"{len(rows):02d}-{name}.json").write_text(
                            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                        summary = {
                            **row,
                            "usage": {key: value for key, value in row["usage"].items() if key != "calls"},
                        }
                        print(json.dumps(summary, ensure_ascii=False), flush=True)
                    finally:
                        session.close()
    finally:
        server.shutdown()
        server.server_close()
        report = {"results": rows, "source_hashes": sources, **compare(rows, args.repeats)}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps({key: value for key, value in report.items() if key not in {"results", "source_hashes"}}),
            flush=True,
        )


if __name__ == "__main__":
    main()
