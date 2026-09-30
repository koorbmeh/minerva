"""Minerva MCP governance gateway — the sole tool source for any engine.

Engines (Hermes, frontier APIs) are configured with this gateway as their
only MCP server. Every tools/call passes GovernanceGate BEFORE execution
and lands in the ledger. The trust boundary is here, never in the engine.

Run:  python -m minerva_core.gateway --state-dir state [--brain PATH] [--tools a,b,c]
"""

from .registry import ToolRegistry, ToolSpec
from .mcp_stdio import MCPGateway

__all__ = ["ToolRegistry", "ToolSpec", "MCPGateway"]
