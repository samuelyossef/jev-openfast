"""Loopback-only HTTP server and chat UI for the JEV OpenFast Browser agent."""

import atexit
import json
import mimetypes
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .browser import validate_viewport
from .chat import ChatSession, validate_message, validate_navigation, validate_url
from .conversations import ConversationStore
from .model import openrouter_key_status, validate_openrouter_key
from .questions import MAX_STEPS
from .secrets_store import decrypt_transport_key, save_openrouter_key, transport_public_key

ROOT = Path(__file__).parent
WEB_ROOT = ROOT / "web"
PORT = int(os.environ.get("TYPESAFE_DEMO_PORT", "8766"))
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
SESSION = None
STORE = ConversationStore()


def load_environment():
    path = Path.cwd() / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


def response_state(compact=False, owner_token=None):
    session = SESSION
    state = session.snapshot(compact=compact) if session else {
        "page": None, "status": "idle", "history": [], "decision": None,
        "messages": [], "chat_status": "idle", "session_id": None, "approval": None,
        "progress": "Descreva o que deseja fazer.",
        "storage_error": None,
    }
    if owner_token is not None and session:
        state["manual"]["owned"] = session.manual.is_owner(owner_token)
    return {
        **state,
        "text_model": os.environ.get("TEXT_MODEL", "inception/mercury-2.5"),
        "max_steps": MAX_STEPS,
        "version": __version__,
        **openrouter_key_status(),
    }


def close_browser():
    global SESSION
    if SESSION:
        SESSION.pause_requested.set()
        if SESSION.worker:
            SESSION.worker.join(timeout=2)
        # Shutdown closes the owned tab even when a daemon worker is waiting on a provider.
        if SESSION.agent:
            SESSION.manual.close()
            SESSION.preview.close()
            SESSION.agent.close()
        SESSION = None


def validated_conversation_id(body):
    value = body.get("conversation_id")
    if not isinstance(value, str) or not value:
        raise ValueError("Conversa inválida.")
    return value


