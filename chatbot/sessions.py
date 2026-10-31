"""Chat session/message persistence in the shared SQLite store.

Uses history_store.connect() (same WAL SQLite DB as observations/forecasts) so
the chatbot adds two tables without touching any existing schema or code.
Chart payloads are NOT persisted — replaying a chart means a fresh API call;
forecast numbers themselves live in forecast_runs/forecast_values already.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from history_store import connect

CHAT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    user_label TEXT,
    title TEXT NOT NULL DEFAULT 'New chat'
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    tools_used TEXT,
    chart_kind TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, id);
"""

DEFAULT_TITLE = "New chat"
_TITLE_LIMIT = 48


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _create_chat_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CHAT_SCHEMA_SQL)


def ensure_chat_schema() -> None:
    """Idempotent DDL; cheap enough to run per chat request (CREATE IF NOT EXISTS)."""
    with connect() as conn:
        _create_chat_schema(conn)


def create_session(user_label: str | None = None, title: str = DEFAULT_TITLE) -> dict[str, Any]:
    ensure_chat_schema()
    session = {
        "session_id": uuid.uuid4().hex,
        "created_at": _now(),
        "updated_at": _now(),
        "user_label": user_label,
        "title": title,
    }
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_sessions(session_id,created_at,updated_at,user_label,title) VALUES(?,?,?,?,?)",
            (session["session_id"], session["created_at"], session["updated_at"],
             session["user_label"], session["title"]),
        )
        conn.commit()
    return session


def get_session(session_id: str) -> dict[str, Any] | None:
    ensure_chat_schema()
    with connect() as conn:
        row = conn.execute(
            "SELECT session_id,created_at,updated_at,user_label,title FROM chat_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def touch_session(session_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE chat_sessions SET updated_at=? WHERE session_id=?", (_now(), session_id))
        conn.commit()


def set_session_title(session_id: str, title: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE chat_sessions SET title=?, updated_at=? WHERE session_id=?",
            (title.strip()[:_TITLE_LIMIT] or DEFAULT_TITLE, _now(), session_id),
        )
        conn.commit()


def auto_title_if_default(session_id: str, first_message: str) -> bool:
    """Set the title from the first user message unless the user already sees one."""
    session = get_session(session_id)
    if session and session["title"] == DEFAULT_TITLE:
        set_session_title(session_id, first_message.strip() or DEFAULT_TITLE)
        return True
    return False


def append_message(session_id: str, role: str, content: str,
                   tools_used: list[str] | None = None, chart_kind: str | None = None) -> None:
    ensure_chat_schema()
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages(session_id,role,content,tools_used,chart_kind,created_at) VALUES(?,?,?,?,?,?)",
            (session_id, role, content,
             json.dumps(tools_used or [], ensure_ascii=False),
             chart_kind, _now()),
        )
        conn.commit()
    touch_session(session_id)


def get_history(session_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Newest-last conversation slice (role/content only for the LLM)."""
    ensure_chat_schema()
    safe_limit = max(1, min(int(limit), 200))
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,role,content,tools_used,chart_kind,created_at FROM chat_messages "
            "WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, safe_limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def message_count(session_id: str) -> int:
    with connect() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE session_id=?", (session_id,)).fetchone()["n"])


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    ensure_chat_schema()
    safe_limit = max(1, min(int(limit), 200))
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.session_id, s.title, s.created_at, s.updated_at, s.user_label, "
            "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.session_id) AS messages_count "
            "FROM chat_sessions s ORDER BY s.updated_at DESC, s.created_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
    return [dict(r) for r in rows]
