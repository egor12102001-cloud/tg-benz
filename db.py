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
        # 1. Create tables/columns as they existed in the very first schema version.
        #    New columns are added via migration below, *before* any index that
        #    references them — SQLite indexes can't reference nonexistent columns.
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
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                chat_id       INTEGER NOT NULL,
                chat_type     TEXT NOT NULL,
                city          TEXT NOT NULL,
                success       INTEGER NOT NULL DEFAULT 1,
                error         TEXT,
                stations      INTEGER,
                created_at    TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS queries_user    ON queries(user_id);
            CREATE INDEX IF NOT EXISTS queries_created ON queries(created_at);
        """)

        # 2. Migrate older DBs to the current column set.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
        if "last_city" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN last_city TEXT")

        cols = {r["name"] for r in con.execute("PRAGMA table_info(queries)")}
        if "city_norm" not in cols:
            con.execute("ALTER TABLE queries ADD COLUMN city_norm TEXT NOT NULL DEFAULT ''")

        # 3. Indexes that depend on migrated columns.
        con.execute("CREATE INDEX IF NOT EXISTS queries_city_norm ON queries(city_norm)")

        # 4. (Re)normalize city_norm in Python: SQLite's LOWER() only lowercases
        #    ASCII, so Cyrillic city names like "Александров" were left
        #    capitalized and split into separate groups from lowercase entries.
        from scraper import normalize_city
        rows = con.execute("SELECT id, city, city_norm FROM queries").fetchall()
        for row in rows:
            correct = normalize_city(row["city"])
            if row["city_norm"] != correct:
                con.execute("UPDATE queries SET city_norm = ? WHERE id = ?", (correct, row["id"]))


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


def set_last_city(user_id: int, city: str) -> None:
    with _connect() as con:
        con.execute("UPDATE users SET last_city = ? WHERE user_id = ?", (city, user_id))


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


def list_all_users() -> list[dict]:
    with _connect() as con:
        rows = con.execute("SELECT * FROM users ORDER BY first_seen").fetchall()
        return [dict(r) for r in rows]


# ─── roles ───────────────────────────────────────────────────────────────────

def set_role(user_id: int, role: str) -> bool:
    with _connect() as con:
        cur = con.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
        return cur.rowcount > 0


def get_role(user_id: int) -> str:
    with _connect() as con:
        row = con.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
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
    city_norm: str,
    success: bool,
    error: str | None = None,
    stations: int | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as con:
        con.execute("""
            INSERT INTO queries
                (user_id, chat_id, chat_type, city, city_norm, success, error, stations, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, chat_id, chat_type, city, city_norm, int(success), error, stations, now))


def clear_user_history(user_id: int) -> int:
    """Delete all stored queries for a user. Returns number of rows removed."""
    with _connect() as con:
        cur = con.execute("DELETE FROM queries WHERE user_id = ?", (user_id,))
        con.execute("UPDATE users SET last_city = NULL WHERE user_id = ?", (user_id,))
        return cur.rowcount


def get_user_top_cities(user_id: int, limit: int = 10) -> list[dict]:
    """Cities a given user has queried most often, grouped by normalized name."""
    with _connect() as con:
        rows = con.execute("""
            SELECT city_norm,
                   (SELECT city FROM queries q2
                    WHERE q2.city_norm = q.city_norm AND q2.user_id = ?
                    GROUP BY city ORDER BY COUNT(*) DESC LIMIT 1) AS city,
                   COUNT(*) as cnt,
                   MAX(created_at) as last_at
            FROM queries q
            WHERE user_id = ? AND city_norm != ''
            GROUP BY city_norm ORDER BY cnt DESC LIMIT ?
        """, (user_id, user_id, limit)).fetchall()
        return [dict(r) for r in rows]


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

        # Group by normalized city, but show the most common original spelling
        top_cities = con.execute("""
            SELECT city_norm,
                   (SELECT city FROM queries q2
                    WHERE q2.city_norm = q.city_norm
                    GROUP BY city ORDER BY COUNT(*) DESC LIMIT 1) AS city,
                   COUNT(*) as cnt
            FROM queries q
            WHERE city_norm != ''
            GROUP BY city_norm ORDER BY cnt DESC LIMIT 10
        """).fetchall()

        all_users = con.execute("""
            SELECT u.user_id, u.first_name, u.last_name, u.username, u.role,
                   u.first_seen, u.last_seen,
                   COUNT(q.id) as total_queries,
                   SUM(CASE WHEN q.success=1 THEN 1 ELSE 0 END) as ok,
                   SUM(CASE WHEN q.success=0 THEN 1 ELSE 0 END) as err,
                   MAX(q.created_at) as last_query_at
            FROM users u
            LEFT JOIN queries q ON q.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY total_queries DESC
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
    }
