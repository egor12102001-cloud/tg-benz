"""
SQLite storage for users, roles, and query statistics.
"""
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
                last_name   TEXT,
                role        TEXT NOT NULL DEFAULT 'user',
                first_seen  TEXT NOT NULL,
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

            CREATE INDEX IF NOT EXISTS queries_user    ON queries(user_id);
            CREATE INDEX IF NOT EXISTS queries_city    ON queries(city);
            CREATE INDEX IF NOT EXISTS queries_created ON queries(created_at);
        """)


# ─── users ───────────────────────────────────────────────────────────────────

def upsert_user(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as con:
        con.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                last_seen  = excluded.last_seen
        """, (user_id, username, first_name, last_name, now, now))


def get_user(user_id: int) -> dict | None:
    with _connect() as con:
        row = con.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username.lstrip("@"),)
        ).fetchone()
        return dict(row) if row else None


# ─── roles ───────────────────────────────────────────────────────────────────

def set_role(user_id: int, role: str) -> bool:
    """Set role for existing user. Returns False if user not found."""
    with _connect() as con:
        cur = con.execute(
            "UPDATE users SET role = ? WHERE user_id = ?", (role, user_id)
        )
        return cur.rowcount > 0


def get_role(user_id: int) -> str:
    with _connect() as con:
        row = con.execute(
            "SELECT role FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["role"] if row else "user"


def is_admin(user_id: int) -> bool:
    return get_role(user_id) == "admin"


def list_admins() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM users WHERE role = 'admin' ORDER BY first_seen"
        ).fetchall()
        return [dict(r) for r in rows]


# ─── queries ─────────────────────────────────────────────────────────────────

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


# ─── stats ───────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    with _connect() as con:
        total_users   = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_queries = con.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
        success_count = con.execute("SELECT COUNT(*) FROM queries WHERE success=1").fetchone()[0]
        error_count   = con.execute("SELECT COUNT(*) FROM queries WHERE success=0").fetchone()[0]

        today = datetime.now(timezone.utc).date().isoformat()
        today_queries = con.execute(
            "SELECT COUNT(*) FROM queries WHERE created_at >= ?", (today,)
        ).fetchone()[0]
        today_users = con.execute(
            "SELECT COUNT(DISTINCT user_id) FROM queries WHERE created_at >= ?", (today,)
        ).fetchone()[0]

        top_cities = con.execute("""
            SELECT city, COUNT(*) as cnt
            FROM queries
            GROUP BY city ORDER BY cnt DESC LIMIT 10
        """).fetchall()

        all_users = con.execute("""
            SELECT u.user_id, u.first_name, u.last_name, u.username, u.role,
                   u.first_seen, u.last_seen,
                   COUNT(q.id) as total_queries,
                   SUM(CASE WHEN q.success=1 THEN 1 ELSE 0 END) as ok,
                   SUM(CASE WHEN q.success=0 THEN 1 ELSE 0 END) as err
            FROM users u
            LEFT JOIN queries q ON q.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY total_queries DESC
        """).fetchall()

        recent = con.execute("""
            SELECT u.first_name, u.last_name, u.username, q.city,
                   q.chat_type, q.success, q.stations, q.created_at
            FROM queries q
            LEFT JOIN users u ON u.user_id = q.user_id
            ORDER BY q.created_at DESC
            LIMIT 15
        """).fetchall()

    return {
        "total_users":   total_users,
        "total_queries": total_queries,
        "success_count": success_count,
        "error_count":   error_count,
        "today_queries": today_queries,
        "today_users":   today_users,
        "top_cities":    [dict(r) for r in top_cities],
        "all_users":     [dict(r) for r in all_users],
        "recent":        [dict(r) for r in recent],
    }
