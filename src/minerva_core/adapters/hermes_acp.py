"""Adapter #1, transport 2: Hermes via ACP (`hermes acp` — JSON-RPC 2.0, NDJSON over stdio).

Replaces the PARKED HTTP adapter (adapters/hermes.py): Hermes 0.16.0 has no
/v1/runs surface. Contract source for everything in this file:
state/acp-trace.jsonl + state/acp-report.json, captured live on the host
2026-06-11 against hermes-agent 0.16.0. The _translate pattern carries over
from the parked adapter: translation lives in one function so contract drift
costs one edit.

OBSERVED (hard contract; round-6 trace + round-7 governed-tool probe 2026-06-11):
  initialize       -> protocolVersion 1, agentInfo, sessionCapabilities {fork,list,resume}
  session/new      -> params {cwd, mcpServers[]}; result sessionId (key "sessionId"),
                      models{availableModels,currentModelId}, modes, _meta.hermes.sessionProvenance
  session/prompt   -> params {sessionId, prompt:[{type:"text",text}]}
  session/update   -> params {sessionId, update{sessionUpdate}}; kinds observed:
                      agent_message_chunk, agent_thought_chunk (content {type:"text",text}),
                      usage_update {size,used}  (context-window fill, NOT tokens),
                      available_commands_update {availableCommands},
                      tool_call {toolCallId, title, kind, rawInput, content[], locations[]
                                 -- NO status on the initial frame},
                      tool_call_update {toolCallId, status:"completed", kind, content[]
                                 -- NO rawOutput observed; result text rides in nested
                                 content blocks {type:"content", content:{type:"text",text}}}
  prompt result    -> {stopReason:"end_turn", usage:{cachedReadTokens,inputTokens,
                      outputTokens,thoughtTokens,totalTokens}}  -- TOKEN-ONLY, no USD
  session/cancel   -> notification {sessionId}
  session/set_model-> {sessionId, modelId} returns {} on success (verified live as a
                      no-op switch to the current :free model; CROSS-model switch
                      still unexercised)
  mcpServers[]     -> entry {name, command, args[], env:[{name,value}]} accepted;
                      server is spawned and registers (per-session allowlist via
                      MINERVA_TOOLS env worked: 1 tool registered vs global 4)
  framing          -> newline-delimited JSON (no Content-Length headers)

LIVE FINDING (0.16.0, probe round 7): a session/new mcpServers[] server REGISTERS
(stderr confirms spawn + tool count) but its tools are NOT exposed to the model's
toolset — the model saw only the four tools of the GLOBAL config-registered
'minerva' server and called ping there (governed round-trip still proven: same
gateway, main ledger). Until Hermes exposes per-session servers to the model,
sensitivity routing via cfg.tools must ride the GLOBAL registration (per-process
MINERVA_TOOLS), not per-session servers. Re-probe on any engine upgrade.

CONTRACT-TODO (still unobserved live):
  * session/request_permission agent->client request (we auto-DENY: the trust
    boundary is OUR gateway, never engine-side approval UX). Nothing triggered
    an ask in round 7 — approvals stay manual engine-side.
  * tool_call_update in-progress/failed statuses; rawOutput field (tolerated if
    it ever appears; content blocks are the observed path)
  * stopReason values other than "end_turn" (spec: cancelled, max_tokens, refusal)
  * persona injection: session/new accepts only cwd+mcpServers — no system-prompt
    field exists on this transport. Hermes persona is engine-side config; a
    cfg.persona lands in session.warnings instead of being silently dropped.

Windows hardening (banked rules, 2026-06-10): pipes are BINARY — we encode
ensure_ascii JSON out (pure ASCII bytes, cp1252-proof in any console host) and
decode utf-8/errors=replace in. stderr is drained on a thread into a bounded
deque: Hermes logs heavily and an undrained pipe deadlocks the child.

Hermes-side config assumed (Engine-Interface spec §5, verified live round 6):
memory/user_profile/curator OFF, builtin toolsets disabled except todo+clarify,
approvals manual, minerva gateway as the sole tool source, pin 0.16.0.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from queue import Empty, Queue
from typing import Callable, Iterator, Sequence

from ..engine import SessionConfig
from ..events import EngineEvent, EventType

WireLog = Callable[[str, dict], None]      # (direction, frame) — must not raise


def _log(cb: WireLog | None, direction: str, frame: dict) -> None:
    if cb is None:
        return
    try:
        cb(direction, frame)
    except Exception:                                          # noqa: BLE001
        pass                       # tracing must never take down the transport


class AcpClient:
    """JSON-RPC 2.0 over a child process's stdio, NDJSON framing.

    One reader thread routes traffic three ways:
      responses      -> per-id: either a routed session queue (prompts, so
                        ordering vs updates is preserved) or a blocking slot
      notifications  -> session/update routed to the session's queue by sessionId
      agent requests -> answered centrally (deny-by-default), then surfaced on
                        the session queue so the host sees an ASK_USER event
    """

    def __init__(self, argv: Sequence[str], cwd: str | None = None,
                 wire_log: WireLog | None = None) -> None:
        self.wire_log = wire_log
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(
            list(argv), cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=flags)
        self.stderr_tail: deque[str] = deque(maxlen=200)
        self._next_id = 0
        self._write_lock = threading.Lock()
        self._route_lock = threading.Lock()
        self._pending: dict[int, list] = {}          # id -> [slot, threading.Event]
        self._id_routes: dict[int, Queue] = {}       # id -> session queue (prompts)
        self._session_queues: dict[str, Queue] = {}  # sessionId -> queue
        self._closed = False
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    # ---- outbound ------------------------------------------------------
    def _write(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=True) + "\n").encode("ascii")
        with self._write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        _log(self.wire_log, "send", msg)

    def request(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        """Blocking request; returns the full response frame (result OR error)."""
        with self._route_lock:
            self._next_id += 1
            rid = self._next_id
            slot: list = [None, threading.Event()]
            self._pending[rid] = slot
        self._write({"jsonrpc": "2.0", "method": method, "params": params, "id": rid})
        if not slot[1].wait(timeout):
            with self._route_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"no response to {method} (id={rid}) in {timeout}s")
        return slot[0]

    def request_routed(self, method: str, params: dict, q: Queue) -> int:
        """Send a request whose response is posted to q as ("response", frame),
        interleaved in arrival order with that session's updates."""
        with self._route_lock:
            self._next_id += 1
            rid = self._next_id
            self._id_routes[rid] = q
        self._write({"jsonrpc": "2.0", "method": method, "params": params, "id": rid})
        return rid

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, rid, result: dict | None = None, error: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        self._write(msg)

    # ---- session queue registry -----------------------------------------
    def register_session(self, session_id: str) -> Queue:
        q: Queue = Queue()
        with self._route_lock:
            self._session_queues[session_id] = q
        return q

    def unregister_session(self, session_id: str) -> None:
        with self._route_lock:
            self._session_queues.pop(session_id, None)

    # ---- inbound ---------------------------------------------------------
    def _read_loop(self) -> None:
        stream = self.proc.stdout
        while True:
            raw = stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                _log(self.wire_log, "recv-garbled", {"_raw": line})
                continue
            _log(self.wire_log, "recv", msg)
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:
                self._handle_agent_request(msg)
            elif "method" in msg:
                self._handle_notification(msg)
            elif "id" in msg:
                self._handle_response(msg)
        self._closed = True
        with self._route_lock:
            queues = list(self._session_queues.values())
            pendings = list(self._pending.values())
            routed = list(self._id_routes.values())
            self._pending.clear()
            self._id_routes.clear()
        for slot in pendings:                       # unblock blocking requests
            slot[0] = {"error": {"code": -1, "message": "acp stream closed"}}
            slot[1].set()
        for q in {id(x): x for x in queues + routed}.values():
            q.put(("eof", None))

    def _handle_response(self, msg: dict) -> None:
        rid = msg.get("id")
        with self._route_lock:
            q = self._id_routes.pop(rid, None)
            slot = self._pending.pop(rid, None) if q is None else None
        if q is not None:
            q.put(("response", msg))
        elif slot is not None:
            slot[0] = msg
            slot[1].set()

    def _handle_notification(self, msg: dict) -> None:
        if msg.get("method") != "session/update":
            return                                   # unknown notifications: wire_log only
        params = msg.get("params") or {}
        sid = params.get("sessionId")
        with self._route_lock:
            q = self._session_queues.get(sid)
        if q is not None:
            q.put(("update", params.get("update") or {}))

    def _handle_agent_request(self, msg: dict) -> None:
        """Agent->client requests. Deny-by-default: Hermes-side approval UX is
        explicitly NOT our trust boundary (the gateway is), and fs is declared
        unsupported at initialize. CONTRACT-TODO: request_permission shape is
        spec-derived, unobserved live."""
        method, rid = msg.get("method", ""), msg.get("id")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            options = params.get("options") or []
            deny = next((o for o in options
                         if str(o.get("kind", "")).startswith("reject")), None)
            if deny is None:
                deny = next((o for o in options
                             if "cancel" in str(o.get("kind", ""))), None)
            if deny is not None:
                outcome = {"outcome": "selected", "optionId": deny.get("optionId")}
            else:
                outcome = {"outcome": "cancelled"}
            self.respond(rid, {"outcome": outcome})
        else:                                        # fs/*, terminal/*, anything else
            self.respond(rid, error={"code": -32601,
                                     "message": f"{method} not supported by minerva host"})
        sid = params.get("sessionId")
        with self._route_lock:
            q = self._session_queues.get(sid)
        if q is not None:
            q.put(("agent_request", {"method": method, "params": params,
                                     "auto_denied": True}))

    def _drain_stderr(self) -> None:
        stream = self.proc.stderr
        while True:
            raw = stream.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_tail.append(line)
                _log(self.wire_log, "stderr", {"_raw": line})

    def close(self) -> None:
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except OSError:
            pass


