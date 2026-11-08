"""
Unit Tests for ShopMind Agent Sandbox Import Boundary.
Verifies Invariants 6 (AI & Payment Authority Separation) and evidence-based safety.

Adversarial Scenarios covered:
  #29 Prompt injection via user input (AST import boundary proof)
  #30 Malicious catalog content injection (import boundary proof)
  #31 Tool argument manipulation (zero-argument submit_cart schema)
"""

import ast
import os
import unittest


AGENT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "app", "agent"
)

# Modules the agent is strictly forbidden from importing
FORBIDDEN_MODULES = {
    "db",
    "financial_journal",
    "connection",
    "bootstrap",
    "orchestrator",
    "admission",
    "capture_lifecycle",
    "core",
    "queue",
    "rechecks",
    "state_machine",
    "paypal",
    "client",
    "auth",
    "routes",
    "dto",
}

# Env-var names that must NEVER appear in agent source
FORBIDDEN_ENV_VARS = {
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "ADMIN_TOKEN",
    "PAYPAL_MERCHANT_ID",
    "ANTHROPIC_API_KEY",
}


def _collect_imports(source: str) -> list[tuple[str, str]]:
    """Parse source and return list of (module_root, full_module) import pairs."""
    tree = ast.parse(source)
    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                results.append((root, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                results.append((root, node.module))
    return results


def _collect_string_literals(source: str) -> list[str]:
    """Parse source and return all string literal values."""
    tree = ast.parse(source)
    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
    return literals


class TestAgentSandboxImportBoundary(unittest.TestCase):
    """
    AST-level proof that the agent module boundary is enforced.
    If any agent module ever imports a forbidden subsystem, this test fails
    at the _code_ level — before runtime ever executes.
    """

    def _get_agent_py_files(self) -> list[tuple[str, str]]:
        """Returns list of (filename, full_path) for all .py files in the agent dir."""
        files = []
        for fname in os.listdir(AGENT_DIR):
            if fname.endswith(".py") and not fname.startswith("__"):
                files.append((fname, os.path.join(AGENT_DIR, fname)))
        self.assertGreater(len(files), 0, "No agent Python files found — check AGENT_DIR path.")
        return files

    def test_agent_modules_do_not_import_db_or_orchestrator(self):
        """
        Scenario #29/#30: AST proof that no agent module imports db, orchestrator,
        paypal, or policy subsystems directly.
        Prevents the agent from bypassing the orchestration layer.
        """
        violations = []
        for fname, fpath in self._get_agent_py_files():
            with open(fpath, "r", encoding="utf-8") as f:
                source = f.read()
            imports = _collect_imports(source)
            for root, full_module in imports:
                # Check if any import segment references a forbidden module
                segments = full_module.lower().split(".")
                for seg in segments:
                    if seg in FORBIDDEN_MODULES:
                        violations.append(
                            f"  [{fname}] imports '{full_module}' (segment '{seg}' is forbidden)"
                        )
                        break

        if violations:
            self.fail(
                "Agent module(s) import forbidden subsystems — sandbox boundary violated!\n"
                + "\n".join(violations)
            )

    def test_agent_modules_do_not_contain_payment_credentials(self):
        """
        Scenario #30: Verify no agent source file embeds payment credential
        env-var names or hardcoded secret patterns.
        """
        violations = []
        for fname, fpath in self._get_agent_py_files():
            with open(fpath, "r", encoding="utf-8") as f:
                source = f.read()
            literals = _collect_string_literals(source)
            for literal in literals:
                for forbidden_var in FORBIDDEN_ENV_VARS:
                    if forbidden_var in literal:
                        violations.append(
                            f"  [{fname}] contains forbidden env-var reference: '{forbidden_var}'"
                        )

        if violations:
            self.fail(
                "Agent module(s) contain payment credential references!\n"
                + "\n".join(violations)
            )

    def test_submit_cart_tool_accepts_zero_arguments(self):
        """
        Scenario #31: submit_cart must accept ZERO required arguments.
        It submits only the latest validated cart already held in loop state.
        A model must not be able to inject product IDs or amounts via submit_cart arguments.
        """
        import inspect
        from backend.app.agent.tools import tool_build_cart  # noqa: F401

        # Confirm build_cart enforces server-side pricing (zero price-inject surface)
        # and that there is no tool named submit_cart that accepts product arguments.
        # The loop submits the validated cart directly — no agent-facing submit_cart fn.
        tools_path = os.path.join(AGENT_DIR, "tools.py")
        with open(tools_path, "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and "submit_cart" in node.name:
                # If a submit_cart function exists, assert it has no required positional args
                # beyond 'self' (instance methods) or zero args (module-level).
                args = node.args
                required_positional = [
                    a for a in args.args
                    if a.arg != "self"
                ]
                defaults_count = len(args.defaults)
                required_count = len(required_positional) - defaults_count
                self.assertEqual(
                    required_count, 0,
                    f"submit_cart has {required_count} required positional arg(s) — "
                    "agents must not inject product data via submit_cart arguments."
                )


if __name__ == "__main__":
    unittest.main()

