r"""Live CONTRACT-TODO verification probe — run ON THE HOST against the local Hermes API.

Usage (PowerShell, with the Hermes API server running):
    python scripts\verify_contract.py --base-url http://127.0.0.1:9119
    python scripts\verify_contract.py --no-run   # skip model-dependent parts

Probes the spike-documented surface (sessions CRUD, /v1/runs, SSE events, stop,
fork) plus a DISCOVERY sweep (openapi.json jackpot if FastAPI) and records what
the server ACTUALLY says: field names, id keys, SSE event taxonomy, usage payload
shape, auth enforcement. Never crashes on a failed probe — every step is recorded,
partial results are still useful.

Writes:  state/contract-report.json   (read back by whoever ran the check)
Prints:  human-readable verdicts mapped to each CONTRACT-TODO.

Stdlib only, like the rest of minerva_core. Read-only against Hermes apart from
creating (and deleting) probe sessions/runs. The probe prompt costs a few tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPORT: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "errors": []}


def note_error(stage: str, exc: Exception | str) -> None:
    REPORT["errors"].append({"stage": stage, "error": str(exc)})


def load_env_key() -> tuple[str, dict]:
    """Read Hermes .env files; report which var names are present."""
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", ".env"),
        os.path.expanduser(os.path.join("~", ".hermes", ".env")),
    ]
    info: dict = {"checked": candidates, "found": None, "key_var_used": None}
    key = ""
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                pairs = dict(line.strip().split("=", 1) for line in f
                             if "=" in line and not line.strip().startswith("#"))
        except OSError:
            continue
        info["found"] = path
        for cand in ("API_SERVER_KEY", "API_KEY", "HERMES_API_KEY", "DASHBOARD_KEY"):
            if pairs.get(cand):
                key, info["key_var_used"] = pairs[cand], cand
                break
        if key:
            break
    return key, info


def http(method: str, url: str, key: str | None = None, body: dict | None = None,
         accept: str | None = None, timeout: float = 30.0):
    """Returns (status, parsed_json_or_text, response_obj_for_streams)."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if accept:
        headers["Accept"] = accept
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        if accept == "text/event-stream":
            return resp.status, None, resp
        raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw) if raw.strip() else {}, None
        except json.JSONDecodeError:
            return resp.status, raw[:500], None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:500]
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            pass
        return e.code, raw, None
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e), None


def probe_auth(base: str, key: str) -> None:
    s_no, _, _ = http("GET", base + "/v1/models")
    s_yes, body, _ = http("GET", base + "/v1/models", key)
    models = []
    if isinstance(body, dict):
        models = [m.get("id", "?") for m in body.get("data", [])][:10]
    REPORT["auth"] = {"no_auth_status": s_no, "with_auth_status": s_yes,
                      "bearer_enforced": s_no in (401, 403) and s_yes == 200,
                      "models_seen": models}


DISCOVERY_PATHS = [
    "/openapi.json", "/api/openapi.json", "/docs", "/api/docs", "/health",
    "/api/health", "/version", "/api/version", "/api", "/api/sessions",
    "/v1/models", "/v1/runs", "/api/runs", "/api/chat", "/v1/chat/completions",
]


def probe_discovery(base: str, key: str) -> None:
    """Map the real API surface. A 405 means the route EXISTS (wrong method).
    If openapi.json is served (FastAPI etc.), capture the whole contract."""
    out: dict = {"paths": {}, "openapi": None}
    REPORT["discovery"] = out
    for path in DISCOVERY_PATHS:
        status, body, _ = http("GET", base + path, key, timeout=10.0)
        out["paths"][path] = status
        if path.endswith("openapi.json") and status == 200 and isinstance(body, dict):
            spec_paths = body.get("paths", {})
            out["openapi"] = {
                "title": body.get("info", {}).get("title"),
                "version": body.get("info", {}).get("version"),
                "n_paths": len(spec_paths),
                "paths": {p: sorted(m.upper() for m in v if m != "parameters")
                          for p, v in sorted(spec_paths.items())},
            }
            keep = {}
            for p, v in spec_paths.items():
                if any(s in p for s in ("session", "run", "chat", "event")):
                    keep[p] = v
            out["openapi"]["relevant_detail"] = keep
    found = [p for p, s in out["paths"].items() if s not in (0, 404)]
    print(f"discovery: {len(found)} live paths -> {found}")


