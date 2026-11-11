"""
LLM Integration Module for Allowance Platform.
Provides Claude 3.5 Sonnet invoker adhering strictly to Invariant 6 (AI Authority Separation).
"""

from .claude_invoker import create_claude_invoker, CLAUDE_TOOLS, SYSTEM_PROMPT

__all__ = ["create_claude_invoker", "CLAUDE_TOOLS", "SYSTEM_PROMPT"]

