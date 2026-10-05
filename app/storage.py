"""SQLite persistence for review batches.

A single module-level connection is shared (FastAPI serves sync endpoints
from a thread pool), guarded by a re-entrant lock. Every write happens inside
``BEGIN IMMEDIATE`` so concurrent requests serialise and the service layer can
rely on read-modify-write atomicity for its optimistic version checks. WAL
mode keeps the database file (plus its WAL files) durable across container
restarts when the directory is backed by a mounted volume.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DB_ENV = "PEER_REVIEW_DB"
DEFAULT_DB = "data/peer_review.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id         TEXT PRIMARY KEY,
    version    INTEGER NOT NULL CHECK (version >= 1),
    input      TEXT NOT NULL,
    assignment TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmations (
    batch_id      TEXT NOT NULL,
    manuscript_id TEXT NOT NULL,
    reviewer_id   TEXT NOT NULL,
    PRIMARY KEY (batch_id, manuscript_id, reviewer_id),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


class Rollback(Exception):
    """Raised inside ``write_transaction`` to roll back without signalling an
    error; the rolled-back payload is carried as ``value``."""

    def __init__(self, value):
        super().__init__("rollback")
        self.value = value


def db_path() -> str:
    return os.environ.get(DB_ENV) or DEFAULT_DB


def init(path: str | None = None) -> None:
    """Open (once) the database connection and ensure the schema exists."""
    global _conn
    with _lock:
        if _conn is not None:
            return
        target = path or db_path()
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        _conn = conn


def reset(path: str | None = None) -> None:
    """Close and reopen the database (tests, or a fresh container process)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
    init(path)


def _db() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


@contextmanager
def write_transaction():
    """Serialise mutations and run them inside ``BEGIN IMMEDIATE``."""
    with _lock:
        conn = _db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()


@contextmanager
def read_transaction():
    with _lock:
        yield _db()
