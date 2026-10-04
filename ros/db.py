"""SQLite persistence: versioned forward-only migrations, WAL, foreign keys.

SQLite is the single system of record. Transactions are short and never wrap
network or model calls.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('research','monitor','digest')),
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT,
    watch_id INTEGER REFERENCES watches(id),
    config_json TEXT NOT NULL DEFAULT '{}',
    budget_json TEXT NOT NULL DEFAULT '{}',
    plan_json TEXT,
    plan_version INTEGER NOT NULL DEFAULT 0,
    elapsed_s REAL NOT NULL DEFAULT 0,
    outcome_note TEXT,
    report_md TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE run_log (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);

CREATE TABLE plan_versions (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    round_n INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, version)
);

CREATE TABLE rounds (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    n INTEGER NOT NULL,
    stage TEXT NOT NULL,
    analysis_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (run_id, n)
);

CREATE TABLE queries (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    round_n INTEGER NOT NULL,
    text TEXT NOT NULL,
    norm TEXT NOT NULL,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    results INTEGER,
    UNIQUE (run_id, norm)
);

CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    locator TEXT NOT NULL,
    title TEXT,
    access_mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (kind, locator)
);

CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    url TEXT,
    canonical_url TEXT,
    title TEXT,
    author TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    content_hash TEXT,
    text TEXT,
    meta_json TEXT,
    duplicate_of INTEGER REFERENCES items(id),
    UNIQUE (source_id, external_id)
);
CREATE INDEX items_canonical ON items(canonical_url);
CREATE INDEX items_hash ON items(content_hash);

CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    text TEXT,
    UNIQUE (item_id, content_hash)
);

CREATE TABLE run_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    round_n INTEGER NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT,
    snippet TEXT,
    query_id INTEGER REFERENCES queries(id),
    item_id INTEGER REFERENCES items(id),
    status TEXT NOT NULL,
    reason TEXT,
    UNIQUE (run_id, canonical_url)
);

CREATE TABLE claims (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    round_n INTEGER,
    text TEXT NOT NULL,
    claim_type TEXT NOT NULL CHECK (claim_type IN ('fact','inference','opinion','rumor','prediction')),
    status TEXT NOT NULL DEFAULT 'unverified' CHECK (status IN ('verified','unverified','disputed','superseded')),
    quote TEXT,
    subtopic TEXT,
    confidence REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE findings (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    round_n INTEGER NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    confidence TEXT NOT NULL,
    claim_ids_json TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'finding' CHECK (kind IN ('finding','contradiction','gap','discovery')),
    superseded_by INTEGER REFERENCES findings(id),
    created_at TEXT NOT NULL
);

CREATE TABLE usage (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    purpose TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE errors (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    watch_id INTEGER REFERENCES watches(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT,
    message TEXT NOT NULL,
    impact TEXT
);

CREATE TABLE watches (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    spec_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT,
    next_digest_at TEXT,
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE watch_versions (
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (watch_id, version)
);

CREATE TABLE cursors (
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    cursor_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (watch_id, source_id)
);

CREATE TABLE watch_items (
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    run_id INTEGER REFERENCES runs(id),
    change TEXT NOT NULL CHECK (change IN ('new','modified')),
    analysis TEXT NOT NULL DEFAULT 'pending' CHECK (analysis IN ('pending','done','skipped')),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (watch_id, item_id, observed_at)
);

CREATE TABLE clusters (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES runs(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    change TEXT NOT NULL,
    relevance REAL NOT NULL,
    importance REAL NOT NULL,
    claim_type TEXT NOT NULL,
    labels_json TEXT NOT NULL DEFAULT '[]',
    why TEXT,
    cluster_id INTEGER REFERENCES clusters(id),
    digest_id INTEGER REFERENCES digests(id),
    alerted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE digests (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT NOT NULL,
    summary TEXT,
    markdown TEXT NOT NULL,
    event_count INTEGER NOT NULL
);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER REFERENCES watches(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('alert','digest','attention')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    read INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE VIRTUAL TABLE items_fts USING fts5(title, text, content='items', content_rowid='id');
CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, text) VALUES ('delete', old.id, old.title, old.text);
END;
CREATE TRIGGER items_au AFTER UPDATE OF title, text ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, text) VALUES ('delete', old.id, old.title, old.text);
  INSERT INTO items_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;

CREATE VIRTUAL TABLE claims_fts USING fts5(text, content='claims', content_rowid='id');
CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
  INSERT INTO claims_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    return default if value is None else json.loads(value)


def text_hash(text: str) -> str:
    norm = " ".join(text.split()).lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._depth = 0
        self.migrate()

    # -- schema -------------------------------------------------------------
    def migrate(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
        applied = {r["version"]: r["checksum"] for r in self.conn.execute("SELECT * FROM schema_migrations")}
        known = {v for v, _ in MIGRATIONS}
        unknown = set(applied) - known
        if unknown:
            raise RuntimeError(f"database schema {max(unknown)} is newer than this ROS version; refusing to open")
        for version, sql in MIGRATIONS:
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise RuntimeError(f"migration {version} checksum drift; refusing to open")
                continue
            with self.tx():
                for statement in _split_sql(sql):
                    self.conn.execute(statement)
                self.conn.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (version, checksum, now_iso()))

    # -- transactions -------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
        return int(cur.lastrowid)

    # -- shared helpers -----------------------------------------------------
    def log(self, run_id: int, kind: str, message: str, data: Any = None) -> None:
        self.insert("run_log", {"run_id": run_id, "ts": now_iso(), "kind": kind, "message": message,
                                "data_json": dumps(data) if data is not None else None})

    def record_error(self, *, kind: str, message: str, run_id: int | None = None, watch_id: int | None = None,
                     subject: str | None = None, impact: str | None = None) -> None:
        self.insert("errors", {"run_id": run_id, "watch_id": watch_id, "ts": now_iso(), "kind": kind,
                               "subject": subject, "message": message[:2000], "impact": impact})

    def upsert_source(self, kind: str, locator: str, access_mode: str, title: str | None = None) -> int:
        row = self.one("SELECT id FROM sources WHERE kind=? AND locator=?", (kind, locator))
        if row:
            if title:
                self.execute("UPDATE sources SET title=COALESCE(title, ?) WHERE id=?", (title, row["id"]))
            return int(row["id"])
        return self.insert("sources", {"kind": kind, "locator": locator, "title": title,
                                       "access_mode": access_mode, "created_at": now_iso()})

    def acquire_lock(self, name: str, owner: str, ttl: float) -> bool:
        now = time.time()
        with self.tx():
            row = self.one("SELECT owner, expires_at FROM locks WHERE name=?", (name,))
            if row and row["owner"] != owner and row["expires_at"] > now:
                return False
            self.execute("INSERT OR REPLACE INTO locks VALUES (?,?,?)", (name, owner, now + ttl))
            return True

    def release_lock(self, name: str, owner: str) -> None:
        self.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))

    def backup(self, dest: str | Path) -> Path:
        """Consistent online backup via SQLite's backup API, then integrity check."""
        dest = Path(dest).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        with self._lock:
            self.conn.backup(target)
        ok = target.execute("PRAGMA integrity_check").fetchone()[0]
        target.close()
        if ok != "ok":
            raise RuntimeError(f"backup integrity check failed: {ok}")
        return dest

    def close(self) -> None:
        self.conn.close()


def _split_sql(sql: str) -> list[str]:
    """Split a migration script on ';' while keeping trigger bodies intact."""
    statements, buf, in_trigger = [], [], False
    for line in sql.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buf.append(line)
        if stripped.upper().startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if stripped.upper() == "END;":
                statements.append("\n".join(buf))
                buf, in_trigger = [], False
        elif stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements
