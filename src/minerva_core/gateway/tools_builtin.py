"""Starter tools. Each declares its governance classes honestly - the gate
decides from these, so misclassifying a tool here IS the security bug.

brain_recall is read-only and stays useful only on engines allowed to see
private data - restrict via --tools per engine (sensitivity routing).
Round 12: it is now a REAL manifest-routed search (minerva_core.brain_recall)
instead of a naive substring grep - size-capped, still strictly read-only
(the brain_access lock allows read-only; engines still have NO write path).
pitch_idea routes through ProposalIndex: the anti-nag ledger is engine-visible,
so a refused idea comes back with the reason and the engine learns.
Round 12 also adds the governed CAPABILITY tools (web_fetch + files_* in a
contained workspace) - see tools_capability.py. Per-lane allowlists still
decide who sees what. Round 23 adds the STANDING AGENDA tools - her own
persistent task queue (tools_agenda.py).
"""
from __future__ import annotations

import json
import os
import time

from .. import brain_recall as brain_recall_mod
from ..ledger import Ledger, NOTE, ProposalIndex
from .registry import ToolRegistry, ToolSpec
from .tools_agenda import register_agenda_tools
from .tools_capability import register_capability_tools
from .tools_connections import register_connection_tools
from .tools_generation import register_generation_tools
from .tools_jobs import register_job_tools
from .tools_decisions import register_decision_tools

SKIP_DIRS = brain_recall_mod.SKIP_DIRS          # back-compat alias


def _brain_recall(brain_dir: str, args: dict) -> str:
    return brain_recall_mod.search(
        brain_dir, str(args.get("query", "")),
        max_results=args.get("max_results", brain_recall_mod.DEFAULT_MAX_FILES))


