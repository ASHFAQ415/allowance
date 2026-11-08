"""
Default bootstrap data for a fresh Allowance database.

Kept separate from the CLI seed script so application startup and the seed
command use exactly the same defaults.
"""

import json
import time

DEFAULT_AGENT_ID = "default"
DEFAULT_TENANT_ID = "tenant_demo"
DEFAULT_ALLOWLIST = ["merchant_shoes", "merchant_coffee", "merchant_tech"]

DEFAULT_DAILY_BUDGET_CENTS = 30000       # $300.00
DEFAULT_MAX_PER_TX_CENTS = 15000         # $150.00
DEFAULT_APPROVAL_THRESHOLD_CENTS = 5000  # $50.00
DEFAULT_VELOCITY_CAP_CENTS = 10000       # $100.00
DEFAULT_TIMEZONE = "UTC"


def seed_defaults(conn) -> None:
    """Seeds the default tenant, agent, and financial policy if absent."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    conn.execute(
        """
        INSERT OR REPLACE INTO agents (id, tenant_id, name, created_at)
        VALUES (?, ?, 'ShopMind Procurement Agent', ?);
        """,
        (DEFAULT_AGENT_ID, DEFAULT_TENANT_ID, now_iso)
    )

    conn.execute(
        """
        INSERT OR REPLACE INTO policy (
            agent_id, tenant_id, version, daily_budget_cents, max_per_tx_cents,
            approval_threshold_cents, velocity_cap_cents, allowlist_json, timezone,
            frozen, updated_at
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, 0, ?);
        """,
        (
            DEFAULT_AGENT_ID,
            DEFAULT_TENANT_ID,
            DEFAULT_DAILY_BUDGET_CENTS,
            DEFAULT_MAX_PER_TX_CENTS,
            DEFAULT_APPROVAL_THRESHOLD_CENTS,
            DEFAULT_VELOCITY_CAP_CENTS,
            json.dumps(DEFAULT_ALLOWLIST),
            DEFAULT_TIMEZONE,
            now_iso,
        )
    )

    conn.execute(
        """
        INSERT OR REPLACE INTO policy_history (
            id, agent_id, version, policy_json, created_at
        ) VALUES ('ph_init', ?, 1, ?, ?);
        """,
        (
            DEFAULT_AGENT_ID,
            json.dumps({
                "daily_budget_cents": DEFAULT_DAILY_BUDGET_CENTS,
                "max_per_tx_cents": DEFAULT_MAX_PER_TX_CENTS,
                "approval_threshold_cents": DEFAULT_APPROVAL_THRESHOLD_CENTS,
                "velocity_cap_cents": DEFAULT_VELOCITY_CAP_CENTS,
                "allowlist": DEFAULT_ALLOWLIST,
                "timezone": DEFAULT_TIMEZONE,
            }),
            now_iso,
        )
    )
