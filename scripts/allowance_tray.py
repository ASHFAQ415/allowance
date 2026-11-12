"""
ALLOWANCE Desktop Dongle — Inspired by OpenJarvis
==================================================
A system-tray widget that:
  • Runs at Windows startup (auto-registered via Registry)
  • Pops up a floating dashboard showing budget / agent status
  • Talks to the ALLOWANCE backend at http://localhost:8000
  • Has a one-line ShopMind prompt so you can fire the AI agent from the desktop

Architecture borrows from OpenJarvis:
  - Modular layers (tray / dashboard / backend bridge / LLM bridge)
  - System-tray first (runs hidden, zero chrome until needed)
  - Local-first: talks to localhost, no cloud dependency
"""

import os
import sys
import json
import time
import winreg
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from urllib.request import urlopen, Request
from urllib.error import URLError
from urllib.parse import urlencode
from io import BytesIO

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    PYSTRAY_OK = True
except ImportError:
    PYSTRAY_OK = False

# ─── Config ───────────────────────────────────────────────────────────────────
APP_NAME   = "AllowanceDongle"
APP_LABEL  = "ALLOWANCE Agent"
BACKEND    = os.environ.get("ALLOWANCE_BACKEND", "http://localhost:8000")
TENANT     = os.environ.get("ALLOWANCE_TENANT", "demo")
CHILD_KEY  = os.environ.get("ALLOWANCE_CHILD_KEY", "child-key-1")
REFRESH_S  = 10          # budget poll interval (seconds)
WINDOW_W   = 420
WINDOW_H   = 520

# ─── Startup registry ─────────────────────────────────────────────────────────
def _startup_key():
    return r"Software\Microsoft\Windows\CurrentVersion\Run"

def enable_startup():
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        cmd = f'"{exe}" "{os.path.abspath(__file__)}"'
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _startup_key(), 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(k)
        return True
    except Exception as e:
        print(f"[startup] {e}")
        return False

def disable_startup():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _startup_key(), 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(k, APP_NAME)
        winreg.CloseKey(k)
        return True
    except Exception:
        return False

def startup_enabled():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _startup_key())
        winreg.QueryValueEx(k, APP_NAME)
        winreg.CloseKey(k)
        return True
    except Exception:
        return False

# ─── Backend bridge (stdlib only, no requests) ────────────────────────────────
def _get(path, token=None):
    url = f"{BACKEND}{path}"
    headers = {"X-Child-Key": CHILD_KEY}
    if token:
        headers["X-Admin-Token"] = token
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=4) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def _post(path, body=None, token=None):
    url = f"{BACKEND}{path}"
    data = json.dumps(body or {}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Child-Key": CHILD_KEY,
    }
    if token:
        headers["X-Admin-Token"] = token
    try:
        req = Request(url, data=data, headers=headers, method="POST")
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def fetch_budget():
    data = _get("/v1/budget")
    if isinstance(data, dict) and ("error" in data or "detail" in data):
        # Fallback to dev_server /api routes
        b = _get("/api/budget")
        if isinstance(b, dict) and "error" in b:
            return b
        p = _get("/api/policy")
        daily = p.get("daily_budget_cents", 10000) if isinstance(p, dict) else 10000
        captured = b.get("captured_cents", 0) if isinstance(b, dict) else 0
        reserved = b.get("reserved_cents", 0) if isinstance(b, dict) else 0
        unresolved = b.get("unresolved_cents", 0) if isinstance(b, dict) else 0
        committed = captured + reserved + unresolved
        avail = max(0, daily - committed)
        return {
            "budget_cents": daily,
            "captured_cents": captured,
            "reserved_cents": reserved,
            "available_cents": avail,
            "frozen": p.get("frozen", False) if isinstance(p, dict) else False,
        }
    return data

def fetch_purchases():
    data = _get("/v1/purchases?limit=5")
    if not isinstance(data, list) or len(data) == 0:
        data = _get("/api/purchases")
    return data if isinstance(data, list) else []

