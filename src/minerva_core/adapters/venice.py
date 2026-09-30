"""Adapter #3: Venice.ai - the hosted-open, low-restriction lane (round 12).

The operator's model_sourcing tiers: frontier APIs for max intelligence, and
low-restriction lanes where provider guardrails would get in the way.
Venice hosts open-weight models behind an OpenAI-compatible API with a
no-filter posture - that makes it guardrail tier "hosted-open" (between
frontier and self-hosted "local"). The broker routes here ONLY when a job
explicitly asks for guardrail_tier hosted-open/open/uncensored
(EngineRegistry.pick v0.2) - and refuses loudly when this adapter is not
registered, never silently re-routing such a job to a frontier lane.

GOVERNANCE IS OURS, NOT THE PROVIDER'S: like the BYOK lane, the agent loop
lives in this adapter and every tool call executes against OUR gateway
(spawned child, stdio) - GovernanceGate + hash-chained ledger apply to this
lane exactly as to the others. "Uncensored model" never means "ungoverned
actions".

Auth: VENICE_API_KEY env var, or config/venice.env (KEY=VALUE lines,
gitignored - same pattern as anthropic.env/discord.env). STANDING RULE: the
key is filled in HOST-SIDE by the operator and never pasted into a Claude session;
it is never written to wire logs (bodies only, never headers).

API shape (OpenAI-compatible chat completions):
  POST {base}/chat/completions  {model, messages, tools?, max_tokens}
  tool calls ride choices[0].message.tool_calls (function name + JSON-string
  arguments); results return as {"role":"tool","tool_call_id",...} messages.
  GET {base}/models lists model ids (see scripts/probe_venice.py).
COST events are TOKEN-ONLY (usd 0.0): Venice bills in their own credit
system (VCU/USD balances vary by plan) - don't fake a USD number we can't
verify. Revisit when the operator wants real Venice prices wired in.

v0 scope mirrors anthropic_byok v0: non-streaming, MAX_TOOL_ROUNDS guard,
one polite retry on 429/5xx, escalations surfaced as ASK_USER, Windows
cp1252-safe (ASCII request bodies; UTF-8/replace decode).
Stdlib only. Reuses GatewayClient from the BYOK adapter (same child gateway).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Iterator, Sequence

from ..brain_recall import DEFAULT_BRAIN
from ..engine import SessionConfig
from ..events import EngineEvent, EventType
from .anthropic_byok import GatewayClient, ROOT

WireLog = Callable[[str, dict], None]

API_BASE = "https://api.venice.ai/api/v1"     # tests point this at a mock
DEFAULT_MODEL = "venice-uncensored"           # Venice's standing alias; env
                                              # VENICE_MODEL overrides; probe
                                              # lists the real catalog
DEFAULT_MAX_TOKENS = 2048
MAX_TOOL_ROUNDS = 8
RESULT_CAP = 20_000

VENICE_ENV = os.path.join(ROOT, "config", "venice.env")


def _log(cb: WireLog | None, direction: str, frame: dict) -> None:
    if cb is None:
        return
    try:
        cb(direction, frame)
    except Exception:                                          # noqa: BLE001
        pass


def load_venice_var(name: str, env_path: str | None = None) -> str:
    """NAME from the environment, else a NAME=value line in config/venice.env
    (the operator edits that file host-side; MINERVA_VENICE_ENV points tests at a
    different file). Returns '' when unset/empty (placeholder lines)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    path = (env_path or os.environ.get("MINERVA_VENICE_ENV", "").strip()
            or VENICE_ENV)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip()
    return ""


def load_venice_key(env_path: str | None = None) -> str | None:
    """VENICE_API_KEY from the environment, else config/venice.env.
    Empty values (the template placeholder) count as missing."""
    return load_venice_var("VENICE_API_KEY", env_path) or None


def default_model() -> str:
    """The VENICE_MODEL pin - env var, else config/venice.env (round-13 fix:
    the probe always SAID to pin it in the file, but only the env var was
    read, so file pins were silently ignored), else the standing alias."""
    return load_venice_var("VENICE_MODEL") or DEFAULT_MODEL


