"""SQLite 持久化层(stdlib only)。

- 单连接 + RLock,事务通过 tx() 显式管理(BEGIN IMMEDIATE)。
- JSON 列以 TEXT 存储,引擎层负责编解码。
"""

import contextlib
import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS baselines(
  id TEXT PRIMARY KEY,
  seq INTEGER UNIQUE,
  parent_id TEXT,
  constraint_epoch INTEGER,
  modules TEXT,
  constraints TEXT,
  matrix TEXT,
  created_from_eco TEXT,
  kind TEXT,
  note TEXT,
  status TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS ecos(
  id TEXT PRIMARY KEY,
  title TEXT,
  status TEXT,
  base_baseline_id TEXT,
  base_seq INTEGER,
  constraint_epoch INTEGER,
  snapshot_hash TEXT,
  changes TEXT,
  affected_modules TEXT,
  matrix TEXT,
  conflict_policy TEXT,
  blocked_by TEXT,
  dispatch_gen INTEGER DEFAULT 0,
  created_by TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS check_runs(
  id TEXT PRIMARY KEY,
  eco_id TEXT,
  check_type TEXT,
  name TEXT,
  status TEXT,
  snapshot_hash TEXT,
  base_baseline_id TEXT,
  constraint_epoch INTEGER,
  token TEXT,
  generation INTEGER,
  depends_on TEXT,
  result TEXT,
  dispatched_at TEXT,
  completed_at TEXT,
  invalidated_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_eco ON check_runs(eco_id);
CREATE TABLE IF NOT EXISTS signoffs(
  id TEXT PRIMARY KEY,
  eco_id TEXT,
  role TEXT,
  user TEXT,
  snapshot_hash TEXT,
  created_at TEXT,
  revoked INTEGER DEFAULT 0,
  revoke_reason TEXT
);
CREATE TABLE IF NOT EXISTS conflicts(
  id TEXT PRIMARY KEY,
  eco_a TEXT,
  eco_b TEXT,
  modules TEXT,
  status TEXT,
  resolution TEXT,
  created_at TEXT,
  resolved_at TEXT,
  resolved_by TEXT
);
CREATE TABLE IF NOT EXISTS release_journal(
  eco_id TEXT PRIMARY KEY,
  target_baseline_id TEXT,
  state TEXT,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  kind TEXT,
  entity_id TEXT,
  payload TEXT
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # ---- generic helpers ----
    def insert(self, table: str, row: dict):
        cols = ", ".join(row.keys())
        ph = ", ".join("?" for _ in row)
        with self.lock:
            self.conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({ph})", tuple(row.values())
            )

    def update(self, table: str, row_id: str, fields: dict, id_col: str = "id"):
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.conn.execute(
                f"UPDATE {table} SET {sets} WHERE {id_col}=?",
                tuple(fields.values()) + (row_id,),
            )

    def get(self, table: str, row_id: str, id_col: str = "id"):
        with self.lock:
            cur = self.conn.execute(
                f"SELECT * FROM {table} WHERE {id_col}=?", (row_id,)
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def find(self, table: str, where: str = "", params: tuple = (), order: str = ""):
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        with self.lock:
            cur = self.conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def one(self, table: str, where: str = "", params: tuple = (), order: str = ""):
        rows = self.find(table, where, params, order)
        return rows[0] if rows else None

    # ---- meta ----
    def meta_get(self, key: str, default=None):
        row = self.get("meta", key, id_col="key")
        return row["value"] if row else default

    def meta_set(self, key: str, value: str):
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def close(self):
        with self.lock:
            self.conn.close()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text, default=None):
    if text is None:
        return default
    return json.loads(text)
