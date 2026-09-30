"""ACP adapter test against a stdlib fake of the captured wire format.

The fake agent is THIS FILE re-invoked with --fake-agent: a real subprocess
speaking NDJSON JSON-RPC on real pipes, so framing, the reader/drain threads,
and the cp1252-class encoding hardening are exercised end to end (the fake
deliberately replies in raw UTF-8). Frame shapes mirror state/acp-trace.jsonl
and the round-7 governed-tool capture in state/acp-tools-report.json
(2026-06-11).  Run: python tests/test_adapter_hermes_acp.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

UNICODE_TEXT = "héllo — ünïcode ✓"


# ===================== fake hermes-acp agent (subprocess) ====================
def fake_agent() -> None:
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    state = {"sessions": 0, "cancels": 0, "model": "nous:stepfun/step-3.7-flash:free",
             "mcp_servers": None}

    def w(obj: dict) -> None:
        out.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        out.flush()

    def update(sid: str, upd: dict) -> None:
        w({"jsonrpc": "2.0", "method": "session/update",
           "params": {"sessionId": sid, "update": upd}})

    def chunk(sid: str, kind: str, text: str) -> None:
        update(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})

    def end_turn(rid, in_t=100, out_t=42) -> None:
        w({"jsonrpc": "2.0", "id": rid, "result": {
            "stopReason": "end_turn",
            "usage": {"cachedReadTokens": 1280, "inputTokens": in_t,
                      "outputTokens": out_t, "thoughtTokens": 0,
                      "totalTokens": in_t + out_t}}})

    while True:
        raw = inp.readline()
        if not raw:
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        p = msg.get("params") or {}

        if method == "initialize":
            w({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1,
                "agentInfo": {"name": "hermes-agent", "version": "0.16.0-fake"},
                "agentCapabilities": {"loadSession": True,
                                      "sessionCapabilities": {"fork": {}, "list": {},
                                                              "resume": {}}}}})
        elif method == "session/new":
            state["sessions"] += 1
            state["mcp_servers"] = p.get("mcpServers")
            sid = f"fake-sess-{state['sessions']}"
            w({"jsonrpc": "2.0", "id": rid, "result": {
                "sessionId": sid,
                "models": {"availableModels": [],
                           "currentModelId": state["model"]},
                "modes": {"availableModes": [], "currentModeId": "default"},
                "_meta": {"hermes": {"sessionProvenance": {"sessionKind": "root"}},
                          "fake": {"mcpServers": state["mcp_servers"]}}}})
        elif method == "session/set_model":
            if "bad" in p.get("modelId", ""):
                w({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32000, "message": "unknown model"}})
            else:
                state["model"] = p.get("modelId")
                w({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/cancel":             # notification
            state["cancels"] += 1
        elif method == "session/prompt":
            sid = p.get("sessionId", "?")
            text = (p.get("prompt") or [{}])[0].get("text", "")
            update(sid, {"sessionUpdate": "available_commands_update",
                         "availableCommands": [{"name": "help"}]})
            if text == "hello":
                chunk(sid, "agent_thought_chunk", "Think")
                chunk(sid, "agent_thought_chunk", "ing.")
                chunk(sid, "agent_message_chunk", "Hel")
                chunk(sid, "agent_message_chunk", "lo there")
                # shapes below mirror state/acp-tools-report.json, observed 2026-06-11:
                # initial tool_call has NO status; result text rides in content blocks
                update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1",
                             "title": "mcp_minerva_ping", "kind": "other",
                             "locations": [], "rawInput": {"x": 1},
                             "content": [{"type": "content",
                                          "content": {"type": "text",
                                                      "text": "{\"x\": 1}"}}]})
                update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                             "status": "in_progress"})   # CONTRACT-TODO tolerance
                update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                             "status": "completed", "kind": "other",
                             "content": [{"type": "content",
                                          "content": {"type": "text",
                                                      "text": "mcp_minerva_ping result\n- **result:** pong"}}]})
                update(sid, {"sessionUpdate": "mystery_kind_42", "data": 1})
                update(sid, {"sessionUpdate": "usage_update",
                             "size": 256000, "used": 11459})
                end_turn(rid)
            elif text == "ask":
                w({"jsonrpc": "2.0", "id": 901,
                   "method": "session/request_permission",
                   "params": {"sessionId": sid,
                              "toolCall": {"toolCallId": "t9", "title": "rm -rf"},
                              "options": [
                                  {"optionId": "allow-once", "name": "Allow",
                                   "kind": "allow_once"},
                                  {"optionId": "reject-once", "name": "Reject",
                                   "kind": "reject_once"}]}})
                reply = json.loads(inp.readline().decode("utf-8"))
                picked = ((reply.get("result") or {}).get("outcome")
                          or {}).get("optionId", "?")
                chunk(sid, "agent_message_chunk", f"denied:{picked}")
                end_turn(rid)
            elif text == "unicode":
                chunk(sid, "agent_message_chunk", UNICODE_TEXT)
                end_turn(rid)
            elif text == "cancels?":
                chunk(sid, "agent_message_chunk", str(state["cancels"]))
                end_turn(rid)
            elif text == "model?":
                chunk(sid, "agent_message_chunk", state["model"])
                end_turn(rid)
            elif text == "die":
                chunk(sid, "agent_message_chunk", "ugh")
                out.flush()
                os._exit(1)
            else:
                end_turn(rid)


# ================================ the tests =================================
PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def main() -> None:
    from minerva_core.adapters.hermes_acp import HermesAcpAdapter
    from minerva_core.engine import RoutingHints, SessionConfig
    from minerva_core.events import EventType

    fake_cmd = [sys.executable, os.path.abspath(__file__), "--fake-agent"]
    gateway_server = {"name": "minerva", "command": "python",
                      "args": ["run_gateway.py"],
                      "env": [{"name": "MINERVA_STATE_DIR", "value": "state"}]}
    adapter = HermesAcpAdapter(hermes_cmd=fake_cmd, mcp_servers=[gateway_server],
                               timeout=30.0)
    try:
        check("capabilities sane", {"stream", "tools_mcp"} <= adapter.capabilities())

        cfg = SessionConfig(persona="You are Minerva.", model="nous:test/model-x",
                            routing=RoutingHints(), tools=("ping", "pitch_idea"))
        sess = adapter.create_session(cfg)
        check("session created (observed sessionId key)", sess.id == "fake-sess-1")
        check("initialize handshake stored", adapter.agent_info.get("version") == "0.16.0-fake")
        check("set_model accepted", sess.model_error is None)
        check("persona impossibility surfaced, not dropped",
              any("persona" in w for w in sess.warnings))

        # gateway pass-through: per-session mcpServers[] with cfg.tools as env
        # allowlist (the fake echoes what it received via _meta.fake)
        resp = adapter.client().request("session/new", {
            "cwd": os.getcwd(),
            "mcpServers": adapter._session_mcp_servers(cfg)}, timeout=30.0)
        servers = resp["result"]["_meta"]["fake"]["mcpServers"]
        check("gateway passed in session/new mcpServers[]",
              servers and servers[0]["name"] == "minerva"
              and servers[0]["command"] == "python")
        env_pairs = {e["name"]: e["value"] for e in servers[0]["env"]}
        check("cfg.tools rides in as MINERVA_TOOLS allowlist",
              env_pairs.get("MINERVA_TOOLS") == "ping,pitch_idea"
              and env_pairs.get("MINERVA_STATE_DIR") == "state")
        check("adapter's own template not mutated by per-session env injection",
              all(e["name"] != "MINERVA_TOOLS" for e in adapter.mcp_servers[0]["env"]))

        # the full observed-stream translation
        events = list(sess.send("hello"))
        types = [e.type for e in events]
        check("event sequence translated", types == [
            EventType.THOUGHT, EventType.THOUGHT,
            EventType.TEXT_DELTA, EventType.TEXT_DELTA,
            EventType.TOOL_CALL, EventType.TOOL_RESULT,
            EventType.COST, EventType.COST, EventType.MESSAGE, EventType.DONE])
        check("thoughts kept out of the message", events[8].payload["text"] == "Hello there")
        check("deltas assemble", "".join(e.payload["text"] for e in events[2:4]) == "Hello there")
        check("tool call mapped (live-observed shape, no status on initial frame)",
              events[4].payload["tool"] == "mcp_minerva_ping"
              and events[4].payload["args"] == {"x": 1}
              and events[4].payload["call_id"] == "t1"
              and events[4].payload["status"] == "")
        check("tool result from content blocks; in-progress tick skipped",
              events[5].payload["ok"]
              and events[5].payload["result"] == "mcp_minerva_ping result\n- **result:** pong")
        check("context-fill COST tick (observed usage_update shape)",
              events[6].payload["context_used"] == 11459
              and events[6].payload["context_size"] == 256000)
        check("token COST from prompt result (token-only, no USD)",
              events[7].payload["input_tokens"] == 100
              and events[7].payload["output_tokens"] == 42
              and events[7].payload["usd"] == 0.0)
        check("DONE carries stopReason", events[9].payload["stop_reason"] == "end_turn")
        check("session id on every event", all(e.session_id == "fake-sess-1" for e in events))
        check("unknown update kinds collected for the probe report",
              sess.unknown_kinds == {"mystery_kind_42"})
        check("available_commands swallowed + stored",
              sess.commands and sess.commands[0]["name"] == "help")

        # permission request -> auto-deny + ASK_USER surfaced
        events = list(sess.send("ask"))
        ask = [e for e in events if e.type is EventType.ASK_USER]
        check("agent permission request surfaced as ASK_USER (auto-denied)",
              len(ask) == 1 and ask[0].payload["auto_denied"]
              and ask[0].payload["reason"] == "session/request_permission")
        msg = [e for e in events if e.type is EventType.MESSAGE][0]
        check("deny-by-default picked the reject option (engine saw it)",
              msg.payload["text"] == "denied:reject-once")

        # raw UTF-8 from the engine survives our binary pipes (cp1252-class regression)
        events = list(sess.send("unicode"))
        msg = [e for e in events if e.type is EventType.MESSAGE][0]
        check("utf-8 round trip exact", msg.payload["text"] == UNICODE_TEXT)

        # cancel: observed notification shape reaches the engine
        sess.cancel()
        events = list(sess.send("cancels?"))
        msg = [e for e in events if e.type is EventType.MESSAGE][0]
        check("session/cancel notification wired through", msg.payload["text"] == "1")

        # bad model -> error recorded, session lives, engine default retained
        sess_bad = adapter.create_session(SessionConfig(model="nous:bad/model"))
        check("bad model: error recorded, session alive (no silent paid routing)",
              sess_bad.model_error is not None and sess_bad.id == "fake-sess-3")
        events = list(sess_bad.send("model?"))
        msg = [e for e in events if e.type is EventType.MESSAGE][0]
        check("engine default model retained after set_model error",
              msg.payload["text"] == "nous:test/model-x")

        # engine dies mid-turn -> ERROR + DONE, never an exception
        events = list(sess_bad.send("die"))
        types = [e.type for e in events]
        check("mid-turn death -> ...ERROR, DONE",
              types[-2:] == [EventType.ERROR, EventType.DONE]
              and events[-2].payload["fatal"]
              and events[-1].payload["stop_reason"] == "transport_error")

        # adapter respawns a fresh engine process on next create_session
        sess3 = adapter.create_session(SessionConfig())
        check("client respawned after engine death", sess3.id == "fake-sess-1"
              and list(sess3.send("hello"))[-1].type is EventType.DONE)
    finally:
        if adapter._client is not None:
            adapter._client.close()
    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    if "--fake-agent" in sys.argv:
        fake_agent()
    else:
        main()
