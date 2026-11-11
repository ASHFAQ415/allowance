"""
Default bootstrap data for a fresh Allowance database.

Kept separate from the CLI seed script so application startup and the seed
command use exactly the same defaults.
"""

import json
import time
from backend.app.auth.credentials import hash_api_key
from backend.app.db.double_entry import seed_chart_of_accounts
from backend.app.paypal.merchants import SUPPORTED_MERCHANTS

DEFAULT_AGENT_ID = "default"
DEFAULT_TENANT_ID = "tenant_demo"
DEFAULT_ALLOWLIST = ["merchant_shoes", "merchant_coffee", "merchant_tech"]

# Demo Organization Profile ($300 daily budget, $50 auto-threshold)
DEFAULT_DAILY_BUDGET_CENTS = 30000       # $300.00
DEFAULT_MAX_PER_TX_CENTS = 15000         # $150.00
DEFAULT_APPROVAL_THRESHOLD_CENTS = 5000  # $50.00
DEFAULT_VELOCITY_CAP_CENTS = 10000       # $100.00
DEFAULT_TIMEZONE = "UTC"

# Personal Edition Profile ($100 daily budget, $30 auto-threshold)
PERSONAL_TENANT_ID = "tenant_personal"
PERSONAL_USER_ID = "user_personal"
PERSONAL_AGENT_ID = "agent_personal"
PERSONAL_DAILY_BUDGET_CENTS = 10000       # $100.00
PERSONAL_MAX_PER_TX_CENTS = 5000          # $50.00
PERSONAL_APPROVAL_THRESHOLD_CENTS = 3000  # $30.00
PERSONAL_VELOCITY_CAP_CENTS = 4500        # $45.00


def seed_defaults(conn) -> None:
    """Seeds the default tenant, agent, and financial policy if absent."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 0. Chart of Accounts & Supported Merchants
    seed_chart_of_accounts(conn)
    for m in SUPPORTED_MERCHANTS.values():
        conn.execute(
            """
            INSERT OR REPLACE INTO merchants (id, name, payee_email, sandbox_merchant_id, category, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (m.id, m.name, m.payee_email, m.sandbox_merchant_id, m.category, m.status, now_iso)
        )

    # 1. Multi-Tenant Organizations & Users
    conn.execute(
        """
        INSERT OR IGNORE INTO tenants (id, name, tier, created_at)
        VALUES (?, 'Demo Organization', 'business', ?);
        """,
        (DEFAULT_TENANT_ID, now_iso)
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO tenants (id, name, tier, created_at)
        VALUES ('tenant_personal', 'Personal Account (Ashfaq)', 'personal', ?);
        """,
        (now_iso,)
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, email, role, created_at)
        VALUES ('user_demo', 'admin@demo.allowance.dev', 'owner', ?);
        """,
        (now_iso,)
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO tenant_memberships (id, tenant_id, user_id, role, created_at)
        VALUES ('mem_demo', ?, 'user_demo', 'owner', ?);
        """,
        (DEFAULT_TENANT_ID, now_iso)
    )

    # 2. Default Agent & Policy
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

    # 3. Personal Edition MVP Profile & Policy
    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, email, role, created_at)
        VALUES (?, 'ashfaq@allowance.dev', 'owner', ?);
        """,
        (PERSONAL_USER_ID, now_iso)
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO tenant_memberships (id, tenant_id, user_id, role, created_at)
        VALUES ('mem_personal', ?, ?, 'owner', ?);
        """,
        (PERSONAL_TENANT_ID, PERSONAL_USER_ID, now_iso)
    )

    conn.execute(
        """
        INSERT OR REPLACE INTO agents (id, tenant_id, name, created_at)
        VALUES (?, ?, 'Personal Shopping Assistant', ?);
        """,
        (PERSONAL_AGENT_ID, PERSONAL_TENANT_ID, now_iso)
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
            PERSONAL_AGENT_ID,
            PERSONAL_TENANT_ID,
            PERSONAL_DAILY_BUDGET_CENTS,
            PERSONAL_MAX_PER_TX_CENTS,
            PERSONAL_APPROVAL_THRESHOLD_CENTS,
            PERSONAL_VELOCITY_CAP_CENTS,
            json.dumps(DEFAULT_ALLOWLIST),
            DEFAULT_TIMEZONE,
            now_iso,
        )
    )

    conn.execute(
        """
        INSERT OR REPLACE INTO policy_history (
            id, agent_id, version, policy_json, created_at
        ) VALUES ('ph_init_personal', ?, 1, ?, ?);
        """,
        (
            PERSONAL_AGENT_ID,
            json.dumps({
                "daily_budget_cents": PERSONAL_DAILY_BUDGET_CENTS,
                "max_per_tx_cents": PERSONAL_MAX_PER_TX_CENTS,
                "approval_threshold_cents": PERSONAL_APPROVAL_THRESHOLD_CENTS,
                "velocity_cap_cents": PERSONAL_VELOCITY_CAP_CENTS,
                "allowlist": DEFAULT_ALLOWLIST,
                "timezone": DEFAULT_TIMEZONE,
            }),
            now_iso,
        )
    )

    # 4. Seed Deterministic Postman & Dev API Key
    demo_key = "ak_test_dev_token_allowance_2026_xyz"
    demo_hash = hash_api_key(demo_key)
    conn.execute(
        """
        INSERT OR REPLACE INTO agent_credentials (
            id, tenant_id, agent_id, name, key_hash, key_prefix,
            scopes_json, expires_at, revoked_at, created_at
        ) VALUES ('cred_demo_postman', ?, ?, 'Postman & Dev Test Key', ?, 'ak_test_dev...', ?, NULL, NULL, ?);
        """,
        (DEFAULT_TENANT_ID, DEFAULT_AGENT_ID, demo_hash, json.dumps(["propose", "read", "admin"]), now_iso)
    )
