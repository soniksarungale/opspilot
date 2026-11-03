import json
import os
import sqlite3
import threading

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, name TEXT, role TEXT, pw_hash TEXT);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS stores(id INTEGER PRIMARY KEY, name TEXT, city TEXT, launch_date TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS inventory(id INTEGER PRIMARY KEY, sku TEXT, item TEXT, location TEXT, qty INTEGER);
CREATE TABLE IF NOT EXISTS suppliers(id INTEGER PRIMARY KEY, name TEXT, contact TEXT, lead_time_days INTEGER);
CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY, sender TEXT, subject TEXT, body TEXT, received_at TEXT,
    channel TEXT, request_id INTEGER);
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, subject TEXT, sender TEXT, channel TEXT, body TEXT,
    received_at TEXT, status TEXT, facts TEXT, evidence TEXT, flags TEXT, risk TEXT, options TEXT, chosen TEXT,
    confidence REAL, duplicate_of INTEGER, summary TEXT, created_by INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY, request_id INTEGER, role TEXT, amount INTEGER,
    due_at TEXT, status TEXT, decided_by INTEGER, reason TEXT, created_at TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, request_id INTEGER, recipient TEXT, subject TEXT,
    body TEXT, status TEXT, created_at TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, request_id INTEGER, actor TEXT, actor_type TEXT,
    action TEXT, detail TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def path() -> str:
    return os.getenv("DB_PATH", "data/opspilot.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_conn: sqlite3.Connection | None = None
_conn_path: str | None = None


def _get() -> sqlite3.Connection:
    global _conn, _conn_path
    if _conn is None or _conn_path != path():
        if _conn is not None:
            _conn.close()
        os.makedirs(os.path.dirname(path()) or ".", exist_ok=True)
        _conn, _conn_path = connect(), path()
    return _conn


def query(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _get().execute(sql, args).fetchall()]


def one(sql: str, args=()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args=()) -> int:
    with _lock:
        conn = _get()
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(text, default=None):
    if text is None or text == "":
        return default
    return json.loads(text)


def log(request_id: int | None, actor: str, actor_type: str, action: str, detail: dict | None = None) -> None:
    from app import clock
    execute(
        "INSERT INTO audit(request_id, actor, actor_type, action, detail, ts) VALUES (?,?,?,?,?,?)",
        (request_id, actor, actor_type, action, dumps(detail or {}), clock.iso(clock.now())),
    )


def get_setting(key: str, default: str = "") -> str:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))


def init() -> None:
    with _lock:
        conn = _get()
        conn.executescript(SCHEMA)
        conn.commit()
    if not query("SELECT 1 FROM users LIMIT 1"):
        from app import seed
        seed.run()
