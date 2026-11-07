import React, { useState, useEffect } from 'react';
import { 
  ShieldAlert, ShieldCheck, ShoppingBag, Terminal, CheckCircle2, 
  Clock, ArrowRight, Zap, RefreshCw, Key
} from 'lucide-react';
import { Purchase, LedgerEntry, Policy } from './types';

export default function App() {
  const [prompt, setPrompt] = useState('Find trail running shoes under $45 from approved store');
  const [isRunning, setIsRunning] = useState(false);
  const [adminToken, setAdminToken] = useState('dev-secret-admin-token-min-32-chars-long-abc-123');
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [purchases, setPurchases] = useState<Purchase[]>([]);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [timeline, setTimeline] = useState<string[]>([
    'System ready. Autonomous policy engine active.'
  ]);

  const fetchDashboardData = async () => {
    try {
      const polRes = await fetch('/api/policy', {
        headers: { 'X-Demo-Session': 'demo-session-token-allowance-hackathon-2026' }
      });
      if (polRes.ok) setPolicy(await polRes.json());

      const purRes = await fetch('/api/purchases', {
        headers: { 'X-Demo-Session': 'demo-session-token-allowance-hackathon-2026' }
      });
      if (purRes.ok) setPurchases(await purRes.json());

      const ledRes = await fetch('/api/ledger', {
        headers: { 'X-Demo-Session': 'demo-session-token-allowance-hackathon-2026' }
      });
      if (ledRes.ok) setLedger(await ledRes.json());
    } catch (err) {
      console.error('Failed to fetch dashboard data', err);
    }
  };

  useEffect(() => {
    fetchDashboardData();
    const interval = setInterval(fetchDashboardData, 3000);
    return () => clearInterval(interval);
  }, []);

  const handleRunAgent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!prompt.trim() || isRunning) return;

    setIsRunning(true);
    setTimeline(prev => [`[${new Date().toLocaleTimeString()}] ShopMind searching catalog for: "${prompt}"...`, ...prev]);

    try {
      const res = await fetch('/api/agent/run', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `run_${Date.now()}`,
          'X-Demo-Session': 'demo-session-token-allowance-hackathon-2026'
        },
        body: JSON.stringify({ user_prompt: prompt, agent_id: 'default' })
      });

      if (res.ok) {
        const data = await res.json();
        setTimeline(prev => [`[${new Date().toLocaleTimeString()}] Run queued (ID: ${data.run_id.slice(0, 8)}). Evaluating PayPilot Checkpoint P1...`, ...prev]);
        setTimeout(fetchDashboardData, 1000);
      } else {
        const err = await res.json();
        setTimeline(prev => [`[${new Date().toLocaleTimeString()}] Run Rejected: ${err.detail}`, ...prev]);
      }
    } catch (err) {
      setTimeline(prev => [`[${new Date().toLocaleTimeString()}] Network error dispatching run.`, ...prev]);
    } finally {
      setIsRunning(false);
    }
  };

  const toggleFreeze = async () => {
    if (!policy) return;
    const endpoint = policy.frozen ? '/api/unfreeze' : '/api/freeze';
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'X-Admin-Token': adminToken }
      });
      if (res.ok) {
        fetchDashboardData();
        setTimeline(prev => [`[${new Date().toLocaleTimeString()}] Emergency Freeze ${policy.frozen ? 'DEACTIVATED' : 'ACTIVATED'} (Rule R0).`, ...prev]);
      }
    } catch (err) {
      console.error('Failed to toggle freeze', err);
    }
  };

  const handleApprove = async (purchaseId: string) => {
    try {
      const res = await fetch(`/api/purchases/${purchaseId}/approve`, {
        method: 'POST',
        headers: { 'X-Admin-Token': adminToken }
      });
      if (res.ok) {
        setTimeline(prev => [`[${new Date().toLocaleTimeString()}] Admin approved purchase ${purchaseId.slice(0, 8)}. PayPilot Checkpoint P2 passed!`, ...prev]);
        fetchDashboardData();
      }
    } catch (err) {
      console.error('Failed to approve purchase', err);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col">
      {/* Top Navigation */}
      <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-blue-600/20 border border-blue-500/30 flex items-center justify-center text-blue-400">
            <Zap className="w-6 h-6" />
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
              ALLOWANCE <span className="text-xs px-2 py-0.5 rounded-full bg-blue-500/10 text-blue-400 border border-blue-500/20">v4.2.2</span>
            </h1>
            <p className="text-xs text-slate-400">Give your AI agent an allowance, not your wallet.</p>
          </div>
        </div>

        {/* Global Controls */}
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-1.5 bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1 text-xs text-slate-400">
            <Key className="w-3.5 h-3.5 text-blue-400" />
            <input
              type="password"
              value={adminToken}
              onChange={(e) => setAdminToken(e.target.value)}
              title="Admin Token for Freeze and Approvals"
              placeholder="Admin Token"
              className="bg-transparent text-slate-300 w-28 focus:w-48 transition-all focus:outline-none font-mono text-xs"
            />
          </div>
          <button
            onClick={toggleFreeze}
            className={`px-4 py-2 rounded-lg font-semibold text-sm flex items-center gap-2 border transition-all ${
              policy?.frozen
                ? 'bg-red-500 text-white border-red-400 shadow-lg shadow-red-500/20 animate-pulse'
                : 'bg-slate-800 text-slate-300 border-slate-700 hover:bg-slate-700'
            }`}
          >
            <ShieldAlert className="w-4 h-4" />
            {policy?.frozen ? 'EMERGENCY FREEZE ACTIVE' : 'Toggle Kill Switch'}
          </button>
        </div>
      </header>

      {/* Main Container */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-6 grid grid-cols-1 lg:grid-cols-3 gap-6">
        
        {/* Left Col: ShopMind Agent Chat & Timeline */}
        <div className="lg:col-span-1 flex flex-col gap-6">
          {/* Agent Input Box */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-sm">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-2">
              <ShoppingBag className="w-4 h-4 text-blue-400" />
              ShopMind Procurement Agent
            </h2>
            <form onSubmit={handleRunAgent} className="flex flex-col gap-3">
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={3}
                className="w-full bg-slate-950 border border-slate-800 rounded-lg p-3 text-sm text-slate-200 focus:outline-none focus:border-blue-500 transition-colors resize-none"
                placeholder="Give your agent a task (e.g. buy shoes under $45)..."
              />
              <button
                type="submit"
                disabled={isRunning}
                className="w-full bg-blue-600 hover:bg-blue-500 text-white font-medium text-sm py-2.5 px-4 rounded-lg flex items-center justify-center gap-2 disabled:opacity-50 transition-colors"
              >
                {isRunning ? <RefreshCw className="w-4 h-4 animate-spin" /> : <ArrowRight className="w-4 h-4" />}
                Execute Autonomous Order
              </button>
            </form>
          </div>

          {/* Active Policy Status Badge */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-2">
              <ShieldCheck className="w-4 h-4 text-emerald-400" />
              PayPilot Policy Status
            </h2>
            {policy ? (
              <div className="grid grid-cols-2 gap-3 text-xs">
                <div className="bg-slate-950 p-2.5 rounded border border-slate-800">
                  <span className="text-slate-500 block">Daily Budget</span>
                  <span className="text-sm font-bold text-slate-200">${(policy.daily_budget_cents / 100).toFixed(2)}</span>
                </div>
                <div className="bg-slate-950 p-2.5 rounded border border-slate-800">
                  <span className="text-slate-500 block">Per-Tx Limit</span>
                  <span className="text-sm font-bold text-slate-200">${(policy.max_per_tx_cents / 100).toFixed(2)}</span>
                </div>
                <div className="bg-slate-950 p-2.5 rounded border border-slate-800">
                  <span className="text-slate-500 block">Human Threshold</span>
                  <span className="text-sm font-bold text-slate-200">${(policy.approval_threshold_cents / 100).toFixed(2)}</span>
                </div>
                <div className="bg-slate-950 p-2.5 rounded border border-slate-800">
                  <span className="text-slate-500 block">Velocity Cap</span>
                  <span className="text-sm font-bold text-slate-200">${(policy.velocity_cap_cents / 100).toFixed(2)}</span>
                </div>
              </div>
            ) : (
              <p className="text-xs text-slate-500">Loading policy invariants...</p>
            )}
          </div>

          {/* Live Agent Timeline */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 flex-1 flex flex-col">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-2">
              <Terminal className="w-4 h-4 text-purple-400" />
              Agent & Policy Event Log
            </h2>
            <div className="flex-1 bg-slate-950 border border-slate-800 rounded-lg p-3 font-mono text-xs overflow-y-auto max-h-64 flex flex-col gap-2 text-slate-300">
              {timeline.map((msg, i) => (
                <div key={i} className="leading-relaxed border-b border-slate-900 pb-1 last:border-none">
                  {msg}
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Center & Right Col: Approvals & Financial Ledger */}
        <div className="lg:col-span-2 flex flex-col gap-6">
          
          {/* Approvals Queue */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3 flex items-center justify-between">
              <span className="flex items-center gap-2">
                <Clock className="w-4 h-4 text-amber-400" />
                Human Review Approval Queue
              </span>
              <span className="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-mono">
                {purchases.filter(p => p.status === 'PENDING_APPROVAL').length} Pending
              </span>
            </h2>

            <div className="flex flex-col gap-3">
              {purchases.filter(p => p.status === 'PENDING_APPROVAL').length === 0 ? (
                <div className="py-8 text-center text-slate-500 text-xs border border-dashed border-slate-800 rounded-lg">
                  Zero carts pending review. Automated allowance flows running smoothly.
                </div>
              ) : (
                purchases
                  .filter(p => p.status === 'PENDING_APPROVAL')
                  .map(pur => (
                    <div key={pur.id} className="bg-slate-950 border border-amber-500/30 p-4 rounded-lg flex items-center justify-between">
                      <div>
                        <div className="flex items-center gap-2 mb-1">
                          <span className="font-bold text-slate-100 text-sm">${(pur.total_cents / 100).toFixed(2)}</span>
                          <span className="text-xs px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20 font-mono">
                            R4: Over Threshold
                          </span>
                        </div>
                        <p className="text-xs text-slate-400">Merchants: {pur.merchant_ids.join(', ')}</p>
                        <p className="text-xs text-slate-500 font-mono">ID: {pur.id.slice(0, 8)}</p>
                      </div>
                      <button
                        onClick={() => handleApprove(pur.id)}
                        className="bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold px-4 py-2 rounded-lg transition-colors flex items-center gap-1.5"
                      >
                        <CheckCircle2 className="w-4 h-4" />
                        Approve Purchase
                      </button>
                    </div>
                  ))
              )}
            </div>
          </div>

          {/* Immutable Financial Ledger Table */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 flex-1 flex flex-col">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3 flex items-center justify-between">
              <span className="flex items-center gap-2">
                <CheckCircle2 className="w-4 h-4 text-emerald-400" />
                Immutable Financial Journal & State Ledger
              </span>
              <span className="text-xs text-slate-500">Append-Only / WAL FULL Sync</span>
            </h2>

            <div className="overflow-x-auto flex-1">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-slate-800 text-slate-500 font-semibold uppercase">
                    <th className="py-2.5 px-3">Time</th>
                    <th className="py-2.5 px-3">State Transition</th>
                    <th className="py-2.5 px-3">Enforced Rule</th>
                    <th className="py-2.5 px-3">Amount</th>
                    <th className="py-2.5 px-3">Actor</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-850 text-slate-300">
                  {ledger.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="py-8 text-center text-slate-500">
                        No ledger transitions recorded yet.
                      </td>
                    </tr>
                  ) : (
                    ledger.map(entry => (
                      <tr key={entry.id} className="hover:bg-slate-850/50 transition-colors">
                        <td className="py-2.5 px-3 font-mono text-slate-500">{new Date(entry.created_at).toLocaleTimeString()}</td>
                        <td className="py-2.5 px-3 font-mono">
                          <span className="text-slate-400">{entry.from_state || 'START'}</span>
                          <span className="text-slate-600 mx-1.5">→</span>
                          <span className={`font-semibold ${
                            entry.to_state === 'CAPTURED' ? 'text-emerald-400' :
                            entry.to_state === 'BLOCKED' ? 'text-red-400' :
                            entry.to_state === 'PENDING_APPROVAL' ? 'text-amber-400' : 'text-blue-400'
                          }`}>
                            {entry.to_state}
                          </span>
                        </td>
                        <td className="py-2.5 px-3">
                          <span className="px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 font-mono text-[10px]">
                            {entry.rule_name}
                          </span>
                        </td>
                        <td className="py-2.5 px-3 font-mono font-medium text-slate-200">
                          ${(entry.amount_cents / 100).toFixed(2)}
                        </td>
                        <td className="py-2.5 px-3 text-slate-400">{entry.actor}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>

        </div>

      </main>
    </div>
  );
}
