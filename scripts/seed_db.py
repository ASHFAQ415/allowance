"""
Seed Script for Allowance v4.2.2.
Initializes the SQLite schema, populates default tenant, agent, policy, and catalog items.
"""

import json
import os
import sqlite3
import time
from backend.app.db.connection import create_connection, init_db


def seed_database(db_path: str = "allowance.db"):
    print(f"Initializing database at: {db_path}...")
    init_db(db_path)
    conn = create_connection(db_path)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with conn:
        # 1. Seed Tenant & Default Agent
        conn.execute(
            """
            INSERT OR REPLACE INTO agents (id, tenant_id, name, created_at)
            VALUES ('default', 'tenant_demo', 'ShopMind Procurement Agent', ?);
            """,
            (now_iso,)
        )

        # 2. Seed Default Policy ($300 Daily Budget, $150 Per-Tx, $50 Review Threshold, $100 Velocity Cap)
        allowlist = ["merchant_shoes", "merchant_coffee", "merchant_tech"]
        conn.execute(
            """
            INSERT OR REPLACE INTO policy (
                agent_id, tenant_id, version, daily_budget_cents, max_per_tx_cents,
                approval_threshold_cents, velocity_cap_cents, allowlist_json, timezone,
                frozen, updated_at
            ) VALUES (
                'default', 'tenant_demo', 1, 30000, 15000, 5000, 10000, ?, 'UTC', 0, ?
            );
            """,
            (json.dumps(allowlist), now_iso)
        )

        # Record Initial Policy History
        conn.execute(
            """
            INSERT OR REPLACE INTO policy_history (
                id, agent_id, version, policy_json, created_at
            ) VALUES ('ph_init', 'default', 1, ?, ?);
            """,
            (
                json.dumps({
                    "daily_budget_cents": 30000,
                    "max_per_tx_cents": 15000,
                    "approval_threshold_cents": 5000,
                    "velocity_cap_cents": 10000,
                    "allowlist": allowlist,
                    "timezone": "UTC"
                }),
                now_iso
            )
        )

    conn.close()
    print("Database seeding completed successfully.")


if __name__ == "__main__":
    seed_database()