class HermesAcpAdapter:
    name = "hermes-acp"

    def __init__(self, hermes_cmd: Sequence[str] | None = None,
                 cwd: str | None = None,
                 mcp_servers: Sequence[dict] | None = None,
                 timeout: float = 300.0,
                 wire_log: WireLog | None = None) -> None:
        self.hermes_cmd = list(hermes_cmd) if hermes_cmd else None
        self.cwd = cwd
        self.mcp_servers = [dict(s) for s in (mcp_servers or [])]
        self.timeout = timeout
        self.wire_log = wire_log
        self._client: AcpClient | None = None
        self._client_lock = threading.Lock()
        self.agent_info: dict = {}

    def capabilities(self) -> set[str]:
        # fork/list/resume observed in initialize; model_per_session: models list
        # per session observed + set_model verified live (no-op switch).
        return {"stream", "tools_mcp", "fork", "model_per_session"}

    def client(self) -> AcpClient:
        with self._client_lock:
            if self._client is None or self._client._closed:
                argv = self.hermes_cmd or [shutil.which("hermes") or "hermes", "acp"]
                c = AcpClient(argv, cwd=self.cwd, wire_log=self.wire_log)
                resp = c.request("initialize", {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False,
                                                  "writeTextFile": False}},
                    "clientInfo": {"name": "minerva-host", "version": "0.1"},
                }, timeout=60.0)
                if "error" in resp:
                    c.close()
                    raise RuntimeError(f"ACP initialize failed: {resp['error']}")
                self.agent_info = (resp.get("result") or {}).get("agentInfo", {})
                self._client = c
            return self._client

    def _session_mcp_servers(self, cfg: SessionConfig) -> list[dict]:
        """Per-session gateway copies. cfg.tools (the gateway allowlist) rides in
        as MINERVA_TOOLS env on any server whose name contains 'minerva' — same
        gateway, narrower tool surface. Env [{name,value}] form VERIFIED live
        2026-06-11 (server spawned, allowlist applied: 1 tool registered). BUT see
        header LIVE FINDING: in 0.16.0 the registered per-session tools don't
        reach the model's toolset — until that changes, treat this lane as
        registration-only and do real allowlisting on the GLOBAL registration."""
        servers = copy.deepcopy(self.mcp_servers)
        if cfg.tools:
            allow = ",".join(cfg.tools)
            for s in servers:
                if "minerva" not in str(s.get("name", "")):
                    continue
                env = s.setdefault("env", [])
                if isinstance(env, dict):
                    env["MINERVA_TOOLS"] = allow
                else:
                    env[:] = [e for e in env if e.get("name") != "MINERVA_TOOLS"]
                    env.append({"name": "MINERVA_TOOLS", "value": allow})
        return servers

    def create_session(self, cfg: SessionConfig) -> "HermesAcpSession":
        c = self.client()
        resp = c.request("session/new", {
            "cwd": self.cwd or os.getcwd(),
            "mcpServers": self._session_mcp_servers(cfg),
        }, timeout=120.0)
        if "error" in resp:
            raise RuntimeError(f"session/new failed: {resp['error']}")
        result = resp.get("result") or {}
        sid = result.get("sessionId")                # key observed live
        if not sid:
            raise RuntimeError(f"no sessionId in session/new result: {result}")
        sess = HermesAcpSession(self, c, str(sid), cfg)
        sess.models = result.get("models") or {}
        sess.modes = result.get("modes") or {}
        sess.provenance = ((result.get("_meta") or {}).get("hermes") or {}) \
            .get("sessionProvenance", {})
        if cfg.persona:
            sess.warnings.append(
                "persona not injectable over ACP (session/new has no system-prompt "
                "field); configure persona engine-side")           # CONTRACT-TODO
        if cfg.model:
            sess._set_model(cfg.model)
        return sess


