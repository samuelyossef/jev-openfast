"""Small, server-owned history for the local Jev chat."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "artifacts" / "conversations.sqlite3"


class ConversationStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=3)
        try:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS conversations ("
                    "id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at INTEGER NOT NULL, "
                    "messages TEXT NOT NULL, phase TEXT NOT NULL, last_url TEXT, "
                    "archived INTEGER NOT NULL DEFAULT 0, title_custom INTEGER NOT NULL DEFAULT 0)"
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(conversations)")}
                if "archived" not in columns:
                    connection.execute("ALTER TABLE conversations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
                if "title_custom" not in columns:
                    connection.execute("ALTER TABLE conversations ADD COLUMN title_custom INTEGER NOT NULL DEFAULT 0")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS selection (id INTEGER PRIMARY KEY CHECK (id = 1), conversation_id TEXT)"
                )
                yield connection
        finally:
            connection.close()

    def save(self, state):
        messages = state["messages"]
        first = next(
            (m["content"].strip() for m in messages if m["role"] == "user" and m.get("kind") != "confirmation"),
            "",
        )
        title = first[:60].replace("\n", " ") or "Nova conversa"
        page = state.get("page") or {}
        with self._connect() as db:
            db.execute(
                "INSERT INTO conversations "
                "(id, title, updated_at, messages, phase, last_url) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "title=CASE WHEN conversations.title_custom=0 AND conversations.title='Nova conversa' "
                "THEN excluded.title ELSE conversations.title END, "
                "updated_at=excluded.updated_at, messages=excluded.messages, phase=excluded.phase, "
                "last_url=COALESCE(excluded.last_url, conversations.last_url)",
                (state["session_id"], title, time.time_ns() // 1_000_000, json.dumps(messages, ensure_ascii=False),
                 state["chat_status"], page.get("url") or state.get("last_url")),
            )

    def list(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, title, updated_at, phase, last_url, archived FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {**dict(zip(("id", "title", "updated_at", "phase", "last_url"), row[:5], strict=True)),
             "archived": bool(row[5])}
            for row in rows
        ]

    def load(self, conversation_id):
        with self._connect() as db:
            row = db.execute(
                "SELECT messages, phase, last_url FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Conversa não encontrada.")
        return {"id": conversation_id, "messages": json.loads(row[0]), "phase": row[1], "last_url": row[2]}

    def delete(self, conversation_id):
        with self._connect() as db:
            count = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,)).rowcount
            db.execute("UPDATE selection SET conversation_id=NULL WHERE conversation_id=?", (conversation_id,))
        if not count:
            raise ValueError("Conversa não encontrada.")

    def rename(self, conversation_id, title):
        title = " ".join(title.split()) if isinstance(title, str) else ""
        if not 1 <= len(title) <= 80:
            raise ValueError("O nome deve ter entre 1 e 80 caracteres.")
        with self._connect() as db:
            # A new name is not activity: keep updated_at so the history order does not change.
            count = db.execute(
                "UPDATE conversations SET title=?, title_custom=1 WHERE id=?", (title, conversation_id)
            ).rowcount
        if not count:
            raise ValueError("Conversa não encontrada.")

    def set_archived(self, conversation_id, archived):
        with self._connect() as db:
            count = db.execute(
                "UPDATE conversations SET archived=? WHERE id=?", (int(archived), conversation_id)
            ).rowcount
        if not count:
            raise ValueError("Conversa não encontrada.")

    def selected(self):
        with self._connect() as db:
            row = db.execute("SELECT conversation_id FROM selection WHERE id=1").fetchone()
        return row[0] if row else None

    def select(self, conversation_id):
        with self._connect() as db:
            db.execute(
                "INSERT INTO selection (id, conversation_id) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET conversation_id=excluded.conversation_id",
                (conversation_id,),
            )
