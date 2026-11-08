-- Allowance v4.2.2 SQLite Schema
-- Engine: SQLite WAL mode, FULL synchronous, foreign keys enabled

PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

-- 1. Metadata & Runtime Boot
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 2. Tenant & Agents
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 3. Policy & Immutable History
CREATE TABLE IF NOT EXISTS policy (
    agent_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    daily_budget_cents INTEGER NOT NULL,
    max_per_tx_cents INTEGER NOT NULL,
    approval_threshold_cents INTEGER NOT NULL,
    velocity_cap_cents INTEGER NOT NULL,
    allowlist_json TEXT NOT NULL,
    timezone TEXT NOT NULL, -- IMMUTABLE via API
    frozen INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS policy_history (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    policy_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);

-- 4. Authoritative Agent Runs & Queue
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    request_hash TEXT NOT NULL,
    user_prompt TEXT,
    worker_id TEXT,
    queued_at REAL NOT NULL,
    started_at REAL,
    status TEXT NOT NULL, -- QUEUED, RUNNING, COMPLETED, FAILED
    status_reason TEXT,
    trace_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);
CREATE INDEX IF NOT EXISTS idx_runs_tenant_status ON agent_runs(tenant_id, status);

-- 5. Purchase Workflow Lifecycle
CREATE TABLE IF NOT EXISTS purchases (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL, -- Forward-only state machine
    status_reason TEXT,
    auto_approved INTEGER NOT NULL,
    cart_json TEXT NOT NULL,
    cart_hash TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    merchant_ids_json TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    create_request_id TEXT NOT NULL UNIQUE,
    capture_boot_id TEXT,
    paypal_order_id TEXT,
    approval_url TEXT,
    state_entered_at REAL NOT NULL,
    expires_at REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agents(id),
    FOREIGN KEY(run_id) REFERENCES agent_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_purchases_budget_day ON purchases(tenant_id, budget_day, status);

-- 6. Durable Capture Operations (1:1 with Purchase)
CREATE TABLE IF NOT EXISTS capture_operations (
    id TEXT PRIMARY KEY,
    purchase_id TEXT NOT NULL UNIQUE,
    capture_request_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL, -- PREPARED, MAY_HAVE_BEEN_SENT, DEFINITIVE_SUCCESS, DEFINITIVE_FAILURE, UNKNOWN, RESOLVED_SUCCESS, RESOLVED_FAILURE
    first_send_started_at REAL,
    last_observed_at REAL,
    paypal_order_status TEXT,
    paypal_capture_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    owner_token TEXT,
    owner_lease_until REAL,
    last_error_class TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(purchase_id) REFERENCES purchases(id)
);

-- 7. Append-Only Financial Facts (Source of Truth)
CREATE TABLE IF NOT EXISTS financial_facts (
    id TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL UNIQUE, -- Idempotency key per financial movement
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    purchase_id TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    currency TEXT NOT NULL,
    fact_type TEXT NOT NULL, -- RESERVATION_CREATED, RESERVATION_RELEASED, CAPTURE_BECAME_UNKNOWN, CAPTURE_SETTLED, UNRESOLVED_CONFIRMED_NOT_SETTLED, LATE_CAPTURE_SETTLED
    delta_reserved_cents INTEGER NOT NULL,
    delta_unresolved_cents INTEGER NOT NULL,
    delta_captured_cents INTEGER NOT NULL,
    delta_auto_approved_cents INTEGER NOT NULL,
    paypal_capture_id TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(purchase_id) REFERENCES purchases(id)
);
CREATE INDEX IF NOT EXISTS idx_financial_facts_budget ON financial_facts(agent_id, budget_day, currency);

-- 8. Rebuildable Budget Accounting Projection
CREATE TABLE IF NOT EXISTS budget_accounting (
    agent_id TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    currency TEXT NOT NULL,
    captured_cents INTEGER NOT NULL DEFAULT 0,
    reserved_cents INTEGER NOT NULL DEFAULT 0,
    unresolved_cents INTEGER NOT NULL DEFAULT 0,
    auto_approved_committed_cents INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, budget_day, currency)
);

-- 9. Purchase Workflow Ledger (State Transitions Only)
CREATE TABLE IF NOT EXISTS ledger (
    id TEXT PRIMARY KEY,
    purchase_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    policy_version INTEGER NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(purchase_id) REFERENCES purchases(id),
    UNIQUE(purchase_id, to_state) -- Guarantees rank acyclicity
);

-- 10. Late Adjustments & Post-Terminal Financial Facts
CREATE TABLE IF NOT EXISTS ledger_adjustments (
    id TEXT PRIMARY KEY,
    purchase_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    adjustment_type TEXT NOT NULL, -- LATE_CAPTURE_SETTLED, LATE_CAPTURE_CONFIRMED_NOT_SETTLED
    amount_cents INTEGER NOT NULL,
    paypal_capture_id TEXT,
    discovered_at TEXT NOT NULL,
    resolved_by TEXT NOT NULL,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(purchase_id) REFERENCES purchases(id),
    UNIQUE(purchase_id, paypal_capture_id)
);

-- 11. Operational & SSE Audit Events
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    purchase_id TEXT,
    run_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 12. Immutability Triggers
CREATE TRIGGER IF NOT EXISTS abort_ledger_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'Ledger entries are strictly immutable'); END;
CREATE TRIGGER IF NOT EXISTS abort_ledger_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT, 'Ledger entries cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS abort_financial_facts_update BEFORE UPDATE ON financial_facts BEGIN SELECT RAISE(ABORT, 'Financial facts are strictly immutable'); END;
CREATE TRIGGER IF NOT EXISTS abort_financial_facts_delete BEFORE DELETE ON financial_facts BEGIN SELECT RAISE(ABORT, 'Financial facts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS abort_policy_history_update BEFORE UPDATE ON policy_history BEGIN SELECT RAISE(ABORT, 'Policy history is strictly immutable'); END;
CREATE TRIGGER IF NOT EXISTS abort_policy_history_delete BEFORE DELETE ON policy_history BEGIN SELECT RAISE(ABORT, 'Policy history cannot be deleted'); END;

