"""Bounded, read-only HTTP load test for the local JEV UI."""

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

RATES = (5, 20, 40)


def signature(state):
    return {key: state.get(key) for key in ("session_id", "messages", "model_usage", "action_count", "chat_status")}


async def request(client, path, expected_version):
    start = time.perf_counter()
    try:
        response = await client.get(path)
        response.raise_for_status()
        if path in ("/", "/settings"):
            assert "text/html" in response.headers.get("content-type", "")
            assert "<html" in response.text.lower()
        else:
            body = response.json()
            if path.startswith("/api/state"):
                assert body["version"] == expected_version and "chat_status" in body
            elif path == "/api/conversations":
                assert isinstance(body["conversations"], list)
            else:
                assert isinstance(body, dict)
        return {"path": path.split("?")[0], "ms": (time.perf_counter() - start) * 1000, "status": 200}
    except (httpx.HTTPError, ValueError, KeyError, AssertionError) as error:
        return {
            "path": path.split("?")[0],
            "ms": (time.perf_counter() - start) * 1000,
            "status": getattr(getattr(error, "response", None), "status_code", 0),
            "error": type(error).__name__,
        }


def percentile(values, fraction):
    if not values:
        return None
    return round(sorted(values)[min(len(values) - 1, int(len(values) * fraction))], 1)


async def stage(client, paths, version, rate, seconds):
    semaphore = asyncio.Semaphore(16)
    tasks = []
    skipped = 0
    start = time.perf_counter()

    async def one(path):
        try:
            return await request(client, path, version)
        finally:
            semaphore.release()

    for index in range(rate * seconds):
        await asyncio.sleep(max(0, start + index / rate - time.perf_counter()))
        if semaphore.locked():
            skipped += 1
            continue
        await semaphore.acquire()
        tasks.append(asyncio.create_task(one(paths[index % len(paths)])))
    await asyncio.sleep(max(0, start + seconds - time.perf_counter()))
    results = await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - start
    good = [item["ms"] for item in results if item["status"] == 200]
    return {
        "target_rps": rate,
        "seconds": round(elapsed, 2),
        "requests": len(results),
        "achieved_rps": round(len(results) / elapsed, 1),
        "skipped_at_concurrency_limit": skipped,
        "status_codes": dict(Counter(item["status"] for item in results)),
        "errors": dict(Counter(item.get("error") for item in results if "error" in item)),
        "p50_ms": percentile(good, 0.50),
        "p95_ms": percentile(good, 0.95),
        "p99_ms": percentile(good, 0.99),
        "max_ms": round(max(good), 1) if good else None,
        "by_path": dict(Counter(item["path"] for item in results)),
    }


async def run(seconds):
    base = "http://127.0.0.1:8766"
    async with httpx.AsyncClient(base_url=base, timeout=10, limits=httpx.Limits(max_connections=16)) as client:
        before_response = await client.get("/api/state?compact=1")
        before_response.raise_for_status()
        before = before_response.json()
        if before.get("chat_status") != "idle":
            raise RuntimeError("A sessão está em execução; teste de carga cancelado para evitar interferência.")
        paths = ["/api/state?compact=1", "/api/state?compact=1", "/api/state", "/api/conversations", "/", "/settings"]
        if before.get("session_id"):
            paths.append(f"/api/preview?session_id={before['session_id']}")
        stages = [await stage(client, paths, before["version"], rate, seconds) for rate in RATES]
        after_response = await client.get("/api/state?compact=1")
        after_response.raise_for_status()
        after = after_response.json()
    unchanged = signature(before) == signature(after)
    passed = unchanged and all(
        s["status_codes"].get(200, 0) == s["requests"] and s["skipped_at_concurrency_limit"] == 0 for s in stages
    )
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "version": before["version"],
        "initial_chat_status": before["chat_status"],
        "state_unchanged": unchanged,
        "passed": passed,
        "stages": stages,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage-seconds", type=int, default=10, help="Duration of each of the 5, 20, 40 req/s stages (1-60)"
    )
    args = parser.parse_args()
    if not 1 <= args.stage_seconds <= 60:
        parser.error("--stage-seconds must be between 1 and 60")
    result = asyncio.run(run(args.stage_seconds))
    output = Path("artifacts") / f"load-test-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"Report: {output}")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
