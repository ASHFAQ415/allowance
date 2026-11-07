"""
Seed Script for Allowance v4.2.2.

Initializes the SQLite schema and populates the default tenant, agent, and
financial policy. Safe to run repeatedly.
"""

import os
import sys

# Allow `python scripts/seed_db.py` from the repository root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db.bootstrap import seed_defaults
from backend.app.db.connection import create_connection, init_db


def seed_database(db_path: str = "allowance.db"):
    print(f"Initializing database at: {db_path}...")
    init_db(db_path)

    conn = create_connection(db_path)
    try:
        seed_defaults(conn)
    finally:
        conn.close()

    print("Database seeding completed successfully.")


if __name__ == "__main__":
    seed_database()
