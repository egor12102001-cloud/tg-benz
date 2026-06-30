"""
SQLite storage for user and query statistics.
"""
import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "stats.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    with _connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_seen   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS queries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                chat_type   TEXT NOT NULL,
                city        TEXT NOT NULL,
                success     INTEGER NOT NULL DEFAULT 1,
                error       TEXT,
                stations    INTEGER,
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS queries_user ON queries(user_id);
            CREATE INDEX IF NOT EXISTS queries_city ON queries(city);
            CREATE INDEX IF NOT EXISTS queries_created ON queries(created_at);
        """)


def upsert_user(user_id: int, username: str | None, first_name: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as con:
        con.execute("""
            INSERT INTO users (user_id, username, first_name, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_seen  = excluded.last_seen
        """, (user_id, username, first_name, now))


def log_query(
    user_id: int,
    chat_id: int,
    chat_type: str,
    city: str,
    success: bool,
    error: str | None = None,
    stations: int | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as con:
        con.execute("""
            INSERT INTO queries
                (user_id, chat_id, chat_type, city, success, error, stations, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, chat_id, chat_type, city, int(success), error, stations, now))


def get_stats() -> dict:
    with _connect() as con:
        total_users    = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_queries  = con.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
        success_count  = con.execute("SELECT COUNT(*) FROM queries WHERE success=1").fetchone()[0]
        error_count    = con.execute("SELECT COUNT(*) FROM queries WHERE success=0").fetchone()[0]

        today = datetime.now(timezone.utc).date().isoformat()
        today_queries  = con.execute(
            "SELECT COUNT(*) FROM queries WHERE created_at >= ?", (today,)
        ).fetchone()[0]
        today_users    = con.execute(
            "SELECT COUNT(DISTINCT user_id) FROM queries WHERE created_at >= ?", (today,)
        ).fetchone()[0]

        top_cities = con.execute("""
            SELECT city, COUNT(*) as cnt
            FROM queries
            GROUP BY city
            ORDER BY cnt DESC
            LIMIT 10
        """).fetchall()

        top_users = con.execute("""
            SELECT u.first_name, u.username, COUNT(q.id) as cnt
            FROM queries q
            LEFT JOIN users u ON u.user_id = q.user_id
            GROUP BY q.user_id
            ORDER BY cnt DESC
            LIMIT 5
        """).fetchall()

        recent = con.execute("""
            SELECT u.first_name, u.username, q.city, q.chat_type,
                   q.success, q.created_at
            FROM queries q
            LEFT JOIN users u ON u.user_id = q.user_id
            ORDER BY q.created_at DESC
            LIMIT 10
        """).fetchall()

    return {
        "total_users":   total_users,
        "total_queries": total_queries,
        "success_count": success_count,
        "error_count":   error_count,
        "today_queries": today_queries,
        "today_users":   today_users,
        "top_cities":    [dict(r) for r in top_cities],
        "top_users":     [dict(r) for r in top_users],
        "recent":        [dict(r) for r in recent],
    }
