"""Adapter #2: frontier lane — direct Anthropic Messages API, BYOK.

The operator's call (2026-06-11): direct API keys over aggregator portals (cheaper,
no third-party guardrail layer; "was my plan from the beginning"). Model
shortlist still open — DEFAULT_MODEL below is a placeholder, override per
session via SessionConfig.model.

ARCHITECTURE — the loop lives HERE: the Messages API has no server-side agent
loop (that was Hermes's job on lane #1), so this adapter runs it: call model
-> on tool_use, execute against OUR MCP gateway (spawned as a child, stdio)
-> feed tool_result back -> repeat until end_turn. The gateway stays the only
trust boundary and the only ledger appender; this adapter NEVER touches the
ledger itself. Unlike Hermes 0.16.0, per-session tool allowlists genuinely
work on this lane (we spawn the gateway, cfg.tools rides MINERVA_TOOLS), and
persona injection works (cfg.persona -> system prompt).

SINGLE-APPENDER (standing rule): each session spawns ONE gateway against
--state-dir. Never run two engine lanes (or two BYOK sessions) against the
same state dir at once — concurrent gateways fork the hash chain.

Auth: ANTHROPIC_API_KEY env var, or config/anthropic.env (KEY=VALUE lines,
gitignored — same pattern as discord.env). The key is never written to wire
logs (bodies only, never headers).

v0 scope: non-streaming (one MESSAGE per model call; TEXT_DELTA per text
block), THOUGHT from thinking blocks when present, COST per API call
(tokens + real usd — MODEL_PRICES filled 2026-06-11, shortlist landed),
one polite retry on 429/5xx. SSE streaming: later.
Tool names are the RAW gateway names (ping, pitch_idea, ...) — no
mcp_minerva_ prefix; prompts written for the Hermes lane must adjust.

Windows hardening (banked rules): gateway child pipes are BINARY, requests
encoded ensure_ascii, replies decoded utf-8/replace, stderr drained to a
bounded deque. HTTPS body is UTF-8 JSON (not a console pipe; safe).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import subprocess
import sys
from collections import deque
from queue import Empty, Queue
from typing import Callable, Iterator, Sequence

from ..brain_recall import DEFAULT_BRAIN
from ..engine import SessionConfig
from ..events import EngineEvent, EventType

WireLog = Callable[[str, dict], None]

API_URL = "https://api.anthropic.com/v1/messages"   # tests point this at a mock
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"   # = the "standard" tier (shortlist below)
DEFAULT_MAX_TOKENS = 4096
MAX_TOOL_ROUNDS = 8                   # runaway-loop guard per turn
RESULT_CAP = 20_000

# SHORTLIST (decided 2026-06-11; prices verified same day via docs.claude.com
# /platform pricing page + coverage). Per-MTok (input_usd, output_usd). Prices
# DRIFT — re-verify when adding models or if COST events look off.
#   light    claude-haiku-4-5-20251001   $1 / $5    ticks, consolidation, chatter
#   standard claude-sonnet-4-6           $3 / $15   day-to-day agent work (default)
#   heavy    claude-opus-4-8             $5 / $25   deep reasoning, big decisions
#   max      claude-fable-5              $10 / $50  top shelf — opt-in only (2x opus)
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
}

# RoutingHints.capability -> model (the BYOK half of the broker; the registry
# half picks the ENGINE — see EngineRegistry.route()).
MODEL_SHORTLIST = {
    "light": "claude-haiku-4-5-20251001",
    "standard": "claude-sonnet-4-6",
    "heavy": "claude-opus-4-8",
    "max": "claude-fable-5",
}


def model_for(capability) -> str:
    """Capability tier (str or RoutingHints) -> shortlist model.
    Unknown/empty tiers land on 'standard' — never on the priciest model."""
    tier = getattr(capability, "capability", capability)
    return MODEL_SHORTLIST.get(str(tier or "").strip().lower(),
                               MODEL_SHORTLIST["standard"])


def price_for(model: str) -> tuple[float, float]:
    """Per-MTok (input, output) for a model id; (0, 0) when unknown so COST
    events degrade to token-only. Bidirectional prefix match: a dated
    response id finds its alias price ('claude-sonnet-4-6-2025xxxx' ->
    'claude-sonnet-4-6') and an alias finds its dated key
    ('claude-haiku-4-5' -> 'claude-haiku-4-5-20251001')."""
    m = (model or "").strip()
    if not m:
        return (0.0, 0.0)
    hit = MODEL_PRICES.get(m)
    if hit:
        return hit
    for k, v in MODEL_PRICES.items():
        if m.startswith(k) or k.startswith(m):
            return v
    return (0.0, 0.0)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
ANTHROPIC_ENV = os.path.join(ROOT, "config", "anthropic.env")


def _log(cb: WireLog | None, direction: str, frame: dict) -> None:
    if cb is None:
        return
    try:
        cb(direction, frame)
    except Exception:                                          # noqa: BLE001
        pass


def load_api_key(env_path: str | None = None) -> str | None:
    """ANTHROPIC_API_KEY from the environment, else config/anthropic.env."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    path = env_path or ANTHROPIC_ENV
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "ANTHROPIC_API_KEY" and v.strip():
                return v.strip()
    return None