def build_default_registry(ledger: Ledger, proposals: ProposalIndex,
                           outbox_path: str, brain_dir: str | None = None,
                           workspace_dir: str | None = None,
                           spend_meter=None) -> ToolRegistry:
    reg = ToolRegistry()

    if spend_meter is not None:                # round 15: the wallet, honestly
        reg.register(ToolSpec(
            name="spend_status",
            description=("How much money Minerva has spent in the last 24h "
                         "vs the operator's daily budget, and what is left. Over-"
                         "budget calls escalate to the operator automatically."),
            input_schema={"type": "object", "properties": {}},
            handler=lambda a: spend_meter.status_text()))

    reg.register(ToolSpec(
        name="ping",
        description="Gateway health check; echoes back.",
        input_schema={"type": "object", "properties": {"echo": {"type": "string"}}},
        handler=lambda a: f"pong {a.get('echo', '')}".strip()))

    if os.environ.get("MINERVA_ENABLE_TEST_TOOLS") == "1":
        # Exercise tool for the approval flow ONLY (env-gated; never registered
        # in production - the global Hermes registration does not set this env).
        reg.register(ToolSpec(
            name="test_spend",
            description="(test) Pretend purchase; exists to exercise ask->approve->retry.",
            input_schema={"type": "object",
                          "properties": {"item": {"type": "string"}}},
            classes={"spend"},
            est_cost_usd=9.99,
            handler=lambda a: f"test purchase complete: {a.get('item', 'thing')}"))

    if brain_dir:
        reg.register(ToolSpec(
            name="brain_recall",
            description=("Search the operator's Brain (his markdown memory) for facts "
                         "about him - preferences, people, projects, health, "
                         "history. Manifest-routed keyword recall with file+line "
                         "citations. STRICTLY READ-ONLY and size-capped. Use it "
                         "before assuming anything about the operator."),
            input_schema={"type": "object",
                          "properties": {"query": {"type": "string"},
                                         "max_results": {"type": "integer"}},
                          "required": ["query"]},
            handler=lambda a: _brain_recall(brain_dir, a)))

    reg.register(ToolSpec(
        name="episodic_note",
        description=("Record an observation/fact candidate into Minerva's episodic ledger. "
                     "Append-only; consolidation reviews it later. NOT direct Brain write."),
        input_schema={"type": "object",
                      "properties": {"text": {"type": "string"},
                                     "about": {"type": "string"}},
                      "required": ["text"]},
        handler=lambda a: "noted ({})".format(
            ledger.append(NOTE, {"text": str(a.get("text", ""))[:2000],
                                 "about": a.get("about", ""),
                                 "source": "engine"})["id"])))

    def _pitch(a: dict) -> str:
        try:
            e = proposals.pitch(str(a.get("domain", "general")),
                                str(a.get("title", "")).strip(),
                                str(a.get("detail", ""))[:2000])
            return f"pitch logged ({e['id']}) - the operator will see it; do not re-pitch."
        except PermissionError as exc:
            return (f"REJECTED by anti-nag ledger: {exc}. Drop this idea unless "
                    "circumstances genuinely change; do not rephrase to retry.")

    reg.register(ToolSpec(
        name="pitch_idea",
        description=("Propose a NEW idea/opportunity to the operator. The proposal ledger "
                     "enforces novelty, cooldowns, and budgets - rejections are final "
                     "answers, not obstacles."),
        input_schema={"type": "object",
                      "properties": {"domain": {"type": "string"},
                                     "title": {"type": "string"},
                                     "detail": {"type": "string"}},
                      "required": ["domain", "title"]},
        handler=_pitch))

    def _notify(a: dict) -> str:
        with open(outbox_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": time.time(),
                                "message": str(a.get("message", ""))[:4000],
                                "priority": a.get("priority", "normal")},
                               ensure_ascii=False) + "\n")
        return "queued for delivery to the operator"

    reg.register(ToolSpec(
        name="notify_operator",
        description="Send the operator a message (queued to his channel). For pitches use pitch_idea.",
        input_schema={"type": "object",
                      "properties": {"message": {"type": "string"},
                                     "priority": {"type": "string",
                                                  "enum": ["low", "normal", "high"]}},
                      "required": ["message"]},
        handler=_notify))

    if workspace_dir:
        def _send_file(a: dict) -> str:        # round 19: files TO the operator
            from .tools_capability import _resolve
            rel = str(a.get("path", "")).strip()
            if not rel:
                return "no path given"
            try:
                p = _resolve(workspace_dir, rel)
            except ValueError as e:
                return f"refused: {e}"
            if not os.path.isfile(p):
                return f"no such workspace file: {rel}"
            try:
                cap = int(os.environ.get("MINERVA_DM_FILE_CAP", "").strip()
                          or 8 * 1024 * 1024)
            except ValueError:
                cap = 8 * 1024 * 1024
            size = os.path.getsize(p)
            if size > cap:
                return (f"refused: {rel} is {size} bytes - over the {cap}-byte "
                        "Discord attachment cap; make a smaller version")
            with open(outbox_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"ts": time.time(),
                                    "file": rel.replace("\\", "/"),
                                    "message": str(a.get("note", ""))[:1800],
                                    "priority": a.get("priority", "normal")},
                                   ensure_ascii=True) + "\n")
            return f"queued for Discord delivery to the operator: {rel}"

        reg.register(ToolSpec(
            name="send_file_to_operator",
            description=("Send the operator a FILE from your workspace as a real "
                         "Discord attachment - e.g. an image or video you "
                         "just generated (pass the exact path the generation "
                         "tool returned). Optional note rides with it. "
                         "Size-capped; the host daemon delivers it."),
            input_schema={"type": "object",
                          "properties": {"path": {"type": "string"},
                                         "note": {"type": "string"}},
                          "required": ["path"]},
            classes={"send_file"},
            handler=_send_file))

        register_capability_tools(reg, workspace_dir)
        # round 13: governed image/video generation (Venice-backed). Always
        # registered with a workspace; handlers refuse plainly when the key
        # is missing, so the registry is identical across machines.
        register_generation_tools(reg, workspace_dir, spend_meter=spend_meter)

    # round 23 (standing agenda): her persistent between-ticks task queue.
    # The path rides next to the outbox (same state dir), so every caller
    # of this builder gets it without a signature change.
    register_agenda_tools(reg, os.path.join(
        os.path.dirname(outbox_path) or ".", "agenda.jsonl"))

    # round 26 (multi-agent orchestration): her JOB QUEUE. Same state dir as
    # the outbox/agenda; the orchestrator daemon drains it. job_submit is the
    # decomposition primitive - a job's session can submit sub-jobs.
    register_job_tools(reg, os.path.join(
        os.path.dirname(outbox_path) or ".", "jobs.jsonl"))

    # round 14 (connection_agency): SHE identifies missing connections and
    # asks the operator. Metadata tools always on; email/calendar content tools
    # register only when their host-side config exists.
    register_connection_tools(reg, proposals)

    # round 27: chat decision tools - Minerva records the operator's verdicts /
    # approvals when he states them in conversation. Reuses the gateway's
    # decision files in the same state dir; CHAT-lane allowlist only.
    _sd = os.path.dirname(outbox_path) or "."
    register_decision_tools(reg, ledger, proposals,
                            os.path.join(_sd, "verdicts.jsonl"),
                            os.path.join(_sd, "approvals.jsonl"))

    return reg
