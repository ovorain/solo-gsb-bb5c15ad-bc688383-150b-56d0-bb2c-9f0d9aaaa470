"""SQLite 持久层：配置、事件、投递记录。"""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("WEBHOOK_DB", "/data/webhook.db")

DEFAULT_CONFIG = {
    "target_url": "http://127.0.0.1:8000/demo-target",
    "hmac_secret": "change-me",
    "max_retries": 3,
    "timeout": 3.0,
    "backoff_base": 1.0,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                target_url    TEXT    NOT NULL,
                hmac_secret   TEXT    NOT NULL,
                max_retries   INTEGER NOT NULL,
                timeout       REAL    NOT NULL,
                backoff_base  REAL    NOT NULL,
                updated_at    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id          TEXT PRIMARY KEY,
                seq         INTEGER NOT NULL UNIQUE,
                payload     TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id          TEXT    NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                attempt           INTEGER NOT NULL,
                idempotency_key   TEXT    NOT NULL,
                signature         TEXT    NOT NULL,
                http_status       INTEGER,
                ok                INTEGER NOT NULL DEFAULT 0,
                will_retry        INTEGER NOT NULL DEFAULT 0,
                response_excerpt  TEXT,
                content_type      TEXT,
                error             TEXT,
                duration_ms       INTEGER,
                sent_at           TEXT    NOT NULL
            );
            """
        )
        exists = conn.execute("SELECT 1 FROM config WHERE id = 1").fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO config (id, target_url, hmac_secret, max_retries, "
                "timeout, backoff_base, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
                (
                    DEFAULT_CONFIG["target_url"],
                    DEFAULT_CONFIG["hmac_secret"],
                    DEFAULT_CONFIG["max_retries"],
                    DEFAULT_CONFIG["timeout"],
                    DEFAULT_CONFIG["backoff_base"],
                    now(),
                ),
            )


# ---------- config ----------

def get_config() -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM config WHERE id = 1").fetchone()
        return {
            "target_url": row["target_url"],
            "hmac_secret": row["hmac_secret"],
            "max_retries": row["max_retries"],
            "timeout": row["timeout"],
            "backoff_base": row["backoff_base"],
            "updated_at": row["updated_at"],
        }


def update_config(data: dict) -> dict:
    with get_db() as conn:
        conn.execute(
            "UPDATE config SET target_url = ?, hmac_secret = ?, max_retries = ?, "
            "timeout = ?, backoff_base = ?, updated_at = ? WHERE id = 1",
            (
                data["target_url"],
                data["hmac_secret"],
                data["max_retries"],
                data["timeout"],
                data["backoff_base"],
                now(),
            ),
        )
    return get_config()


# ---------- events ----------

def insert_events(items: list[dict]) -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()
        next_seq = row["m"] + 1
        ts = now()
        for item in items:
            event_id = str(uuid.uuid4())
            payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                "INSERT INTO events (id, seq, payload, status, attempts, "
                "created_at, updated_at) VALUES (?, ?, ?, 'pending', 0, ?, ?)",
                (event_id, next_seq, payload, ts, ts),
            )
            next_seq += 1
        return len(items)


def list_events() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, seq, payload, status, attempts, last_error, "
            "created_at, updated_at FROM events ORDER BY seq ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_event(event_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None


def get_deliveries(event_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM deliveries WHERE event_id = ? ORDER BY id ASC",
            (event_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_due(event_ids: list[str] | None, only_failed: bool) -> list[dict]:
    sql = "SELECT * FROM events"
    params: list = []
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        sql += f" WHERE id IN ({placeholders})"
        params.extend(event_ids)
    elif only_failed:
        sql += " WHERE status = 'failed'"
    else:
        sql += " WHERE status != 'success'"
    sql += " ORDER BY seq ASC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def mark_event(event_id: str, status: str, attempts: int | None = None,
               last_error: str | None = None) -> None:
    with get_db() as conn:
        if attempts is None:
            conn.execute(
                "UPDATE events SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, last_error, now(), event_id),
            )
        else:
            conn.execute(
                "UPDATE events SET status = ?, attempts = ?, last_error = ?, "
                "updated_at = ? WHERE id = ?",
                (status, attempts, last_error, now(), event_id),
            )


def insert_delivery(*, event_id: str, attempt: int, idempotency_key: str,
                    signature: str, http_status: int | None, ok: bool,
                    will_retry: bool, response_excerpt: str | None,
                    content_type: str | None, error: str | None,
                    duration_ms: int) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO deliveries (event_id, attempt, idempotency_key, signature, "
            "http_status, ok, will_retry, response_excerpt, content_type, error, "
            "duration_ms, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id, attempt, idempotency_key, signature, http_status,
                int(ok), int(will_retry), response_excerpt, content_type, error,
                duration_ms, now(),
            ),
        )


def counts() -> dict:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM events GROUP BY status"
        ).fetchall()
        result = {"total": 0, "pending": 0, "running": 0, "success": 0, "failed": 0}
        for r in rows:
            result[r["status"]] = r["c"]
            result["total"] += r["c"]
        return result


def clear_events() -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM events")  # deliveries 通过外键级联删除