def probe_sessions(base: str, key: str) -> str | None:
    out: dict = {}
    REPORT["sessions"] = out
    body = {"name": "minerva-contract-probe", "system_prompt": "You are a test probe."}
    status, d, _ = http("POST", base + "/api/sessions", key, body)
    out["create_status"] = status
    sid = None
    if status in (200, 201) and isinstance(d, dict):
        out["response_keys"] = sorted(d.keys())
        sid = d.get("id") or d.get("session_id")
        out["id_key"] = "id" if "id" in d else ("session_id" if "session_id" in d else None)
        out["system_prompt_echoed"] = "system_prompt" in d
    elif status in (400, 422):
        out["rejected_body"] = d
        status2, d2, _ = http("POST", base + "/api/sessions", key,
                              {"name": "minerva-contract-probe"})
        out["minimal_create_status"] = status2
        if isinstance(d2, dict):
            sid = d2.get("id") or d2.get("session_id")
            out["response_keys"] = sorted(d2.keys())
    else:
        note_error("sessions.create", f"status {status}: {d}")
        return None
    if not sid:
        return None
    sid = str(sid)

    status, d, _ = http("GET", base + "/api/sessions", key)
    out["list_status"] = status
    status, d, _ = http("POST", f"{base}/api/sessions/{sid}/fork", key, {})
    out["fork_status"] = status
    if status in (200, 201) and isinstance(d, dict):
        fork_id = d.get("id") or d.get("session_id")
        if fork_id:
            http("DELETE", f"{base}/api/sessions/{fork_id}", key)
    return sid


def probe_run(base: str, key: str, sid: str) -> str | None:
    """Find the working /v1/runs body shape. Tries documented shape first."""
    out: dict = {}
    REPORT["runs"] = out
    shapes = [
        ("session_id+input", {"session_id": sid, "input": "Reply with exactly: OK"}),
        ("session_id+prompt", {"session_id": sid, "prompt": "Reply with exactly: OK"}),
        ("session_id+message", {"session_id": sid, "message": "Reply with exactly: OK"}),
        ("session_id+messages[]", {"session_id": sid, "messages": [
            {"role": "user", "content": "Reply with exactly: OK"}]}),
    ]
    for label, body in shapes:
        status, d, _ = http("POST", base + "/v1/runs", key, body)
        out.setdefault("attempts", []).append({"shape": label, "status": status})
        if status in (200, 201) and isinstance(d, dict):
            out["working_shape"] = label
            out["response_keys"] = sorted(d.keys())
            rid = d.get("id") or d.get("run_id")
            out["id_key"] = "id" if "id" in d else ("run_id" if "run_id" in d else None)
            return str(rid) if rid else None
        if status in (400, 401, 402, 403, 422, 500):
            out.setdefault("rejections", []).append({"shape": label, "body": d})
    return None


def probe_sse(base: str, key: str, rid: str, max_secs: float = 90.0) -> None:
    out: dict = {"event_names": [], "samples": {}, "usage_keys": None,
                 "has_usd": None, "final_event": None, "text_delta_keys": None}
    REPORT["sse"] = out
    status, _, resp = http("GET", f"{base}/v1/runs/{rid}/events", key,
                           accept="text/event-stream", timeout=20.0)
    out["status"] = status
    if resp is None:
        note_error("sse", f"no stream (status {status})")
        return
    t0, name, data_lines, n = time.time(), "", [], 0
    try:
        while time.time() - t0 < max_secs and n < 200:
            line = resp.readline()
            if not line:
                break
            line = line.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "" and (name or data_lines):
                try:
                    d = json.loads("\n".join(data_lines)) if data_lines else {}
                except json.JSONDecodeError:
                    d = {"raw": "\n".join(data_lines)[:200]}
                ev = name or (d.get("type", "") if isinstance(d, dict) else "")
                n += 1
                if ev not in out["event_names"]:
                    out["event_names"].append(ev)
                    if isinstance(d, dict):
                        out["samples"][ev] = sorted(d.keys())
                if isinstance(d, dict):
                    if ev == "usage" or "input_tokens" in d:
                        out["usage_keys"] = sorted(d.keys())
                        out["has_usd"] = "usd" in d
                    if "delta" in ev.lower() and out["text_delta_keys"] is None:
                        out["text_delta_keys"] = sorted(d.keys())
                out["final_event"] = ev
                name, data_lines = "", []
    except OSError as exc:
        note_error("sse.read", exc)
    finally:
        try:
            resp.close()
        except OSError:
            pass


