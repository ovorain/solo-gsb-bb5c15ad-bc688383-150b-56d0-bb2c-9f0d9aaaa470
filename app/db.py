"""SQLite persistence layer (single row of settings + events/attempts)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

DB_PATH = os.environ.get("REPLAY_DB", "/data/webhook_replay.db")

_lock = threading.RLock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _lock, get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                target_url TEXT NOT NULL DEFAULT 'http://127.0.0.1:8000/recv',
                hmac_secret TEXT NOT NULL DEFAULT 'shhh',
                max_retries INTEGER NOT NULL DEFAULT 3,
                timeout_seconds REAL NOT NULL DEFAULT 5.0
            );
            INSERT OR IGNORE INTO settings (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS events (
                event_id   TEXT PRIMARY KEY,
                seq        INTEGER NOT NULL,
                payload    TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attempts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
                attempt_no   INTEGER NOT NULL,
                replay_no    INTEGER NOT NULL,
                idem_key     TEXT NOT NULL,
                request_at   TEXT NOT NULL,
                response_at  TEXT,
                http_status  INTEGER,
                outcome      TEXT NOT NULL,
                elapsed_ms   INTEGER,
                req_headers  TEXT,
                resp_excerpt TEXT
            );
            """
        )


# ---------- settings ----------

def get_settings() -> dict[str, Any]:
    with _lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return dict(row)


def update_settings(target_url: str, hmac_secret: str, max_retries: int,
                    timeout_seconds: float) -> dict[str, Any]:
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE settings SET target_url=?, hmac_secret=?, max_retries=?, timeout_seconds=? WHERE id=1",
            (target_url, hmac_secret, max_retries, timeout_seconds),
        )
    return get_settings()


# ---------- events ----------

def next_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM events").fetchone()
    return int(row["s"])


def upsert_event(event_id: str, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Insert or replace an event. Returns (event, created)."""
    now = utcnow()
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _lock, get_conn() as conn:
        existing = conn.execute(
            "SELECT seq FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        if existing is None:
            seq = next_seq(conn)
            conn.execute(
                "INSERT INTO events (event_id, seq, payload, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?)",
                (event_id, seq, raw, now, now),
            )
            created = True
        else:
            # new content -> fresh lifecycle
            conn.execute(
                "DELETE FROM attempts WHERE event_id=?", (event_id,)
            )
            conn.execute(
                "UPDATE events SET payload=?, status='pending', updated_at=? WHERE event_id=?",
                (raw, now, event_id),
            )
            seq = existing["seq"]
            created = False
        row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row), created


def get_event(event_id: str) -> dict[str, Any] | None:
    with _lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None


def list_events() -> list[dict[str, Any]]:
    with _lock, get_conn() as conn:
        rows = conn.execute(
            """
            SELECT e.*,
                   (SELECT COUNT(*) FROM attempts a WHERE a.event_id=e.event_id) AS attempt_count,
                   (SELECT MAX(replay_no) FROM attempts a WHERE a.event_id=e.event_id) AS last_replay_no
            FROM events e ORDER BY e.seq ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def events_by_status(statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in statuses)
    with _lock, get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM events WHERE status IN ({placeholders}) ORDER BY seq ASC",
            statuses,
        ).fetchall()
        return [dict(r) for r in rows]


def set_event_status(event_id: str, status: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE events SET status=?, updated_at=? WHERE event_id=?",
            (status, utcnow(), event_id),
        )


def latest_replay_no(conn: sqlite3.Connection, event_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(replay_no), 0) AS n FROM attempts WHERE event_id=?",
        (event_id,),
    ).fetchone()
    return int(row["n"])


def record_attempt(event_id: str, *, attempt_no: int, replay_no: int,
                   idem_key: str, request_at: str, req_headers: dict[str, str],
                   response_at: str | None, http_status: int | None,
                   outcome: str, elapsed_ms: int, resp_excerpt: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            """INSERT INTO attempts
               (event_id, attempt_no, replay_no, idem_key, request_at, response_at,
                http_status, outcome, elapsed_ms, req_headers, resp_excerpt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, attempt_no, replay_no, idem_key, request_at, response_at,
             http_status, outcome, elapsed_ms,
             json.dumps(req_headers, ensure_ascii=False), resp_excerpt[:2000]),
        )
        conn.execute("UPDATE events SET updated_at=? WHERE event_id=?",
                     (utcnow(), event_id))


def timeline_for_event(event_id: str) -> list[dict[str, Any]]:
    with _lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM attempts WHERE event_id=? ORDER BY id ASC", (event_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def timeline(limit: int = 200) -> list[dict[str, Any]]:
    with _lock, get_conn() as conn:
        rows = conn.execute(
            """SELECT a.*, e.seq FROM attempts a
               JOIN events e ON e.event_id = a.event_id
               ORDER BY a.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_history() -> None:
    """Delete all events and attempts. Settings are preserved."""
    with _lock, get_conn() as conn:
        conn.execute("DELETE FROM attempts")
        conn.execute("DELETE FROM events")
