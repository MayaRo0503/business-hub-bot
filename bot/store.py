"""Storage for sessions, messages and the knowledge base.

SQLite for local runs and tests; Postgres (DATABASE_URL=postgres://...) in production, because
Render's free web service has no persistent disk. Only portable SQL is used.
Logs follow the israeli-chatbot-analytics "Standard conversation log schema".
"""
import hashlib
import hmac
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY, channel TEXT, user_key TEXT, contact TEXT,
        started_at TEXT, last_at TEXT, state TEXT, fallback_count INTEGER,
        handoff_until TEXT, handoff_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, session_id TEXT, ts TEXT, sender TEXT, text TEXT,
        intent TEXT, confidence REAL, response_ms INTEGER)""",
    "CREATE INDEX IF NOT EXISTS messages_session ON messages (session_id)",
    "CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_key, last_at)",
    "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS seen (msg_id TEXT PRIMARY KEY, ts TEXT)",
]

OUTCOME_RULE = "outcome-v1: escalated if a handoff happened; resolved if the last user turn got an answer; abandoned otherwise"


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, url: str, salt: str):
        self.salt = salt.encode()
        self.lock = threading.Lock()
        self.pg = url.startswith(("postgres://", "postgresql://"))
        if self.pg:
            import psycopg  # only needed in production
            self._connect = lambda: psycopg.connect(url, autocommit=True)
        else:
            path = url.removeprefix("sqlite:///") or ":memory:"
            self._connect = lambda: sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn = self._connect()
        for stmt in SCHEMA:
            self._exec(stmt)

    # -- low level --
    def _exec(self, sql: str, params=(), fetch=False):
        if self.pg:
            sql = sql.replace("?", "%s")
        with self.lock:
            for attempt in (1, 2):
                try:
                    cur = self.conn.cursor()
                    cur.execute(sql, params)
                    return cur.fetchall() if fetch else cur.rowcount
                except Exception:
                    if not self.pg or attempt == 2:
                        raise
                    self.conn = self._connect()  # Postgres dropped the idle connection

    def user_key(self, channel: str, user_id: str) -> str:
        return hmac.new(self.salt, f"{channel}:{user_id}".encode(), hashlib.sha256).hexdigest()[:20]

    # -- dedup (Meta and Telegram retry deliveries) --
    def first_time(self, msg_id: str) -> bool:
        if not msg_id:
            return True
        inserted = self._exec(
            "INSERT INTO seen (msg_id, ts) VALUES (?, ?) ON CONFLICT (msg_id) DO NOTHING", (msg_id, iso(now()))
        )
        return inserted == 1

    def prune_seen(self):
        self._exec("DELETE FROM seen WHERE ts < ?", (iso(now() - timedelta(days=2)),))

    # -- knowledge --
    def get_kv(self, k: str):
        rows = self._exec("SELECT v FROM kv WHERE k = ?", (k,), fetch=True)
        return json.loads(rows[0][0]) if rows else None

    def set_kv(self, k: str, v):
        self._exec(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
            (k, json.dumps(v, ensure_ascii=False)),
        )

    # -- sessions --
    def session_for(self, channel: str, user_id: str, timeout_min: int) -> dict:
        key = self.user_key(channel, user_id)
        cutoff = iso(now() - timedelta(minutes=timeout_min))
        rows = self._exec(
            "SELECT session_id, state, fallback_count, handoff_until, started_at FROM sessions "
            "WHERE user_key = ? AND channel = ? AND last_at >= ? ORDER BY last_at DESC LIMIT 1",
            (key, channel, cutoff), fetch=True,
        )
        # handoff silence can outlive the inactivity cut, so check it separately
        silence = self._exec(
            "SELECT handoff_until FROM sessions WHERE user_key = ? AND channel = ? AND handoff_until > ? "
            "ORDER BY handoff_until DESC LIMIT 1",
            (key, channel, iso(now())), fetch=True,
        )
        handoff_until = silence[0][0] if silence else None
        if rows:
            sid, state, fb, hu, started = rows[0]
            return {"session_id": sid, "state": state, "fallback_count": fb or 0,
                    "handoff_until": handoff_until or hu, "new": False, "channel": channel}
        sid = str(uuid.uuid4())
        t = iso(now())
        self._exec(
            "INSERT INTO sessions (session_id, channel, user_key, started_at, last_at, state, fallback_count) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (sid, channel, key, t, t, "start"),
        )
        return {"session_id": sid, "state": "start", "fallback_count": 0,
                "handoff_until": handoff_until, "new": True, "channel": channel}

    def update_session(self, s: dict, **fields):
        fields["last_at"] = iso(now())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE sessions SET {cols} WHERE session_id = ?", (*fields.values(), s["session_id"]))
        s.update(fields)

    def add_message(self, session_id: str, sender: str, text: str, intent=None, confidence=None, response_ms=None):
        self._exec(
            "INSERT INTO messages (id, session_id, ts, sender, text, intent, confidence, response_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), session_id, now().isoformat(timespec="microseconds"), sender, text, intent, confidence,
             response_ms),
        )

    def recent_messages(self, session_id: str, limit: int = 8) -> list[dict]:
        rows = self._exec(
            "SELECT sender, text FROM messages WHERE session_id = ? ORDER BY ts DESC LIMIT ?",
            (session_id, limit), fetch=True,
        )
        return [{"sender": r[0], "text": r[1]} for r in reversed(rows)]

    # -- export (israeli-chatbot-analytics schema) --
    def export(self, since: str, timeout_min: int) -> list[dict]:
        """Closed sessions that ended at or after `since` (a session is closed after timeout_min of silence)."""
        closed_before = iso(now() - timedelta(minutes=timeout_min))
        sessions = self._exec(
            "SELECT session_id, channel, user_key, started_at, last_at, handoff_at FROM sessions "
            "WHERE last_at >= ? AND last_at <= ? ORDER BY last_at",
            (since, closed_before), fetch=True,
        )
        out = []
        for sid, channel, ukey, started, last, handoff_at in sessions:
            msgs = self._exec(
                "SELECT ts, sender, text, intent, confidence, response_ms FROM messages "
                "WHERE session_id = ? ORDER BY ts", (sid,), fetch=True,
            )
            messages = [{"timestamp": m[0], "sender": m[1], "text": m[2], "intent": m[3],
                         "intent_confidence": m[4], "entities": [], "response_time_ms": m[5]} for m in msgs]
            out.append({
                "session_id": sid, "user_id": ukey, "channel": channel, "language": "he",
                "started_at": started, "ended_at": last, "messages": messages,
                "outcome": derive_outcome(messages, handoff_at),
                "satisfaction_score": None,
                "metadata": {"bot_version": "1.0.0", "outcome_rule": "outcome-v1"},
            })
        return out

    def handoffs(self, since: str) -> list[dict]:
        rows = self._exec(
            "SELECT session_id, channel, contact, handoff_at FROM sessions WHERE handoff_at >= ? ORDER BY handoff_at DESC",
            (since,), fetch=True,
        )
        result = []
        for sid, channel, contact, at in rows:
            last = self._exec(
                "SELECT text FROM messages WHERE session_id = ? AND sender = 'user' ORDER BY ts DESC LIMIT 3",
                (sid,), fetch=True,
            )
            result.append({"channel": channel, "contact": contact, "at": at,
                           "lastMessages": [r[0] for r in reversed(last)]})
        return result


ANSWER_INTENTS = ("faq:", "smart_answer", "closing")


def derive_outcome(messages: list[dict], handoff_at) -> str:
    """Outcome rule v1 (see OUTCOME_RULE). Derived from events, never from the bot's own claim."""
    if handoff_at:
        return "escalated"
    last_user = max((i for i, m in enumerate(messages) if m["sender"] == "user"), default=None)
    if last_user is None:
        return "unknown"
    reply = next((m for m in messages[last_user + 1:] if m["sender"] == "bot"), None)
    if reply and (reply.get("intent") or "").startswith(ANSWER_INTENTS):
        return "resolved"
    return "abandoned"
