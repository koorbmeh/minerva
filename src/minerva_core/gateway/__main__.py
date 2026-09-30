"""Entry point: python -m minerva_core.gateway --state-dir state [--brain PATH]
                 [--workspace PATH] [--tools a,b,c]

This is the command an engine's MCP config launches (stdio transport).
Hermes example (config.yaml):

  mcp_servers:
    minerva:
      command: python
      args: ["-m", "minerva_core.gateway", "--state-dir", "C:/path/to/state",
             "--brain", "C:/path/to/Brain"]
      tools: {include: ["ping","brain_recall","episodic_note","pitch_idea","notify_operator"]}
"""
from __future__ import annotations

import argparse
import os
import sys

from ..governance import GovernanceGate
from ..inline_warden import (InlineWarden, fail_mode, inline_classes,
                              inline_mode)
from ..ledger import Ledger, ProposalIndex
from ..spend import SpendMeter, spend_path
from .mcp_stdio import MCPGateway
from .tools_builtin import build_default_registry


def main() -> None:
    # Windows pipes default to cp1252; MCP is UTF-8. Reconfigure FIRST or any
    # non-ASCII char in a tool description kills the handshake (hit live 2026-06-10:
    # an em-dash became 0x97 and Hermes dropped the connection).
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, OSError):
            pass

    # Env-var fallbacks (2026-06-10): Hermes 0.16.0's `mcp add --args` chokes on dashed
    # tokens, but `--env KEY=VALUE` passes cleanly — so every flag has an env twin.
    # CLI flag, when present, still wins.
    env = os.environ
    ap = argparse.ArgumentParser(prog="minerva-gateway")
    ap.add_argument("--state-dir", default=env.get("MINERVA_STATE_DIR", "state"))
    ap.add_argument("--brain", default=env.get("MINERVA_BRAIN") or None,
                    help="Brain dir (enables brain_recall)")
    ap.add_argument("--workspace", default=env.get("MINERVA_WORKSPACE") or None,
                    help="her contained scratch dir (default <state-dir>/workspace; "
                         "enables web_fetch + files_*)")
    ap.add_argument("--tools", default=env.get("MINERVA_TOOLS") or None,
                    help="comma allowlist (sensitivity routing)")
    ap.add_argument("--spend-threshold", type=float,
                    default=float(env.get("MINERVA_SPEND_THRESHOLD", "5.0")))
    args = ap.parse_args()

    os.makedirs(args.state_dir, exist_ok=True)
    approvals_path = os.path.join(args.state_dir, "approvals.jsonl")
    ledger = Ledger(os.path.join(args.state_dir, "ledger.jsonl"))
    proposals = ProposalIndex(ledger)
    meter = SpendMeter(spend_path(args.state_dir))     # round 15: budget (G4)
    icls = inline_classes()                            # round 21: pre-action veto
    reviewer = InlineWarden(spend_file=spend_path(args.state_dir)) if icls else None
    gate = GovernanceGate(ledger, spend_ask_threshold_usd=args.spend_threshold,
                          approvals_path=approvals_path,   # v0.1: retry honoring
                          spend_meter=meter,
                          state_dir=args.state_dir,        # round 16: STOP file
                          inline_reviewer=reviewer,        # round 21
                          inline_classes=icls,
                          inline_fail=fail_mode(),
                          inline_mode=inline_mode())
    if icls:
        print(f"inline warden: ON for {sorted(icls)} (mode={inline_mode()}, "
              f"fail={fail_mode()})",
              file=sys.stderr)
    registry = build_default_registry(
        ledger, proposals,
        outbox_path=os.path.join(args.state_dir, "outbox.jsonl"),
        brain_dir=args.brain,
        workspace_dir=args.workspace or os.path.join(args.state_dir, "workspace"),
        spend_meter=meter)
    if args.tools:
        registry.restrict({t.strip() for t in args.tools.split(",") if t.strip()})

    gw = MCPGateway(registry, gate, ledger, approvals_path=approvals_path,
                    proposals=proposals,
                    verdicts_path=os.path.join(args.state_dir, "verdicts.jsonl"))
    print(f"minerva-gateway up: tools={registry.names()}", file=sys.stderr)
    gw.serve()


if __name__ == "__main__":
    main()