def run_agent(prompt):
    res = _post("/v1/agent/run", {"prompt": prompt})
    if isinstance(res, dict) and ("error" in res or "detail" in res):
        res = _post("/api/agent/run", {"prompt": prompt})
    return res

# ─── Tray icon generator ──────────────────────────────────────────────────────
def _make_icon(color="#6C63FF", label="A"):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Gradient-ish circle
    d.ellipse([2, 2, 62, 62], fill=color, outline="#ffffff", width=2)
    d.text((22, 18), label, fill="white")
    return img

# ─── Dashboard window ─────────────────────────────────────────────────────────
class AllowanceDashboard:
    def __init__(self, app):
        self.app = app
        self.root = tk.Tk()
        self.root.withdraw()          # start hidden
        self.root.title("ALLOWANCE — Agent Dashboard")
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}+80+80")
        self.root.resizable(False, False)
        self.root.configure(bg="#0f0f1a")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        self._build_ui()
        self._refresh_loop()

    # ── UI layout ──────────────────────────────────────────────────────────
    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        # Header
        hdr = tk.Frame(self.root, bg="#6C63FF", height=56)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🧠 ALLOWANCE  ·  AI Agent Dongle",
                 font=("Segoe UI", 13, "bold"), fg="white", bg="#6C63FF"
                 ).pack(side="left", padx=14, pady=12)
        tk.Label(hdr, text="Powered by ShopMind + PayPilot",
                 font=("Segoe UI", 8), fg="#d0d0ff", bg="#6C63FF"
                 ).pack(side="right", padx=14)

        # Budget panel
        bframe = tk.LabelFrame(self.root, text=" 💰 Budget Status ", fg="#6C63FF",
                               bg="#0f0f1a", font=("Segoe UI", 9, "bold"),
                               relief="flat", highlightthickness=1,
                               highlightbackground="#2a2a3a")
        bframe.pack(fill="x", **pad)

        self.lbl_budget    = self._stat(bframe, "Budget",    "—")
        self.lbl_spent     = self._stat(bframe, "Spent",     "—")
        self.lbl_reserved  = self._stat(bframe, "Reserved",  "—")
        self.lbl_available = self._stat(bframe, "Available", "—")
        self.lbl_frozen    = self._stat(bframe, "System",    "—")

        # Recent purchases
        pframe = tk.LabelFrame(self.root, text=" 🛒 Recent Purchases ", fg="#6C63FF",
                               bg="#0f0f1a", font=("Segoe UI", 9, "bold"),
                               relief="flat", highlightthickness=1,
                               highlightbackground="#2a2a3a")
        pframe.pack(fill="x", **pad)

        self.purchase_list = tk.Text(pframe, height=5, bg="#12122a", fg="#c0c8ff",
                                     font=("Consolas", 8), relief="flat", state="disabled")
        self.purchase_list.pack(fill="x", padx=6, pady=4)

        # Agent prompt
        aframe = tk.LabelFrame(self.root, text=" 🤖 ShopMind Agent ", fg="#6C63FF",
                               bg="#0f0f1a", font=("Segoe UI", 9, "bold"),
                               relief="flat", highlightthickness=1,
                               highlightbackground="#2a2a3a")
        aframe.pack(fill="x", **pad)

        tk.Label(aframe, text='Ask ShopMind to find & buy something:',
                 fg="#8888cc", bg="#0f0f1a", font=("Segoe UI", 8)).pack(anchor="w", padx=6)
        self.prompt_var = tk.StringVar()
        entry_row = tk.Frame(aframe, bg="#0f0f1a")
        entry_row.pack(fill="x", padx=6, pady=4)
        tk.Entry(entry_row, textvariable=self.prompt_var, bg="#1a1a2e", fg="white",
                 insertbackground="white", relief="flat", font=("Segoe UI", 10)
                 ).pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(entry_row, text="▶ Run", command=self._run_agent,
                  bg="#6C63FF", fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10).pack(side="left", padx=(6, 0))

        self.agent_out = tk.Text(aframe, height=5, bg="#12122a", fg="#aaffcc",
                                 font=("Consolas", 8), relief="flat", state="disabled")
        self.agent_out.pack(fill="x", padx=6, pady=(0, 6))

        # Footer buttons
        frow = tk.Frame(self.root, bg="#0f0f1a")
        frow.pack(fill="x", padx=12, pady=8)
        self._btn(frow, "Open Web UI", self._open_browser, "#2a2a4a")
        self._btn(frow, "Refresh",     self._refresh_now,  "#2a2a4a")
        self._btn(frow, "✓ Enable Startup" if not startup_enabled() else "✗ Disable Startup",
                  self._toggle_startup, "#2a2a4a", ref="startup_btn")
        self._btn(frow, "Hide to Tray", self.hide, "#2a2a4a")

        # Status bar
        self.status_var = tk.StringVar(value="Connecting to backend…")
        tk.Label(self.root, textvariable=self.status_var, fg="#555577",
                 bg="#0a0a14", font=("Segoe UI", 7), anchor="w"
                 ).pack(fill="x", side="bottom")

    def _stat(self, parent, label, value):
        row = tk.Frame(parent, bg="#0f0f1a")
        row.pack(fill="x", padx=8, pady=1)
        tk.Label(row, text=label + ":", fg="#6666aa", bg="#0f0f1a",
                 width=10, anchor="w", font=("Segoe UI", 9)).pack(side="left")
        var = tk.StringVar(value=value)
        tk.Label(row, textvariable=var, fg="white", bg="#0f0f1a",
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left")
        return var

    def _btn(self, parent, text, cmd, bg, ref=None):
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg="#aaaaff",
                      font=("Segoe UI", 8), relief="flat", padx=8, pady=4)
        b.pack(side="left", padx=3)
        if ref == "startup_btn":
            self._startup_btn = b
        return b

    # ── Business logic ─────────────────────────────────────────────────────
    def _refresh_loop(self):
        self._refresh_now()
        self.root.after(REFRESH_S * 1000, self._refresh_loop)

    def _refresh_now(self):
        def _work():
            budget = fetch_budget()
            purchases = fetch_purchases()
            self.root.after(0, lambda: self._update_ui(budget, purchases))
        threading.Thread(target=_work, daemon=True).start()

    def _update_ui(self, budget, purchases):
        if "error" in budget:
            self.status_var.set(f"⚠ Backend offline — {budget['error'][:60]}")
            self.lbl_budget.set("offline")
            return

        cents = lambda k: f"${budget.get(k, 0) / 100:.2f}"
        self.lbl_budget.set(cents("budget_cents"))
        self.lbl_spent.set(cents("captured_cents"))
        self.lbl_reserved.set(cents("reserved_cents"))
        self.lbl_available.set(cents("available_cents"))
        self.lbl_frozen.set("🔴 FROZEN" if budget.get("frozen") else "🟢 Active")
        self.status_var.set(f"Last refresh: {time.strftime('%H:%M:%S')}")

        # Update purchase list
        self.purchase_list.config(state="normal")
        self.purchase_list.delete("1.0", "end")
        if purchases:
            for p in purchases[:5]:
                status = p.get("status", "?")
                pid    = p.get("id", "")[:8]
                amt    = f"${p.get('total_cents', 0)/100:.2f}"
                desc   = p.get("prompt", p.get("description", ""))[:30]
                color_map = {"CAPTURED": "🟢", "FAILED": "🔴", "PENDING_APPROVAL": "🟡",
                             "APPROVED": "🔵", "QUEUED": "⚪", "RUNNING": "🔄"}
                dot = color_map.get(status, "·")
                self.purchase_list.insert("end", f"{dot} {pid}  {amt:>7}  {status:<18}  {desc}\n")
        else:
            self.purchase_list.insert("end", "No purchases yet.")
        self.purchase_list.config(state="disabled")

    def _run_agent(self):
        prompt = self.prompt_var.get().strip()
        if not prompt:
            return
        self._write_agent(f'\U0001f504 Running: "{prompt}"\u2026\n')
        self.prompt_var.set("")

        def _work():
            result = run_agent(prompt)
            self.root.after(0, lambda: self._show_agent_result(result))
        threading.Thread(target=_work, daemon=True).start()

    def _show_agent_result(self, result):
        if "error" in result:
            self._write_agent(f"❌ Error: {result['error']}\n")
            return
        status = result.get("status", "?")
        cart   = result.get("cart", {})
        total  = cart.get("total_cents", 0) / 100 if cart else 0
        pid    = result.get("purchase_id", "")
        self._write_agent(
            f"✅ Status: {status}\n"
            f"   Cart total: ${total:.2f}\n"
            f"   Purchase ID: {pid or '—'}\n"
            + ("   ⏳ Awaiting approval (over limit)\n" if status == "PENDING_APPROVAL" else "")
        )
        self._refresh_now()

    def _write_agent(self, text):
        self.agent_out.config(state="normal")
        self.agent_out.insert("end", text)
        self.agent_out.see("end")
        self.agent_out.config(state="disabled")

    def _open_browser(self):
        import subprocess
        subprocess.Popen(f'start "" "{BACKEND}"', shell=True)

    def _toggle_startup(self):
        if startup_enabled():
            disable_startup()
            if self._startup_btn:
                self._startup_btn.config(text="✓ Enable Startup")
        else:
            ok = enable_startup()
            if ok and self._startup_btn:
                self._startup_btn.config(text="✗ Disable Startup")
            elif not ok:
                messagebox.showerror("Startup", "Could not write registry. Try running as admin.")

    # ── Show / hide ────────────────────────────────────────────────────────
    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.root.withdraw()

    def run(self):
        self.root.mainloop()