class VeniceAdapter:
    name = "venice"

    def __init__(self, api_key: str | None = None,
                 gateway_argv: Sequence[str] | None = None,
                 state_dir: str | None = None,
                 model: str | None = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 api_base: str | None = None,
                 wire_log: WireLog | None = None) -> None:
        self.api_key = api_key or load_venice_key()
        if not self.api_key:
            raise RuntimeError("no Venice key: set VENICE_API_KEY or fill "
                               "config/venice.env (host-side only)")
        self.state_dir = state_dir or os.path.join(ROOT, "state")
        if gateway_argv:
            self.gateway_argv = list(gateway_argv)
        else:
            brain = os.environ.get("MINERVA_BRAIN", "").strip() or DEFAULT_BRAIN
            self.gateway_argv = [
                sys.executable, os.path.join(ROOT, "scripts", "run_gateway.py"),
                "--state-dir", self.state_dir, "--brain", brain]
        self.model = model or default_model()
        self.max_tokens = max_tokens
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.wire_log = wire_log
        self._n_sessions = 0

    def capabilities(self) -> set[str]:
        return {"tools_mcp", "model_per_session", "persona",
                "hosted-open", "uncensored", "image_gen", "video_gen"}

    def create_session(self, cfg: SessionConfig) -> "VeniceSession":
        extra_env = {}
        if cfg.tools:
            extra_env["MINERVA_TOOLS"] = ",".join(cfg.tools)   # REAL allowlisting
        gw = GatewayClient(self.gateway_argv, extra_env=extra_env)
        gw.initialize()
        tools = [{"type": "function",
                  "function": {"name": t["name"],
                               "description": t.get("description", ""),
                               "parameters": t.get("inputSchema") or
                                             {"type": "object", "properties": {}}}}
                 for t in gw.list_tools()]
        self._n_sessions += 1
        return VeniceSession(self, gw, f"venice-{self._n_sessions}", cfg, tools)


class VeniceSession:
    def __init__(self, adapter: VeniceAdapter, gateway: GatewayClient,
                 session_id: str, cfg: SessionConfig, tools: list[dict]) -> None:
        self.adapter = adapter
        self.gateway = gateway
        self.id = session_id
        self.cfg = cfg
        self.tools = tools
        self.messages: list[dict] = []
        if cfg.persona:
            self.messages.append({"role": "system", "content": cfg.persona})
        self._cancelled = False

    # ---- HTTP ------------------------------------------------------------
    def _post(self, body: dict) -> tuple[int, dict]:
        """One request + one polite retry on 429/5xx. The key never enters
        wire logs (bodies only, never headers)."""
        data = json.dumps(body, ensure_ascii=True).encode("ascii")
        _log(self.adapter.wire_log, "send", body)
        url = self.adapter.api_base + "/chat/completions"
        for attempt in (1, 2):
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Authorization": f"Bearer {self.adapter.api_key}",
                "Content-Type": "application/json"})
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
                    "messages": self.messages}
            if self.tools:
                body["tools"] = self.tools
            status, resp = self._post(body)
            if status != 200:
                err = resp.get("error")
                msg = (err.get("message", "") if isinstance(err, dict)
                       else str(err or "")) or f"http {status}"
                yield EngineEvent(EventType.ERROR, sid,
                                  {"message": f"api error: {msg}"[:500],
                                   "fatal": True})
                yield EngineEvent(EventType.DONE, sid, {"stop_reason": "error"})
                return

            usage = resp.get("usage") or {}
            yield EngineEvent(EventType.COST, sid, {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "usd": 0.0})                       # token-only (see module doc)

            choice = (resp.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str) and content:
                assembled.append(content)
                yield EngineEvent(EventType.TEXT_DELTA, sid, {"text": content})
            tool_calls = message.get("tool_calls") or []
            finish = choice.get("finish_reason", "")

            if not tool_calls:
                full = "".join(assembled)
                if full:
                    yield EngineEvent(EventType.MESSAGE, sid, {"text": full})
                yield EngineEvent(EventType.DONE, sid,
                                  {"stop_reason": finish or "stop"})
                return

            # ---- execute tool calls via OUR gateway, feed results back ----
            self.messages.append(message)
            for tc in tool_calls:
                call_id = tc.get("id", "")
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                yield EngineEvent(EventType.TOOL_CALL, sid,
                                  {"tool": name, "args": args, "call_id": call_id})
                try:
                    out_text, is_err = self.gateway.call(name, args)
                except (TimeoutError, RuntimeError, OSError) as exc:
                    out_text, is_err = f"gateway failure: {exc}", True
                out_text = out_text[:RESULT_CAP]
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
                self.messages.append({"role": "tool", "tool_call_id": call_id,
                                      "content": out_text})

        yield EngineEvent(EventType.ERROR, sid,
                          {"message": f"tool-round cap hit ({MAX_TOOL_ROUNDS}); "
                           "aborting turn", "fatal": False})
        yield EngineEvent(EventType.DONE, sid, {"stop_reason": "tool_round_cap"})

    def cancel(self) -> None:
        self._cancelled = True                  # takes effect between rounds

    def close(self) -> None:
        self.gateway.close()
