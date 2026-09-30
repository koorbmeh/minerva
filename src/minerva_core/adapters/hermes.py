"""Adapter #1: Hermes Agent as an external service (spec §5).

STATUS 2026-06-10 (live host verification): PARKED. Hermes 0.16.0 has NO
/v1/runs + SSE HTTP surface — the spike's claimed contract does not exist.
The dashboard (port 9119) is a management API; chat rides a WebSocket with
ticket auth (undocumented, churn-prone). The supported embedding surfaces are
`hermes acp` (Agent Client Protocol, stdio, streaming — chosen successor) and
`hermes mcp serve` (request/response). Replacement: adapters/hermes_acp.py
once scripts/probe_acp.py captures the live wire format. This file stays as
the mock-tested reference for the translation layer (the _translate pattern
carries over).

Original header (docs-sourced, now known wrong):
Speaks HTTP to Hermes's loopback API server (spike-claimed surface:
/api/sessions CRUD, /v1/runs + /v1/runs/{id}/events SSE, /v1/runs/{id}/stop,
bearer auth REQUIRED even on loopback). Stdlib only (urllib).

CONTRACT-TODO markers flag field names taken from docs but not yet verified
hands-on; the host install confirms them. Translation lives in one function
(_translate) so contract drift costs one edit.

Hermes-side config this adapter assumes (see Engine-Interface spec §5):
  memory_enabled:false, user_profile_enabled:false, curator.enabled:false,
  built-in toolsets disabled, minerva gateway as sole MCP server,
  approvals.mode:manual, pinned hermes-agent==0.16.0.
"""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from typing import Iterator

from ..engine import SessionConfig
from ..events import EngineEvent, EventType


class HermesAdapter:
    name = "hermes"

    def __init__(self, base_url: str = "http://127.0.0.1:8642",
                 api_key: str = "", timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def capabilities(self) -> set[str]:
        return {"stream", "tools_mcp", "fork", "model_per_session"}

    def create_session(self, cfg: SessionConfig) -> "HermesSession":
        body: dict = {"name": f"minerva-{secrets.token_hex(4)}"}
        if cfg.model:
            body["model"] = cfg.model                  # CONTRACT-TODO verify field
        if cfg.persona:
            body["system_prompt"] = cfg.persona        # CONTRACT-TODO verify field
        d = self._request_json("POST", "/api/sessions", body)
        sid = d.get("id") or d.get("session_id")       # tolerate either shape
        if not sid:
            raise RuntimeError(f"no session id in response: {d}")
        return HermesSession(self, str(sid), cfg)

    # ---- http plumbing --------------------------------------------------
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _request_json(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=self._headers(), method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def _stream_sse(self, path: str) -> Iterator[tuple[str, dict]]:
        """Yield (event_name, data_dict) pairs from an SSE stream."""
        req = urllib.request.Request(self.base_url + path, headers={
            **self._headers(), "Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            event_name, data_lines = "", []
            while True:
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "" and (event_name or data_lines):
                    try:
                        data = json.loads("\n".join(data_lines)) if data_lines else {}
                    except json.JSONDecodeError:
                        data = {"raw": "\n".join(data_lines)}
                    yield event_name or data.get("type", ""), data
                    event_name, data_lines = "", []


class HermesSession:
    def __init__(self, adapter: HermesAdapter, session_id: str, cfg: SessionConfig) -> None:
        self.adapter = adapter
        self.id = session_id
        self.cfg = cfg
        self._last_run_id: str | None = None

    def send(self, text: str) -> Iterator[EngineEvent]:
        try:
            body: dict = {"session_id": self.id, "input": text}   # CONTRACT-TODO verify fields
            if self.cfg.model:
                body["model"] = self.cfg.model
            run = self.adapter._request_json("POST", "/v1/runs", body)
            run_id = str(run.get("id") or run.get("run_id") or "")
            if not run_id:
                raise RuntimeError(f"no run id in response: {run}")
            self._last_run_id = run_id

            done_sent = False
            for name, data in self.adapter._stream_sse(f"/v1/runs/{run_id}/events"):
                ev = self._translate(name, data)
                if ev is not None:
                    yield ev
                    if ev.type is EventType.DONE:
                        done_sent = True
                        break
            if not done_sent:
                yield EngineEvent(EventType.DONE, self.id)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                RuntimeError, json.JSONDecodeError) as exc:
            yield EngineEvent(EventType.ERROR, self.id,
                              {"message": str(exc), "fatal": True})
            yield EngineEvent(EventType.DONE, self.id)

    def _translate(self, name: str, d: dict) -> EngineEvent | None:
        sid = self.id
        if name in ("assistant.delta", "message.delta"):
            return EngineEvent(EventType.TEXT_DELTA, sid,
                               {"text": d.get("text") or d.get("delta", "")})
        if name == "message.complete":
            return EngineEvent(EventType.MESSAGE, sid, {"text": d.get("text", "")})
        if name in ("tool.started", "tool.start"):
            return EngineEvent(EventType.TOOL_CALL, sid,
                               {"tool": d.get("tool") or d.get("name", ""),
                                "args": d.get("args") or d.get("arguments", {}),
                                "call_id": d.get("call_id") or d.get("id", "")})
        if name in ("tool.completed", "tool.complete"):
            return EngineEvent(EventType.TOOL_RESULT, sid,
                               {"call_id": d.get("call_id") or d.get("id", ""),
                                "ok": not d.get("error"),
                                "result": d.get("result", "")})
        if name in ("approval.request", "clarify.request"):
            return EngineEvent(EventType.ASK_USER, sid,
                               {"reason": name, "question": d.get("question")
                                or d.get("prompt", ""), "ref": d.get("id", "")})
        if name == "usage" or "input_tokens" in d:
            return EngineEvent(EventType.COST, sid,
                               {"input_tokens": d.get("input_tokens", 0),
                                "output_tokens": d.get("output_tokens", 0),
                                "usd": d.get("usd", 0.0)})   # CONTRACT-TODO: $ unverified
        if name == "error":
            return EngineEvent(EventType.ERROR, sid,
                               {"message": d.get("message", "engine error"),
                                "fatal": bool(d.get("fatal"))})
        if name in ("run.completed", "run.complete", "done"):
            return EngineEvent(EventType.DONE, sid)
        return None      # unknown event names are skipped by design

    def cancel(self) -> None:
        if self._last_run_id:
            try:
                self.adapter._request_json("POST", f"/v1/runs/{self._last_run_id}/stop", {})
            except Exception:                                     # noqa: BLE001
                pass

    def close(self) -> None:
        pass        # Hermes sessions persist server-side; nothing to release
