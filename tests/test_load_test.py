"""The load runner validates responses without mutating the application."""

import asyncio
import json

import httpx

from scripts.load_test import request, stage


def test_stage_counts_read_only_requests_and_reports_latency():
    seen = []

    def respond(incoming):
        seen.append((incoming.method, incoming.url.path))
        return httpx.Response(200, json={"version": "0.2.1", "chat_status": "idle"})

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="http://127.0.0.1:8766"
        ) as client:
            return await stage(client, ["/api/state?compact=1"], "0.2.1", 5, 1)

    result = asyncio.run(check())
    assert result["requests"] == 5
    assert result["status_codes"] == {200: 5}
    assert result["p95_ms"] is not None
    assert seen == [("GET", "/api/state")] * 5


def test_invalid_response_is_counted_as_failure():
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=json.dumps({}))),
            base_url="http://127.0.0.1:8766",
        ) as client:
            return await request(client, "/api/state", "0.2.1")

    result = asyncio.run(check())
    assert result["status"] == 0
    assert result["error"] == "KeyError"
