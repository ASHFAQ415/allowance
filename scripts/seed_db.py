"""
Seed Script for Allowance v4.2.2.

Initializes the SQLite schema and populates the default tenant, agent, and
financial policy. Safe to run repeatedly.
"""

import argparse
import os
import sys

# Allow `python scripts/seed_db.py` from the repository root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db.bootstrap import seed_defaults
from backend.app.db.connection import create_connection, init_db


def seed_database(db_path: str = "allowance.db", reset: bool = False):
    if reset and os.path.exists(db_path):
        print(f"Resetting database at: {db_path}...")
        try:
            # In-place clean wipe avoids Windows file-lock collision (WinError 32)
            clean_conn = create_connection(db_path)
            try:
                clean_conn.execute("PRAGMA foreign_keys = OFF;")
                triggers = [r[0] for r in clean_conn.execute("SELECT name FROM sqlite_master WHERE type='trigger';").fetchall()]
                for tr in triggers:
                    clean_conn.execute(f"DROP TRIGGER IF EXISTS {tr};")
                tables = [r[0] for r in clean_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';").fetchall()]
                for t in tables:
                    clean_conn.execute(f"DROP TABLE IF EXISTS {t};")
                # Reset autoincrement sequence if present
                has_seq = clean_conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence';").fetchone()
                if has_seq:
                    clean_conn.execute("DELETE FROM sqlite_sequence;")
                clean_conn.commit()
                clean_conn.execute("PRAGMA foreign_keys = ON;")
                print("  In-place database reset successful (zero file lock collision).")
            finally:
                clean_conn.close()
        except Exception as e:
            print(f"  In-place reset fallback: {e}")
            for ext in ["", "-wal", "-shm"]:
                f = f"{db_path}{ext}"
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except OSError:
                        pass

    print(f"Initializing database at: {db_path}...")
    init_db(db_path)

    conn = create_connection(db_path)
    try:
        seed_defaults(conn)
        purchases_cnt = conn.execute("SELECT COUNT(*) FROM purchases;").fetchone()[0]
        ledger_cnt = conn.execute("SELECT COUNT(*) FROM ledger;").fetchone()[0]
        frozen_cnt = conn.execute("SELECT COUNT(*) FROM policy WHERE frozen = 1;").fetchone()[0]
        print(f"Database seeded: {purchases_cnt} purchases, {ledger_cnt} ledger entries, kill switch active: {frozen_cnt > 0}")
    finally:
        conn.close()

    print("Database seeding completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed or reset the Allowance SQLite database.")
    parser.add_argument("--reset", action="store_true", help="Remove existing database files before seeding a clean state.")
    parser.add_argument("--db-path", default="allowance.db", help="Path to SQLite database file (default: allowance.db)")
    args = parser.parse_args()

    seed_database(db_path=args.db_path, reset=args.reset)