class HermesAcpSession:
    def __init__(self, adapter: HermesAcpAdapter, client: AcpClient,
                 session_id: str, cfg: SessionConfig) -> None:
        self.adapter = adapter
        self.client = client
        self.id = session_id
        self.cfg = cfg
        self.queue = client.register_session(session_id)
        self.models: dict = {}
        self.modes: dict = {}
        self.provenance: dict = {}
        self.commands: list = []
        self.warnings: list[str] = []
        self.unknown_kinds: set[str] = set()
        self.model_error: str | None = None

    def _set_model(self, model_id: str) -> None:
        """session/set_model VERIFIED live 2026-06-11 (no-op switch to the current
        :free model returned {}). Cross-model switching still unexercised. On error
        we keep the engine default (the :free model) and record why — a failure
        here must never silently route spend to a paid model."""
        try:
            resp = self.client.request("session/set_model",
                                       {"sessionId": self.id, "modelId": model_id},
                                       timeout=30.0)
            if "error" in resp:
                self.model_error = str(resp["error"])
        except (TimeoutError, OSError) as exc:
            self.model_error = str(exc)

    # ---- the turn loop ---------------------------------------------------
    def send(self, text: str) -> Iterator[EngineEvent]:
        sid = self.id
        assembled: list[str] = []
        try:
            rid = self.client.request_routed("session/prompt", {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": text}],
            }, self.queue)
        except (OSError, ValueError) as exc:         # dead pipe at write time
            yield EngineEvent(EventType.ERROR, sid, {"message": str(exc), "fatal": True})
            yield EngineEvent(EventType.DONE, sid, {"stop_reason": "transport_error"})
            return

        deadline = time.time() + self.adapter.timeout
        while True:
            try:
                kind, item = self.queue.get(timeout=1.0)
            except Empty:
                if time.time() > deadline:
                    yield EngineEvent(EventType.ERROR, sid,
                                      {"message": f"turn timeout after {self.adapter.timeout}s",
                                       "fatal": True})
                    yield EngineEvent(EventType.DONE, sid, {"stop_reason": "timeout"})
                    return
                continue

            if kind == "eof":
                yield EngineEvent(EventType.ERROR, sid,
                                  {"message": "acp stream closed mid-turn; stderr tail: "
                                   + " | ".join(list(self.client.stderr_tail)[-3:]),
                                   "fatal": True})
                yield EngineEvent(EventType.DONE, sid, {"stop_reason": "transport_error"})
                return

            if kind == "agent_request":              # answered (denied) centrally
                yield EngineEvent(EventType.ASK_USER, sid, {
                    "reason": item.get("method", ""),
                    "question": json.dumps(item.get("params", {}))[:500],
                    "ref": "", "auto_denied": True})
                continue

            if kind == "update":
                ev = self._translate(item)
                if ev is not None:
                    if ev.type is EventType.TEXT_DELTA:
                        assembled.append(ev.payload.get("text", ""))
                    yield ev
                continue

            if kind == "response":
                msg = item
                if msg.get("id") != rid:             # stale response for this session
                    continue
                if "error" in msg:
                    yield EngineEvent(EventType.ERROR, sid,
                                      {"message": str(msg["error"]), "fatal": True})
                    yield EngineEvent(EventType.DONE, sid, {"stop_reason": "error"})
                    return
                result = msg.get("result") or {}
                usage = result.get("usage") or {}
                if usage:
                    # Observed: token-only, no USD on this transport. Pricing is
                    # host-side; usd stays 0.0 so COST consumers keep their shape.
                    yield EngineEvent(EventType.COST, sid, {
                        "input_tokens": usage.get("inputTokens", 0),
                        "output_tokens": usage.get("outputTokens", 0),
                        "cached_read_tokens": usage.get("cachedReadTokens", 0),
                        "thought_tokens": usage.get("thoughtTokens", 0),
                        "total_tokens": usage.get("totalTokens", 0),
                        "usd": 0.0})
                full = "".join(assembled)
                if full:
                    yield EngineEvent(EventType.MESSAGE, sid, {"text": full})
                yield EngineEvent(EventType.DONE, sid,
                                  {"stop_reason": result.get("stopReason", "")})
                return

    # ---- translation (the one place contract drift costs an edit) --------
    def _translate(self, upd: dict) -> EngineEvent | None:
        sid = self.id
        kind = upd.get("sessionUpdate", "")
        if kind == "agent_message_chunk":
            return EngineEvent(EventType.TEXT_DELTA, sid,
                               {"text": _content_text(upd.get("content"))})
        if kind == "agent_thought_chunk":
            return EngineEvent(EventType.THOUGHT, sid,
                               {"text": _content_text(upd.get("content"))})
        if kind == "usage_update":
            # Observed shape: {size, used} — context-window fill, not tokens.
            return EngineEvent(EventType.COST, sid,
                               {"context_used": upd.get("used", 0),
                                "context_size": upd.get("size", 0)})
        if kind == "available_commands_update":
            self.commands = upd.get("availableCommands") or []
            return None
        if kind == "current_mode_update":            # CONTRACT-TODO unobserved
            self.modes["currentModeId"] = upd.get("currentModeId")
            return None
        if kind == "tool_call":
            # OBSERVED 2026-06-11: {toolCallId, title, kind, rawInput, content[],
            # locations[]} — no status on the initial frame.
            return EngineEvent(EventType.TOOL_CALL, sid, {
                "tool": upd.get("title") or upd.get("kind", ""),
                "args": upd.get("rawInput") or {},
                "call_id": upd.get("toolCallId", ""),
                "status": upd.get("status", "")})
        if kind == "tool_call_update":
            # OBSERVED 2026-06-11: {toolCallId, status:"completed", kind, content[]}.
            # Result text rides in content blocks; rawOutput never observed but
            # preferred if present. "failed" + in-progress ticks: CONTRACT-TODO.
            status = upd.get("status", "")
            if status not in ("completed", "failed"):
                return None                          # in-progress ticks: skip
            result = upd.get("rawOutput")
            if result is None:
                result = _content_text_blocks(upd.get("content"))
            return EngineEvent(EventType.TOOL_RESULT, sid, {
                "call_id": upd.get("toolCallId", ""),
                "ok": status == "completed",
                "result": result})
        if kind == "plan":                           # CONTRACT-TODO unobserved
            return None
        if kind:
            self.unknown_kinds.add(kind)             # probe report mines this
        return None                                  # unknown kinds skipped by design

    def cancel(self) -> None:
        try:
            self.client.notify("session/cancel", {"sessionId": self.id})  # observed
        except OSError:
            pass

    def close(self) -> None:
        # ACP sessions persist engine-side (list/resume/fork observed); nothing
        # to release but our queue registration.
        self.client.unregister_session(self.id)


def _content_text(content) -> str:
    """ContentBlock -> text. Observed: {type:"text", text}. Anything else
    (image etc.) degrades to a tagged stub rather than crashing the stream."""
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text", ""))
        return f"[{content.get('type', 'non-text')}]"
    return "" if content is None else str(content)


def _content_text_blocks(blocks) -> str:
    """tool_call content list -> joined text. OBSERVED shape 2026-06-11:
    [{type:"content", content:{type:"text", text}}]; tolerate plain blocks too."""
    if not isinstance(blocks, list):
        return "" if blocks is None else str(blocks)
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, dict):
            inner = b.get("content") if isinstance(b.get("content"), dict) else b
            t = _content_text(inner)
            if t:
                parts.append(t)
    return "\n".join(parts)
