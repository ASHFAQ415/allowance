"""
API Key & Scoped Credential Management for Allowance Platform.
Enforces Invariant 6:
- Only hashed verifiers stored in SQLite (SHA-256). Raw secrets displayed only once.
- Scoped permissions: 'propose', 'read', 'admin'.
- Tenant and agent identity bound to credential, never supplied by client body.
- Constant-time verification using hmac.compare_digest.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class CredentialContext:
    tenant_id: str
    agent_id: str
    credential_id: str
    name: str
    scopes: List[str]
    expires_at: Optional[float]


def hash_api_key(raw_key: str) -> str:
    """Computes SHA-256 hex digest of the raw API key."""
    return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()


def generate_api_key(prefix: str = "ak_live") -> Tuple[str, str, str]:
    """
    Generates a secure, cryptographically random API key.
    Format: ak_live_<32 hex chars>
    Returns: (raw_key, key_hash, key_prefix)
    """
    token_entropy = secrets.token_hex(24)
    raw_key = f"{prefix}_{token_entropy}"
    key_hash = hash_api_key(raw_key)
    key_prefix = raw_key[:12] + "..."
    return raw_key, key_hash, key_prefix


def create_agent_credential(
    conn: sqlite3.Connection,
    tenant_id: str,
    agent_id: str,
    name: str = "Default Agent Key",
    scopes: Optional[List[str]] = None,
    expires_in_days: int = 90
) -> Tuple[str, str]:
    """
    Creates and records a new scoped API key for an agent.
    Returns: (credential_id, raw_key). The raw_key must be presented to user immediately.
    """
    scopes = scopes or ["propose", "read"]
    raw_key, key_hash, key_prefix = generate_api_key(prefix="ak_live")
    cred_id = str(uuid.uuid4())
    now = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    expires_at = now + (expires_in_days * 86400) if expires_in_days > 0 else None

    conn.execute(
        """
        INSERT INTO agent_credentials (
            id, tenant_id, agent_id, name, key_hash, key_prefix,
            scopes_json, expires_at, revoked_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?);
        """,
        (
            cred_id,
            tenant_id,
            agent_id,
            name,
            key_hash,
            key_prefix,
            json.dumps(scopes),
            expires_at,
            now_iso,
        )
    )
    return cred_id, raw_key


def authenticate_api_key(
    conn: sqlite3.Connection,
    raw_key: str,
    required_scope: Optional[str] = None
) -> Optional[CredentialContext]:
    """
    Authenticates a raw API key against stored SHA-256 verifiers.
    Returns CredentialContext if valid, unexpired, and not revoked; otherwise None.
    """
    if not raw_key or not isinstance(raw_key, str):
        return None

    raw_key = raw_key.strip()
    if raw_key.startswith("Bearer "):
        raw_key = raw_key[7:].strip()

    candidate_hash = hash_api_key(raw_key)

    cur = conn.execute(
        """
        SELECT id, tenant_id, agent_id, name, key_hash, scopes_json, expires_at, revoked_at
        FROM agent_credentials
        WHERE key_hash = ? AND revoked_at IS NULL;
        """,
        (candidate_hash,)
    )
    row = cur.fetchone()
    if not row:
        return None

    # Constant-time comparison
    if not hmac.compare_digest(candidate_hash, row["key_hash"]):
        return None

    # Check expiry
    now = time.time()
    if row["expires_at"] is not None and now > row["expires_at"]:
        return None

    scopes = json.loads(row["scopes_json"])
    if required_scope and required_scope not in scopes and "admin" not in scopes:
        return None

    return CredentialContext(
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        credential_id=row["id"],
        name=row["name"],
        scopes=scopes,
        expires_at=row["expires_at"]
    )


def revoke_credential(conn: sqlite3.Connection, credential_id: str, tenant_id: str) -> bool:
    """Revokes an agent credential, locking out subsequent requests immediately."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute(
        """
        UPDATE agent_credentials
        SET revoked_at = ?
        WHERE id = ? AND tenant_id = ? AND revoked_at IS NULL;
        """,
        (now_iso, credential_id, tenant_id)
    )
    return cur.rowcount == 1
