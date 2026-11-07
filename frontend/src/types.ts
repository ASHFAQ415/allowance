export interface Purchase {
  id: string;
  agent_id: string;
  status: string;
  status_reason?: string;
  auto_approved: boolean;
  total_cents: number;
  currency: string;
  merchant_ids: string[];
  budget_day: string;
  created_at: string;
  updated_at: string;
}

export interface LedgerEntry {
  id: string;
  purchase_id: string;
  agent_id: string;
  from_state?: string;
  to_state: string;
  rule_name: string;
  reason: string;
  checkpoint: string;
  amount_cents: number;
  actor: string;
  created_at: string;
}

export interface Policy {
  agent_id: string;
  version: number;
  daily_budget_cents: number;
  max_per_tx_cents: number;
  approval_threshold_cents: number;
  velocity_cap_cents: number;
  allowlist: string[];
  timezone: string;
  frozen: boolean;
}

export interface SSEEvent {
  id: number;
  type: string;
  payload: Record<string, any>;
}