def probe_stop(base: str, key: str, sid: str) -> None:
    """Second short run, stopped immediately — verifies the cancel path."""
    status, d, _ = http("POST", base + "/v1/runs", key,
                        {"session_id": sid,
                         "input": "Count from 1 to 500, one number per line."})
    if status not in (200, 201) or not isinstance(d, dict):
        REPORT["stop"] = {"skipped": f"run create failed ({status})"}
        return
    rid = d.get("id") or d.get("run_id")
    time.sleep(1.0)
    status, body, _ = http("POST", f"{base}/v1/runs/{rid}/stop", key, {})
    REPORT["stop"] = {"status": status,
                      "body_keys": sorted(body) if isinstance(body, dict) else body}


def verdicts() -> dict:
    s, r, sse = REPORT.get("sessions", {}), REPORT.get("runs", {}), REPORT.get("sse", {})
    auth = REPORT.get("auth", {})
    disc = REPORT.get("discovery", {})
    known = {"assistant.delta", "message.delta", "message.complete", "tool.started",
             "tool.start", "tool.completed", "tool.complete", "approval.request",
             "clarify.request", "usage", "error", "run.completed", "run.complete", "done"}
    seen = set(sse.get("event_names", []))
    openapi = disc.get("openapi") or {}
    v = {
        "openapi_found": f"YES — {openapi.get('n_paths')} paths (see report)"
        if openapi else "no",
        "bearer_enforced_on_loopback": auth.get("bearer_enforced"),
        "session_create_system_prompt_field":
            "VERIFIED" if s.get("create_status") in (200, 201) and s.get("system_prompt_echoed")
            else f"create={s.get('create_status')} echoed={s.get('system_prompt_echoed')} — inspect",
        "session_id_key": s.get("id_key"),
        "runs_body_shape": r.get("working_shape") or "NONE WORKED — inspect rejections",
        "run_id_key": r.get("id_key"),
        "sse_event_names_seen": sorted(seen),
        "sse_unknown_to_adapter": sorted(seen - known),
        "usage_has_usd": sse.get("has_usd"),
        "final_event": sse.get("final_event"),
        "stop_status": REPORT.get("stop", {}).get("status"),
        "config_keys_memory": "VERIFIED 2026-06-10 (live config read: memory.memory_enabled etc.)",
        "idle_footprint": "MANUAL — PowerShell: Get-Process | ? {$_.Name -match 'hermes|python'} | Select Name,Id,@{n='MB';e={[int]($_.WS/1MB)}}",
    }
    REPORT["verdicts"] = v
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:9119")
    ap.add_argument("--no-run", action="store_true", help="skip model-dependent probes")
    ap.add_argument("--keep", action="store_true", help="don't delete the probe session")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    REPORT["base_url"] = base

    key, env_info = load_env_key()
    REPORT["env"] = env_info
    if not key:
        print("note: no API key found in Hermes .env files — probing without bearer")

    probe_auth(base, key)
    if REPORT["auth"]["with_auth_status"] == 0 and not REPORT["auth"]["no_auth_status"]:
        print(f"!! cannot reach {base} — is the Hermes API server running?")
    probe_discovery(base, key)
    sid = probe_sessions(base, key)
    if sid and not args.no_run:
        rid = probe_run(base, key, sid)
        if rid:
            probe_sse(base, key, rid)
        probe_stop(base, key, sid)
    if sid and not args.keep:
        status, _, _ = http("DELETE", f"{base}/api/sessions/{sid}", key)
        REPORT["sessions"]["delete_status"] = status

    v = verdicts()
    os.makedirs("state", exist_ok=True)
    out_path = os.path.join("state", "contract-report.json")
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(REPORT, f, indent=2, ensure_ascii=False)

    print("\n=== CONTRACT VERDICTS ===")
    for k, val in v.items():
        print(f"{k:38} {val}")
    print(f"\nfull report -> {out_path}")
    print(f"errors: {len(REPORT['errors'])}" + (" (see report)" if REPORT["errors"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