# ─── System-tray wrapper ──────────────────────────────────────────────────────
class AllowanceTray:
    def __init__(self):
        self.dashboard = AllowanceDashboard(self)
        self.icon = None

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Open Dashboard",    self._show, default=True),
            pystray.MenuItem("Refresh Budget",    lambda: self.dashboard._refresh_now()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Enable Startup" if not startup_enabled() else "Disable Startup",
                self._toggle_startup
            ),
            pystray.MenuItem("Quit",              self._quit),
        )

    def _show(self, icon=None, item=None):
        self.dashboard.root.after(0, self.dashboard.show)

    def _toggle_startup(self, icon=None, item=None):
        if startup_enabled():
            disable_startup()
        else:
            enable_startup()
        # Rebuild menu to reflect new state
        if self.icon:
            self.icon.menu = self._build_menu()

    def _quit(self, icon=None, item=None):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        self.dashboard.root.quit()
        self.dashboard.root.destroy()
        sys.exit(0)

    def run(self):
        if not PYSTRAY_OK:
            # Fallback: just show the window (no tray)
            self.dashboard.show()
            self.dashboard.run()
            return

        img = _make_icon("#6C63FF", "A")
        self.icon = pystray.Icon(
            APP_NAME, img, APP_LABEL, menu=self._build_menu()
        )
        try:
            self.icon.run_detached()
        except Exception as e:
            print(f"[tray] run_detached error: {e}")

        # Show dashboard immediately on first launch
        self.dashboard.root.after(300, self.dashboard.show)
        self.dashboard.run()      # blocks here (Tk mainloop)


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Optional: register startup on first run if --startup flag given
    if "--enable-startup" in sys.argv:
        enable_startup()
        print("Startup registered.")
        sys.exit(0)
    if "--disable-startup" in sys.argv:
        disable_startup()
        print("Startup removed.")
        sys.exit(0)

    tray = AllowanceTray()
    tray.run()
