r"""ACP wire-format probe — spawns `hermes acp` and captures the REAL protocol.

Run ON THE HOST:  python scripts\probe_acp.py
Writes: state/acp-trace.jsonl  (every frame, sent + received, timestamped)
        state/acp-report.json  (summary verdicts)

Why: Hermes 0.16.0's embeddable surfaces are ACP and `mcp serve` (the HTTP
/v1/runs contract from the spike doesn't exist — live-verified 2026-06-10).
Before writing adapters/hermes_acp.py we capture the exact initialize /
session-new / prompt / update shapes, the same probe-first discipline that
caught every docs-guess being wrong today.

Assumes ACP = JSON-RPC 2.0 over stdio, newline-delimited (per the published
spec); if Hermes instead frames with Content-Length headers, the raw trace
will show it immediately and the reader handles both.

Stdlib only. Sends one tiny prompt (a few free-tier tokens).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

TRACE_PATH = os.path.join("state", "acp-trace.jsonl")
REPORT_PATH = os.path.join("state", "acp-report.json")
REPORT: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "errors": [], "notes": []}
_trace_f = None
_next_id = 0


def trace(direction: str, payload) -> None:
    _trace_f.write(json.dumps({"t": round(time.time(), 3), "dir": direction,
                               "msg": payload}, ensure_ascii=False) + "\n")
    _trace_f.flush()


def reader_thread(stream, q: "queue.Queue", label: str) -> None:
    """Reads ndjson OR Content-Length-framed JSON-RPC; pushes parsed msgs."""
    buf = b""
    while True:
        chunk = stream.readline()
        if not chunk:
            q.put((label, None))
            return
        line = chunk.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.lower().startswith("content-length:"):
            # LSP-style framing: read header block then exact body bytes
            try:
                n = int(line.split(":", 1)[1].strip())
                while True:                       # consume rest of header block
                    h = stream.readline()
                    if not h or h.strip() == b"":
                        break
                body = stream.read(n)
                q.put((label, json.loads(body.decode("utf-8", "replace"))))
                REPORT["notes"].append("framing: Content-Length (LSP-style)")
            except Exception as exc:              # noqa: BLE001
                q.put((label, {"_raw": line, "_error": str(exc)}))
            continue
        try:
            q.put((label, json.loads(line)))
        except json.JSONDecodeError:
            q.put((label, {"_raw": line}))


def send(proc, method: str, params: dict, *, notify: bool = False) -> int | None:
    global _next_id
    msg: dict = {"jsonrpc": "2.0", "method": method, "params": params}
    if not notify:
        _next_id += 1
        msg["id"] = _next_id
    trace("send", msg)
    proc.stdin.write((json.dumps(msg, ensure_ascii=True) + "\n").encode("ascii"))
    proc.stdin.flush()
    return None if notify else _next_id


def collect(q: "queue.Queue", want_id: int | None, timeout: float,
            updates: list) -> dict | None:
    """Wait for the response to want_id, collecting notifications along the way."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            label, msg = q.get(timeout=1.0)
        except queue.Empty:
            continue
        if msg is None:
            REPORT["errors"].append({"stage": "stream-closed", "label": label})
            return None
        if label == "stderr":
            trace("stderr", msg)
            continue
        trace("recv", msg)
        if isinstance(msg, dict) and msg.get("id") == want_id and (
                "result" in msg or "error" in msg):
            return msg
        updates.append(msg)
    REPORT["errors"].append({"stage": f"timeout waiting for id={want_id}"})
    return None


def main() -> int:
    global _trace_f
    os.makedirs("state", exist_ok=True)
    _trace_f = open(TRACE_PATH, "w", encoding="utf-8", newline="\n")

    exe = shutil.which("hermes")
    REPORT["hermes_path"] = exe
    if not exe:
        print("!! hermes not on PATH")
        return 1
    proc = subprocess.Popen([exe, "acp"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    q: "queue.Queue" = queue.Queue()
    threading.Thread(target=reader_thread, args=(proc.stdout, q, "stdout"),
                     daemon=True).start()
    threading.Thread(target=reader_thread, args=(proc.stderr, q, "stderr"),
                     daemon=True).start()
    updates: list = []
    try:
        # 1. initialize (ACP v1 shape; an error reply still teaches us the real shape)
        rid = send(proc, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False,
                                          "writeTextFile": False}},
            "clientInfo": {"name": "minerva-probe", "version": "0.1"},
        })
        resp = collect(q, rid, 30.0, updates)
        REPORT["initialize"] = resp
        if not resp or "error" in (resp or {}):
            print("initialize failed/odd — see report; trying anyway")

        # 2. new session
        rid = send(proc, "session/new", {
            "cwd": os.getcwd(), "mcpServers": [],
        })
        resp = collect(q, rid, 30.0, updates)
        REPORT["session_new"] = resp
        sid = None
        if resp and isinstance(resp.get("result"), dict):
            sid = resp["result"].get("sessionId") or resp["result"].get("session_id")
        REPORT["session_id_key"] = ("sessionId" if resp and "sessionId" in
                                    (resp.get("result") or {}) else
                                    "session_id" if resp and "session_id" in
                                    (resp.get("result") or {}) else None)

        # 3. one tiny prompt, harvest streaming updates
        if sid:
            rid = send(proc, "session/prompt", {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": "Reply with exactly: OK"}],
            })
            resp = collect(q, rid, 120.0, updates)
            REPORT["prompt_result"] = resp

        # 4. cancel shape (fire and observe — session may already be idle)
        if sid:
            send(proc, "session/cancel", {"sessionId": sid}, notify=True)
            time.sleep(2.0)
            while not q.empty():
                label, msg = q.get_nowait()
                if msg is not None and label != "stderr":
                    trace("recv", msg)
                    updates.append(msg)
    finally:
        try:
            proc.kill()
        except OSError:
            pass

    # digest the notification taxonomy
    methods: dict = {}
    for u in updates:
        if isinstance(u, dict) and "method" in u:
            m = u["method"]
            entry = methods.setdefault(m, {"count": 0, "param_keys": set(),
                                           "update_kinds": set()})
            entry["count"] += 1
            params = u.get("params") or {}
            entry["param_keys"].update(params.keys())
            upd = params.get("update")
            if isinstance(upd, dict):
                entry["update_kinds"].add(upd.get("sessionUpdate")
                                          or upd.get("kind") or "?")
    REPORT["notification_methods"] = {
        m: {"count": v["count"], "param_keys": sorted(v["param_keys"]),
            "update_kinds": sorted(v["update_kinds"])}
        for m, v in methods.items()}
    REPORT["n_updates"] = len(updates)

    with open(REPORT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(REPORT, f, indent=2, ensure_ascii=False, default=str)
    print("\n=== ACP PROBE SUMMARY ===")
    print("initialize ok:", bool(REPORT.get("initialize", {}) or {}).__class__ is bool
          and "result" in (REPORT.get("initialize") or {}))
    print("session id key:", REPORT.get("session_id_key"))
    print("notification methods:", list(REPORT.get("notification_methods", {})))
    print("stop reason:", ((REPORT.get("prompt_result") or {}).get("result")
                           or {}).get("stopReason"))
    print(f"frames -> {TRACE_PATH}\nreport -> {REPORT_PATH}")
    print(f"errors: {len(REPORT['errors'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