# ---------- minimal MCP client for OUR gateway (stdio child) -----------------
TOOL_TIMEOUT_DEFAULT = 60.0


def tool_call_timeout(name: str) -> float:
    """Per-tool RPC allowance. generate_video legitimately polls for minutes
    (its own deadline = MINERVA_VIDEO_TIMEOUT_S, default 600s) - the client
    must OUTLAST it so the tool's honest timeout message (with the resume
    queue_id) can come back, instead of an RPC TimeoutError that strands the
    job (live lesson 2026-06-12: a $0.57 video was queued and its id lost
    when the 60s client gave up mid-poll). generate_image can take >60s on
    slow/tiered models too (36.9s seen live on seedream-v4)."""
    if name == "generate_video":
        try:
            inner = float(os.environ.get("MINERVA_VIDEO_TIMEOUT_S", "").strip()
                          or 600.0)
        except ValueError:
            inner = 600.0
        return inner + 60.0
    if name == "generate_image":
        return 240.0
    return TOOL_TIMEOUT_DEFAULT


class GatewayClient:
    """Blocking JSON-RPC over the gateway child's stdio. The gateway is
    strictly request->response (no notifications out), so one reader thread
    feeding a queue + per-call timeout is all the routing we need."""

    def __init__(self, argv: Sequence[str], extra_env: dict | None = None) -> None:
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, creationflags=flags)
        self.stderr_tail: deque[str] = deque(maxlen=100)
        self._q: Queue = Queue()
        self._next_id = 0
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _read_loop(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                self._q.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self._q.put(None)                                  # EOF sentinel

    def _drain_stderr(self) -> None:
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_tail.append(line)

    def rpc(self, method: str, params: dict | None = None,
            timeout: float = 30.0) -> dict:
        self._next_id += 1
        rid = self._next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method,
               "params": params or {}}
        data = (json.dumps(msg, ensure_ascii=True) + "\n").encode("ascii")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while True:
            try:
                resp = self._q.get(timeout=max(0.05, deadline - time.time()))
            except Empty:
                raise TimeoutError(f"gateway: no response to {method} in {timeout}s")
            if resp is None:
                raise RuntimeError("gateway died: "
                                   + " | ".join(list(self.stderr_tail)[-3:]))
            if resp.get("id") == rid:
                if "error" in resp:
                    raise RuntimeError(f"gateway {method}: {resp['error']}")
                return resp.get("result") or {}
            # stale/unmatched frame: keep waiting (gateway sends no notifications)

    def notify(self, method: str) -> None:
        data = (json.dumps({"jsonrpc": "2.0", "method": method},
                           ensure_ascii=True) + "\n").encode("ascii")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def initialize(self) -> dict:
        out = self.rpc("initialize", {"protocolVersion": "2025-06-18",
                                      "clientInfo": {"name": "minerva-byok",
                                                     "version": "0.1"}})
        self.notify("notifications/initialized")
        return out

    def list_tools(self) -> list[dict]:
        return (self.rpc("tools/list") or {}).get("tools", [])

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Returns (text, is_error) — the gateway's governance verdicts ride
        in the text (ESCALATED/DENIED/STILL AWAITING) with isError=True."""
        out = self.rpc("tools/call", {"name": name, "arguments": args},
                       timeout=tool_call_timeout(name))
        parts = [c.get("text", "") for c in out.get("content", [])
                 if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(parts)[:RESULT_CAP], bool(out.get("isError"))

    def close(self) -> None:
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except OSError:
            pass


# ---------- the adapter -------------------------------------------------------
class AnthropicByokAdapter:
    name = "anthropic-byok"

    def __init__(self, api_key: str | None = None,
                 gateway_argv: Sequence[str] | None = None,
                 state_dir: str | None = None,
                 model: str = DEFAULT_MODEL,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 api_url: str | None = None,
                 wire_log: WireLog | None = None) -> None:
        self.api_key = api_key or load_api_key()
        if not self.api_key:
            raise RuntimeError("no API key: set ANTHROPIC_API_KEY or fill "
                               "config/anthropic.env")
        self.state_dir = state_dir or os.path.join(ROOT, "state")
        if gateway_argv:
            self.gateway_argv = list(gateway_argv)
        else:
            # Round 12: the default lane wires the Brain too, so brain_recall
            # is REAL here (read-only; the chat allowlist decides exposure).
            brain = os.environ.get("MINERVA_BRAIN", "").strip() or DEFAULT_BRAIN
            self.gateway_argv = [
                sys.executable, os.path.join(ROOT, "scripts", "run_gateway.py"),
                "--state-dir", self.state_dir, "--brain", brain]
        self.model = model
        self.max_tokens = max_tokens
        self.api_url = api_url or API_URL
        self.wire_log = wire_log
        self._n_sessions = 0

    def capabilities(self) -> set[str]:
        return {"tools_mcp", "model_per_session", "persona", "frontier"}

    def model_for_hints(self, hints) -> str:
        """Broker hook (EngineRegistry.route): capability tier -> shortlist
        model for this session. Explicit SessionConfig.model still wins."""
        return model_for(hints)

    def create_session(self, cfg: SessionConfig) -> "AnthropicByokSession":
        extra_env = {}
        if cfg.tools:
            extra_env["MINERVA_TOOLS"] = ",".join(cfg.tools)   # REAL allowlisting
        gw = GatewayClient(self.gateway_argv, extra_env=extra_env)
        gw.initialize()
        tools = [{"name": t["name"], "description": t.get("description", ""),
                  "input_schema": t.get("inputSchema") or
                                  {"type": "object", "properties": {}}}
                 for t in gw.list_tools()]
        self._n_sessions += 1
        return AnthropicByokSession(self, gw, f"byok-{self._n_sessions}",
                                    cfg, tools)


class AnthropicByokSession:
    def __init__(self, adapter: AnthropicByokAdapter, gateway: GatewayClient,
                 session_id: str, cfg: SessionConfig, tools: list[dict]) -> None:
        self.adapter = adapter
        self.gateway = gateway
        self.id = session_id
        self.cfg = cfg
        self.tools = tools
        self.messages: list[dict] = []          # conversation state lives host-side
        self.warnings: list[str] = []
        self._cancelled = False

    # ---- HTTP ------------------------------------------------------------
    def _post(self, body: dict) -> tuple[int, dict]:
        """One request + one polite retry on 429/5xx. The key never enters
        wire logs (bodies only, never headers)."""
        data = json.dumps(body, ensure_ascii=True).encode("ascii")
        _log(self.adapter.wire_log, "send", body)
        for attempt in (1, 2):
            req = urllib.request.Request(self.adapter.api_url, data=data,
                                         method="POST", headers={
                "x-api-key": self.adapter.api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    out = json.loads(resp.read().decode("utf-8", "replace"))
                    _log(self.adapter.wire_log, "recv", out)
                    return resp.status, out
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    out = json.loads(raw)
                except json.JSONDecodeError:
                    out = {"error": {"message": raw[:300]}}
                _log(self.adapter.wire_log, "recv-error",
                     {"status": e.code, "body": out})
                if e.code in (429, 500, 502, 503, 529) and attempt == 1:
                    time.sleep(min(float(e.headers.get("retry-after") or 2.0), 15.0))
                    continue
                return e.code, out
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
                _log(self.adapter.wire_log, "recv-error", {"exception": repr(e)})
                if attempt == 1:
                    time.sleep(2.0)
                    continue
                return 0, {"error": {"message": repr(e)}}
        return 0, {"error": {"message": "unreachable"}}

    # ---- the turn loop -----------------------------------------------------
    def send(self, text: str) -> Iterator[EngineEvent]:
        sid = self.id
        self._cancelled = False
        self.messages.append({"role": "user", "content": text})
        assembled: list[str] = []

        for _round in range(MAX_TOOL_ROUNDS + 1):
            if self._cancelled:
                yield EngineEvent(EventType.DONE, sid, {"stop_reason": "cancelled"})
                return
            body = {"model": self.cfg.model or self.adapter.model,
                    "max_tokens": self.adapter.max_tokens,
                    "messages": self.messages,
                    "tools": self.tools}
            if self.cfg.persona:
                body["system"] = self.cfg.persona      # works on THIS lane
            status, resp = self._post(body)
            if status != 200:
                msg = ((resp.get("error") or {}).get("message", "")
                       or f"http {status}")
                yield EngineEvent(EventType.ERROR, sid,
                                  {"message": f"api error: {msg}"[:500],
                                   "fatal": True})
                yield EngineEvent(EventType.DONE, sid, {"stop_reason": "error"})
                return

            usage = resp.get("usage") or {}
            model = resp.get("model", "")
            inp, out_t = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            pin, pout = price_for(model)
            yield EngineEvent(EventType.COST, sid, {
                "input_tokens": inp, "output_tokens": out_t,
                "cached_read_tokens": usage.get("cache_read_input_tokens", 0),
                "usd": round((inp * pin + out_t * pout) / 1_000_000, 6)})

            content = resp.get("content") or []
            tool_uses = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    t = block.get("text", "")
                    assembled.append(t)
                    yield EngineEvent(EventType.TEXT_DELTA, sid, {"text": t})
                elif btype == "thinking":
                    yield EngineEvent(EventType.THOUGHT, sid,
                                      {"text": block.get("thinking", "")})
                elif btype == "tool_use":
                    tool_uses.append(block)

            stop = resp.get("stop_reason", "")
            if stop != "tool_use" or not tool_uses:
                full = "".join(assembled)
                if full:
                    yield EngineEvent(EventType.MESSAGE, sid, {"text": full})
                yield EngineEvent(EventType.DONE, sid, {"stop_reason": stop})
                return

            # ---- execute tool calls via OUR gateway, feed results back ----
            self.messages.append({"role": "assistant", "content": content})
            results = []
            for tu in tool_uses:
                call_id = tu.get("id", "")
                name = tu.get("name", "")
                args = tu.get("input") or {}
                yield EngineEvent(EventType.TOOL_CALL, sid,
                                  {"tool": name, "args": args, "call_id": call_id})
                try:
                    out_text, is_err = self.gateway.call(name, args)
                except (TimeoutError, RuntimeError, OSError) as exc:
                    out_text, is_err = f"gateway failure: {exc}", True
                yield EngineEvent(EventType.TOOL_RESULT, sid,
                                  {"call_id": call_id, "ok": not is_err,
                                   "result": out_text})
                if out_text.startswith("ESCALATED to the operator"):
                    ref = ""
                    if "(ref " in out_text:
                        ref = out_text.split("(ref ", 1)[1].split(")", 1)[0]
                    yield EngineEvent(EventType.ASK_USER, sid, {
                        "reason": "governance escalation",
                        "question": out_text[:300], "ref": ref})
                results.append({"type": "tool_result", "tool_use_id": call_id,
                                "content": [{"type": "text", "text": out_text}],
                                "is_error": is_err})
            self.messages.append({"role": "user", "content": results})

        yield EngineEvent(EventType.ERROR, sid,
                          {"message": f"tool-round cap hit ({MAX_TOOL_ROUNDS}); "
                           "aborting turn", "fatal": False})
        yield EngineEvent(EventType.DONE, sid, {"stop_reason": "tool_round_cap"})

    def cancel(self) -> None:
        self._cancelled = True                  # takes effect between rounds

    def close(self) -> None:
        self.gateway.close()
