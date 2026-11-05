"""SQLite persistence: accounts, usage records, events and settings."""

import json
import sqlite3
import threading
import time

from . import util


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    realm TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_accounts_channel ON accounts(channel);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    secret TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    expires_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_keys_enabled ON api_keys(enabled);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    api_key_id TEXT NOT NULL DEFAULT '',
    api_key_name TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 200,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_account_ts ON usage(account_id, ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'INFO',
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class Store(object):
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False,
                                  timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            self._ensure_columns("usage", {
                "api_key_id": "TEXT NOT NULL DEFAULT ''",
                "api_key_name": "TEXT NOT NULL DEFAULT ''",
            })
            self._ensure_columns("api_keys", {
                "channel": "TEXT NOT NULL DEFAULT ''",
            })
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage(api_key_id, ts)")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.commit()

    def _ensure_columns(self, table, columns):
        existing = {r["name"] for r in self.db.execute(
            "PRAGMA table_info(%s)" % table)}
        for name, ddl in columns.items():
            if name not in existing:
                self.db.execute("ALTER TABLE %s ADD COLUMN %s %s"
                                % (table, name, ddl))

    # -- accounts ---------------------------------------------------------
    def _row_to_account(self, row):
        try:
            data = json.loads(row["data"] or "{}")
        except Exception:
            data = {}
        return {
            "id": row["id"],
            "channel": row["channel"],
            "name": row["name"],
            "realm": row["realm"],
            "enabled": bool(row["enabled"]),
            "data": data if isinstance(data, dict) else {},
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_accounts(self, channel=None):
        with self.lock:
            if channel:
                rows = self.db.execute(
                    "SELECT * FROM accounts WHERE channel=? ORDER BY created_at",
                    (channel,)).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM accounts ORDER BY created_at").fetchall()
        return [self._row_to_account(r) for r in rows]

    def get_account(self, account_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM accounts WHERE id=?",
                                  (account_id,)).fetchone()
        return self._row_to_account(row) if row else None

    def find_account(self, channel, uid):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM accounts WHERE channel=?", (channel,)).fetchall()
        for row in rows:
            acc = self._row_to_account(row)
            if str(acc["data"].get("uid") or "") == str(uid or ""):
                return acc
        return None

    def save_account(self, account):
        now = util.now()
        account.setdefault("created_at", now)
        account["updated_at"] = now
        with self.lock:
            self.db.execute(
                "INSERT INTO accounts(id, channel, name, realm, enabled, data, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "channel=excluded.channel, name=excluded.name, realm=excluded.realm, "
                "enabled=excluded.enabled, data=excluded.data, updated_at=excluded.updated_at",
                (account["id"], account["channel"], account.get("name") or "",
                 account.get("realm") or "", 1 if account.get("enabled", True) else 0,
                 util.json_dumps(account.get("data") or {}),
                 float(account.get("created_at") or now), float(account["updated_at"])))
            self.db.commit()
        return account

    def delete_account(self, account_id):
        with self.lock:
            cur = self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self.db.commit()
            return cur.rowcount > 0

    # -- api keys ---------------------------------------------------------
    @staticmethod
    def _row_to_api_key(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "secret": row["secret"],
            "channel": row["channel"] or "",
            "enabled": bool(row["enabled"]),
            "expires_at": float(row["expires_at"] or 0),
            "created_at": float(row["created_at"] or 0),
            "last_used_at": float(row["last_used_at"] or 0),
        }

    def list_api_keys(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        return [self._row_to_api_key(r) for r in rows]

    def get_api_key(self, key_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM api_keys WHERE id=?",
                                  (str(key_id or ""),)).fetchone()
        return self._row_to_api_key(row) if row else None

    def insert_api_key(self, key):
        now = util.now()
        with self.lock:
            self.db.execute(
                "INSERT INTO api_keys(id, name, secret, channel, enabled, expires_at, "
                "created_at, last_used_at) VALUES(?,?,?,?,?,?,?,?)",
                (key["id"], key.get("name") or "", key["secret"],
                 key.get("channel") or "",
                 1 if key.get("enabled", True) else 0,
                 float(key.get("expires_at") or 0),
                 float(key.get("created_at") or now),
                 float(key.get("last_used_at") or 0)))
            self.db.commit()
        return self.get_api_key(key["id"])

    def update_api_key(self, key_id, fields):
        allowed = ("name", "secret", "channel", "enabled", "expires_at")
        sets, values = [], []
        for name in allowed:
            if name not in fields:
                continue
            value = fields[name]
            if name == "enabled":
                value = 1 if value else 0
            elif name in ("expires_at",):
                value = float(value or 0)
            elif name in ("name", "channel"):
                value = str(value or "")
            sets.append("%s=?" % name)
            values.append(value)
        if not sets:
            return self.get_api_key(key_id)
        values.append(str(key_id or ""))
        with self.lock:
            self.db.execute("UPDATE api_keys SET %s WHERE id=?"
                            % ", ".join(sets), tuple(values))
            self.db.commit()
        return self.get_api_key(key_id)

    def delete_api_key(self, key_id):
        with self.lock:
            cur = self.db.execute("DELETE FROM api_keys WHERE id=?",
                                  (str(key_id or ""),))
            self.db.commit()
            return cur.rowcount > 0

    def touch_api_key(self, key_id, ts=None):
        with self.lock:
            self.db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?",
                            (float(ts or util.now()), str(key_id or "")))
            self.db.commit()

    def backfill_usage_key(self, key_id, key_name):
        with self.lock:
            self.db.execute(
                "UPDATE usage SET api_key_id=?, api_key_name=? "
                "WHERE api_key_id=''",
                (str(key_id or ""), str(key_name or "")))
            self.db.commit()

    def usage_by_key(self, since=0):
        with self.lock:
            rows = self.db.execute(
                "SELECT api_key_id, COUNT(*) n, "
                "COALESCE(SUM(prompt_tokens),0) p, "
                "COALESCE(SUM(completion_tokens),0) c, "
                "COALESCE(SUM(total_tokens),0) t, "
                "COALESCE(SUM(CASE WHEN status>=400 THEN 1 ELSE 0 END),0) e, "
                "COALESCE(MAX(ts),0) last_ts "
                "FROM usage WHERE ts>=? GROUP BY api_key_id",
                (float(since or 0),)).fetchall()
        return {
            str(r["api_key_id"] or ""): {
                "requests": int(r["n"] or 0),
                "prompt_tokens": int(r["p"] or 0),
                "completion_tokens": int(r["c"] or 0),
                "total_tokens": int(r["t"] or 0),
                "errors": int(r["e"] or 0),
                "last_request_at": float(r["last_ts"] or 0),
            }
            for r in rows
        }

    # -- usage ------------------------------------------------------------
    def add_usage(self, record):
        with self.lock:
            self.db.execute(
                "INSERT INTO usage(ts, channel, account_id, account_name, "
                "api_key_id, api_key_name, model, prompt_tokens, "
                "completion_tokens, total_tokens, status, latency_ms, error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (float(record.get("ts") or util.now()), record.get("channel") or "",
                 record.get("account_id") or "", record.get("account_name") or "",
                 record.get("api_key_id") or "", record.get("api_key_name") or "",
                 record.get("model") or "", int(record.get("prompt_tokens") or 0),
                 int(record.get("completion_tokens") or 0),
                 int(record.get("total_tokens") or 0), int(record.get("status") or 200),
                 int(record.get("latency_ms") or 0), str(record.get("error") or "")[:500]))
            self.db.commit()

    def usage_count_account(self, account_id, since):
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) n FROM usage WHERE account_id=? AND ts>=?",
                (str(account_id or ""), float(since or 0))).fetchone()
        return int(row["n"] or 0)

    def usage_summary(self, since=None):
        since = float(since if since is not None else util.now() - 86400)
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(prompt_tokens),0) p, "
                "COALESCE(SUM(completion_tokens),0) c, COALESCE(SUM(total_tokens),0) t, "
                "COALESCE(SUM(CASE WHEN status>=400 THEN 1 ELSE 0 END),0) e "
                "FROM usage WHERE ts>=?", (since,)).fetchone()
            by = self.db.execute(
                "SELECT channel, COUNT(*) n, COALESCE(SUM(total_tokens),0) t "
                "FROM usage WHERE ts>=? GROUP BY channel", (since,)).fetchall()
        return {
            "requests": int(row["n"] or 0),
            "prompt_tokens": int(row["p"] or 0),
            "completion_tokens": int(row["c"] or 0),
            "total_tokens": int(row["t"] or 0),
            "errors": int(row["e"] or 0),
            "by_channel": {r["channel"]: {"requests": int(r["n"] or 0),
                                          "total_tokens": int(r["t"] or 0)}
                           for r in by},
        }

    def usage_recent(self, limit=50):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM usage ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit or 50), 500)),)).fetchall()
        return [dict(r) for r in rows]

    def prune_usage(self, days=90):
        cutoff = util.now() - max(1, int(days)) * 86400
        with self.lock:
            self.db.execute("DELETE FROM usage WHERE ts<?", (cutoff,))
            self.db.execute("DELETE FROM events WHERE ts<?", (cutoff,))
            self.db.commit()

    # -- events -----------------------------------------------------------
    def add_event(self, level, message):
        with self.lock:
            self.db.execute("INSERT INTO events(ts, level, message) VALUES(?,?,?)",
                            (util.now(), str(level or "INFO")[:16],
                             str(message or "")[:2000]))
            self.db.commit()

    def events(self, limit=100):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit or 100), 500)),)).fetchall()
        return [dict(r) for r in rows]

    # -- settings ---------------------------------------------------------
    def get_setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?",
                                  (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    def set_setting(self, key, value):
        with self.lock:
            self.db.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))
            self.db.commit()

    def close(self):
        with self.lock:
            try:
                self.db.close()
            except Exception:
                pass
