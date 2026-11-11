"""
SQLite Connection Factory and Transaction Management.
Enforces WAL mode, FULL synchronous, foreign keys, busy timeout, and explicit BEGIN IMMEDIATE.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator, Optional

_local = threading.local()

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "allowance.db")
DEFAULT_BUSY_TIMEOUT_MS = int(os.environ.get("BUSY_TIMEOUT_MS", "5000"))


def create_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Creates and configures an isolated SQLite connection with hardened financial pragmas.
    isolation_level=None allows explicit transaction control (BEGIN IMMEDIATE).
    """
    target_path = db_path or os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    conn = sqlite3.connect(
        target_path,
        isolation_level=None,  # Autocommit mode: we control BEGIN / COMMIT explicitly
        timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000.0,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    
    # Configure hardened SQLite Pragmas
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = FULL;")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA foreign_keys = ON;")
    
    return conn


def get_thread_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Returns a thread-local SQLite connection."""
    target_path = db_path or os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    if not hasattr(_local, "conn") or _local.conn is None or getattr(_local, "db_path", None) != target_path:
        if hasattr(_local, "conn") and _local.conn is not None:
            try:
                _local.conn.close()
            except Exception:
                pass
        _local.conn = create_connection(target_path)
        _local.db_path = target_path
    return _local.conn


def close_thread_connection() -> None:
    """Closes the current thread-local connection."""
    if hasattr(_local, "conn") and _local.conn is not None:
        try:
            _local.conn.close()
        finally:
            _local.conn = None
            _local.db_path = None


@contextmanager
def transactional_session(conn: Optional[sqlite3.Connection] = None, db_path: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager for atomic financial operations using BEGIN IMMEDIATE.
    Guarantees that a writer lock is acquired upfront and all mutations commit or rollback together.
    """
    c = conn or get_thread_connection(db_path)
    c.execute("BEGIN IMMEDIATE;")
    try:
        yield c
        c.execute("COMMIT;")
    except Exception:
        c.execute("ROLLBACK;")
        raise


def init_db(db_path: Optional[str] = None, schema_path: Optional[str] = None) -> None:
    """Initializes the database schema and triggers."""
    target_path = db_path or os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    if schema_path is None:
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()
        
    conn = create_connection(target_path)
    try:
        conn.executescript(schema_sql)
        _apply_light_migrations(conn)
    finally:
        conn.close()


def _apply_light_migrations(conn: sqlite3.Connection) -> None:
    """
    Forward-only column additions for databases created before a column existed.
    Full versioned migrations are tracked separately in the implementation plan.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(agent_runs);")}
    if "user_prompt" not in existing:
        conn.execute("ALTER TABLE agent_runs ADD COLUMN user_prompt TEXT;")

