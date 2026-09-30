"""Tool registry — tools self-describe their governance metadata at registration.

The gateway, not the engine, owns this metadata (spec §3): action classes
drive GovernanceGate decisions; input_schema is what the engine sees.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict                      # JSON Schema for MCP tools/list
    handler: Callable[[dict], str]          # args -> text result (raise on failure)
    classes: set[str] = field(default_factory=set)   # governance action classes
    est_cost_usd: float = 0.0


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def restrict(self, allowed: set[str]) -> None:
        """Drop everything not in `allowed` (per-engine/per-session allowlist)."""
        for name in list(self._tools):
            if name not in allowed:
                del self._tools[name]

    def mcp_list(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in self._tools.values()]