def command(name, body, owner_token=None):
    global SESSION
    if not isinstance(body, dict):
        raise ValueError("Envie um objeto JSON.")
    viewport = None
    if name in {"session", "message", "navigate"} and ("width" in body or "height" in body):
        viewport = validate_viewport(body.get("width"), body.get("height"))
    if name == "settings":
        key = decrypt_transport_key(body.get("encrypted_key")).strip()
        validate_openrouter_key(key)
        save_openrouter_key(key)
    elif name.startswith("manual/"):
        if SESSION is None or body.get("session_id") != SESSION.id:
            raise ValueError("Esta conversa não está mais ativa.")
        operation = name.removeprefix("manual/")
        owner = None
        if operation == "start":
            owner = SESSION.manual.start(body)
        elif operation == "input":
            SESSION.manual.input(body)
        elif operation == "end":
            SESSION.manual.end(body)
        else:
            raise ValueError("Comando manual desconhecido.")
        result = response_state(compact=True, owner_token=owner or body.get("owner_token", ""))
        if owner:
            result["manual_owner_token"] = owner
        return result
    elif name == "select":
        conversation_id = validated_conversation_id(body)
        if SESSION is None or SESSION.id != conversation_id:
            record = STORE.load(conversation_id)
            if SESSION:
                SESSION.close()
            SESSION = ChatSession.restore(record, on_change=STORE.save)
            STORE.select(conversation_id)
    elif name == "rename":
        conversation_id, title = validated_conversation_id(body), body.get("title")
        STORE.rename(conversation_id, title)
    elif name in {"archive", "unarchive"}:
        conversation_id = validated_conversation_id(body)
        archiving = name == "archive"
        active = SESSION is not None and SESSION.id == conversation_id
        if active and archiving:
            SESSION._check_idle()
        STORE.set_archived(conversation_id, archiving)
        if active and archiving:
            SESSION.close()
            SESSION = None
            STORE.select(None)
    elif name == "delete":
        conversation_id = validated_conversation_id(body)
        if SESSION and SESSION.id == conversation_id:
            SESSION.close()
            SESSION = None
        STORE.delete(conversation_id)
    elif name == "session":
        url = validate_url(body.get("url"))
        if SESSION:
            SESSION._check_idle()
        new_session = ChatSession(url, on_change=STORE.save, viewport=viewport)
        if SESSION:
            try:
                SESSION.close()
            except Exception:
                new_session.close()
                raise
        SESSION = new_session
        STORE.select(SESSION.id)
    elif name == "message" and SESSION is None:
        if body.get("session_id") is not None:
            raise ValueError("Esta conversa não está mais ativa. Recarregue a página.")
        validate_message(body)
        SESSION = ChatSession(on_change=STORE.save, viewport=viewport)
        SESSION.message(body)
        STORE.select(SESSION.id)
    elif name == "navigate" and SESSION is None:
        if body.get("session_id") is not None:
            raise ValueError("Esta conversa não está mais ativa. Recarregue a página.")
        if validate_navigation(body)[0] != "url":
            raise ValueError("Nenhuma página aberta para navegar.")
        new_session = ChatSession(on_change=STORE.save, viewport=viewport)
        try:
            new_session.navigate_user(body)
        except Exception:
            new_session.close()
            STORE.delete(new_session.id)  # the empty conversation row it saved is not a conversation
            raise
        SESSION = new_session
        STORE.select(SESSION.id)
    elif name == "reset":
        if SESSION is None or body.get("session_id") != SESSION.id:
            raise ValueError("Esta conversa não está mais ativa. Recarregue a página.")
        SESSION.close()
        SESSION = None
        STORE.select(None)
    else:
        if SESSION is None or body.get("session_id") != SESSION.id:
            raise ValueError("Esta conversa não está mais ativa. Recarregue a página.")
        if name == "viewport" and SESSION.manual.status != "off" and not SESSION.manual.is_owner(owner_token):
            raise ValueError("A prévia está em leitura nesta interface durante o controle manual.")
        actions = {
            "message": lambda: SESSION.message(body), "approve": lambda: SESSION.approve(body),
            "reject": lambda: SESSION.reject(body), "pause": SESSION.pause, "resume": SESSION.resume,
            "verify": SESSION.recheck, "navigate": lambda: SESSION.navigate_user(body),
            "viewport": lambda: SESSION.resize_viewport(body.get("width"), body.get("height")),
        }
        if name not in actions:
            raise ValueError("Comando desconhecido.")
        actions[name]()
    return response_state(owner_token=owner_token)


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, mime="application/json"):
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, "Forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            return self.send(204, b"", "image/x-icon")
        if path == "/api/state":
            compact = parse_qs(urlparse(self.path).query).get("compact") == ["1"]
            return self.send(200, json.dumps(response_state(compact=compact,
                                                          owner_token=self.headers.get("X-Manual-Control", ""))))
        if path == "/api/preview":
            session = SESSION
            query = parse_qs(urlparse(self.path).query)
            if not session or query.get("session_id") != [session.id]:
                return self.send(404, json.dumps({"error": "Esta prévia não está mais ativa."}))
            preview = session.preview.snapshot(after=query.get("after", [None])[0])
            return self.send(200, json.dumps(preview))
        if path == "/api/public-key":
            return self.send(200, json.dumps({"public_key": transport_public_key()}))
        if path == "/api/conversations":
            session = SESSION
            result = {"conversations": STORE.list(), "active_id": session.id if session else None}
            return self.send(200, json.dumps(result))
        if path == "/demo.mp4":
            video = ROOT.parent / "docs" / "demo.mp4"
            if video.exists():
                return self.send(200, video.read_bytes(), "video/mp4")
        if path in {"/", "/settings"} and (WEB_ROOT / "index.html").is_file():
            html = (WEB_ROOT / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            return self.send(200, html, "text/html; charset=utf-8")
        if path.startswith("/assets/") or path == "/favicon.svg":
            asset = (WEB_ROOT / path.lstrip("/")).resolve()
            if asset.is_relative_to(WEB_ROOT.resolve()) and asset.is_file():
                mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                return self.send(200, asset.read_bytes(), mime)
        files = {
            "/": ("index.html", "text/html"),
            "/settings": ("settings.html", "text/html"),
            "/settings.js": ("settings.js", "text/javascript"),
            "/app.js": ("app.js", "text/javascript"),
            "/manual.js": ("manual.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),
            "/fixture.html": ("fixture.html", "text/html"),
        }
        if path not in files:
            return self.send(404, "Not found", "text/plain")
        name, mime = files[path]
        content = (ROOT / "static" / name).read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
        self.send(200, content, mime + "; charset=utf-8")

    def reject_post(self, status, content):
        # Drain a bounded rejected body so Windows delivers the error response
        # instead of resetting a socket that still has unread request data.
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if 0 < length < 32768:
                self.connection.settimeout(1)
                self.rfile.read(length)
        except (ValueError, OSError):
            pass
        return self.send(status, content)

    def do_POST(self):
        if (
            self.headers.get("Host") != f"127.0.0.1:{PORT}"
            or self.headers.get("X-Demo-Token") != TOKEN
            or self.headers.get("Origin") not in (None, ORIGIN)
        ):
            return self.reject_post(403, json.dumps({"error": "Local demo requests only"}))
        if not LOCK.acquire(blocking=False):
            return self.reject_post(409, json.dumps({"error": "Outra solicitação está sendo processada."}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 32768:
                raise ValueError("Invalid request size")
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(length))
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                raise ValueError("Comando desconhecido.")
            result = command(path.removeprefix("/api/"), body, owner_token=self.headers.get("X-Manual-Control", ""))
            self.send(200, json.dumps(result))
        except (ValueError, RuntimeError, OSError) as error:
            self.send(400, json.dumps({"error": str(error)}))
        except Exception:
            self.send(500, json.dumps({"error": "Falha local. A operação não será repetida automaticamente."}))
        finally:
            LOCK.release()

    def log_message(self, *_args):
        pass


def restore_selected():
    global SESSION
    selected = STORE.selected()
    if selected:
        try:
            SESSION = ChatSession.restore(STORE.load(selected), on_change=STORE.save)
        except ValueError:
            STORE.select(None)


def main():
    if "--version" in sys.argv[1:]:
        print(f"JEV OpenFast Browser {__version__}")
        return
    load_environment()
    if "--doctor" in sys.argv[1:]:
        from .doctor import run
        raise SystemExit(run(PORT))
    restore_selected()
    atexit.register(close_browser)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"JEV OpenFast Browser {__version__}: {ORIGIN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
