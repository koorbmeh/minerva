"""MCP server over stdio — newline-delimited JSON-RPC 2.0.

Implements: initialize, notifications/initialized, ping, tools/list, tools/call.
Governance: every tools/call passes GovernanceGate first.
  allow -> execute handler, ledger the call
  ask   -> queue in approvals.jsonl, tell the engine to stand down (isError)
  deny  -> refuse (isError)
Logs to stderr only; stdout carries protocol messages exclusively.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

from .. import verdicts as verdicts_mod
from ..governance import ALLOW, ASK_OPERATOR, GovernanceGate
from ..ledger import Ledger, ProposalIndex, TOOL_CALL
from .registry import ToolRegistry

PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
DEFAULT_PROTOCOL = "2025-06-18"
RESULT_CAP = 20_000          # chars of tool output returned/ledgered


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class MCPGateway:
    def __init__(self, registry: ToolRegistry, gate: GovernanceGate, ledger: Ledger,
                 approvals_path: str, name: str = "minerva-gateway",
                 version: str = "0.1.0",
                 proposals: ProposalIndex | None = None,
                 verdicts_path: str | None = None) -> None:
        self.registry = registry
        self.gate = gate
        self.ledger = ledger
        self.approvals_path = approvals_path
        self.name = name
        self.version = version
        # Pitch-verdict fold (2026-06-11): this process is the single ledger
        # appender, so IT folds the operator's verdicts.jsonl decisions in — at
        # startup and before any pitch_idea call (anti-nag must see them
        # before the next can_pitch). None/None disables (legacy tests).
        self.proposals = proposals
        self.verdicts_path = verdicts_path
        self._verdicts_seen: tuple[float, int] | None = None
        if self.proposals is not None and self.verdicts_path:
            self._fold_verdicts()

    # ---- pitch-verdict fold ---------------------------------------------
    def _fold_verdicts(self) -> None:
        try:
            summary = verdicts_mod.fold(self.ledger, self.proposals, self.verdicts_path)
            if summary["folded"] or summary["errors"]:
                print(f"verdict fold: {summary}", file=sys.stderr)
        except Exception:                                         # noqa: BLE001
            traceback.print_exc(file=sys.stderr)                  # never kill serving
        try:
            st = os.stat(self.verdicts_path)
            self._verdicts_seen = (st.st_mtime, st.st_size)
        except OSError:
            self._verdicts_seen = None

    def _maybe_fold_verdicts(self) -> None:
        """Cheap mtime/size guard so pitch_idea calls don't re-read the file
        unless the operator actually recorded something since the last fold."""
        if self.proposals is None or not self.verdicts_path:
            return
        try:
            st = os.stat(self.verdicts_path)
            now = (st.st_mtime, st.st_size)
        except OSError:
            now = None
        if now != self._verdicts_seen:
            self._fold_verdicts()

    # ---- protocol ------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method", "")
        msg_id = msg.get("id")
        is_notification = "id" not in msg

        try:
            if method == "initialize":
                client_ver = (msg.get("params") or {}).get("protocolVersion", "")
                ver = client_ver if client_ver in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
                return self._ok(msg_id, {
                    "protocolVersion": ver,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                })
            if method == "notifications/initialized":
                return None
            if method == "ping":
                return self._ok(msg_id, {})
            if method == "tools/list":
                return self._ok(msg_id, {"tools": self.registry.mcp_list()})
            if method == "tools/call":
                params = msg.get("params") or {}
                result = self.call_tool(params.get("name", ""), params.get("arguments") or {})
                return self._ok(msg_id, result)
            if is_notification:
                return None
            return self._err(msg_id, -32601, f"method not found: {method}")
        except Exception as exc:                                  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            if is_notification:
                return None
            return self._err(msg_id, -32603, f"internal error: {exc}")

    # ---- governance-wrapped execution ----------------------------------
    def call_tool(self, name: str, arguments: dict) -> dict:
        spec = self.registry.get(name)
        if spec is None:
            return _text_result(f"unknown tool: {name}", is_error=True)
        if name == "pitch_idea":         # taste state must be current first
            self._maybe_fold_verdicts()

        decision = self.gate.check(tool=name, args=arguments, classes=spec.classes,
                                   est_cost_usd=spec.est_cost_usd)
        if decision.decision == ASK_OPERATOR:
            if decision.already_queued:      # identical call already awaiting the operator
                return _text_result(
                    f"STILL AWAITING the operator (ref {decision.awaiting_ref}): "
                    f"{decision.reason}. Do not re-ask or work around this; "
                    "continue with other tasks.", is_error=True)
            self._queue_approval(decision.entry_id, name, arguments, decision.reason)
            return _text_result(
                f"ESCALATED to the operator (ref {decision.entry_id}): {decision.reason}. "
                "Continue with other tasks. If the operator approves, retry this exact "
                "call (same tool, same arguments) once - changed arguments need "
                "a fresh approval.", is_error=True)
        if decision.decision != ALLOW:
            return _text_result(f"DENIED (ref {decision.entry_id}): {decision.reason}",
                                is_error=True)
        try:
            out = str(spec.handler(arguments))[:RESULT_CAP]
            ok = True
        except Exception as exc:                                  # noqa: BLE001
            out, ok = f"tool failed: {exc}", False
        self.ledger.append(TOOL_CALL,
                           {"tool": name, "ok": ok, "result_head": out[:500]},
                           parent_id=decision.entry_id, actor="engine")
        return _text_result(out, is_error=not ok)

    def _queue_approval(self, entry_id: str, tool: str, arguments: dict, reason: str) -> None:
        import time
        with open(self.approvals_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ref": entry_id, "ts": time.time(), "tool": tool,
                                "args": arguments, "reason": reason, "status": "pending"},
                               ensure_ascii=False) + "\n")

    # ---- plumbing -------------------------------------------------------
    @staticmethod
    def _ok(msg_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _err(msg_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                resp = self._err(None, -32700, "parse error")
            else:
                resp = self.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=True,   # ASCII-safe: survives
                                        separators=(",", ":")) + "\n")  # cp1252 pipes (2026-06-10)
                stdout.flush()
