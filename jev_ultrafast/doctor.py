"""`jev --doctor`: read-only checks of everything Jev needs before it can drive a browser."""

import json
import os
import socket
import sys
from pathlib import Path
from urllib.request import urlopen

from . import __version__
from .model import openrouter_key_status

PROFILES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data",
    Path.home() / "Library/Application Support/Google/Chrome",
    Path.home() / ".config/google-chrome",
)


def chrome_endpoint():
    """The CDP endpoint Jev would use: BU_CDP_URL, else the personal Chrome's debugging port."""
    url = os.environ.get("BU_CDP_URL", "").strip()
    if url:
        return url, "BU_CDP_URL"
    for profile in PROFILES:
        active = profile / "DevToolsActivePort"
        if active.is_file():
            return f"http://127.0.0.1:{active.read_text(encoding='utf-8').split()[0]}", str(active)
    return None, None


def probe(url):
    with urlopen(url.rstrip("/") + "/json/version", timeout=2) as response:  # noqa: S310 - local debugging URL
        return json.load(response).get("Browser", "Chrome")


def checks(port):
    yield "JEV OpenFast Browser", True, __version__
    yield "Python 3.12+", sys.version_info >= (3, 12), sys.version.split()[0]
    key = openrouter_key_status()
    yield "OpenRouter key", key["openrouter_key_configured"], (
        key["openrouter_key_source"] if key["openrouter_key_configured"]
        else "add OPENROUTER_API_KEY to .env or paste it in Settings → Model")
    with socket.socket() as sock:
        free = sock.connect_ex(("127.0.0.1", port)) != 0
    yield f"Port {port} free", free, "ok" if free else "already in use (is Jev running? or set TYPESAFE_DEMO_PORT)"
    endpoint, source = chrome_endpoint()
    if not endpoint:
        yield "Chrome debugging", False, ("not found. Open chrome://inspect/#remote-debugging and enable it, "
                                          "or use scripts/start_windows.ps1")
        return
    try:
        yield "Chrome debugging", True, f"{probe(endpoint)} via {source}"
    except OSError:
        yield "Chrome debugging", False, f"{endpoint} ({source}) does not answer; restart Chrome with debugging on"


def run(port):
    failed = 0
    for name, ok, detail in checks(port):
        failed += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {name}: {detail}")
    print("All checks passed." if not failed else f"{failed} check(s) failed.")
    return 1 if failed else 0
