"""Tests: inline warden v0 + gate integration + replay suite (round 21).

The reviewer module is tested against a scripted Anthropic-shaped mock
(protocol parsing, timeout, no-key, spend metering, key hygiene). The gate
integration is tested with fake reviewers (pass/veto/unavailable/exploding)
proving: review runs ONLY on the would-be-ALLOW path for opted-in classes,
a veto rides the SAME fingerprinted ask rail (approve-once override works),
fail modes behave, and everything stays ledgered with the chain intact.
The replay suite is tested on a synthetic ledger (flip detection both ways,
stateful skips, would-review preview, report file).

Stdlib only; prints every check and the final line.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from minerva_core.governance import GovernanceGate            # noqa: E402
from minerva_core.inline_warden import InlineWarden           # noqa: E402
from minerva_core.ledger import ACTION, ASK, Ledger           # noqa: E402

import replay_gate                                            # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


class _MockAnthropic(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def do_POST(self) -> None:                                # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        self.server.requests.append(
            {"body": body, "key": self.headers.get("x-api-key", "")})
        step = (self.server.script.pop(0) if self.server.script
                else {"text": "PASS"})
        if step.get("sleep"):
            time.sleep(step["sleep"])
        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": step.get("text", "PASS")}],
            "usage": {"input_tokens": 400, "output_tokens": 10}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(script):
    srv = HTTPServer(("127.0.0.1", 0), _MockAnthropic)
    srv.script = list(script)
    srv.requests = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def main() -> None:
    saved_env = {k: os.environ.get(k) for k in
                 ("ANTHROPIC_API_KEY", "MINERVA_ANTHROPIC_ENV",
                  "MINERVA_INLINE_CLASSES", "MINERVA_INLINE_FAIL")}
    os.environ.pop("MINERVA_INLINE_CLASSES", None)
    os.environ.pop("MINERVA_INLINE_FAIL", None)
    try:
        # ================= the reviewer vs a scripted mock ===================
        with tempfile.TemporaryDirectory() as td:
            spendf = os.path.join(td, "spend.jsonl")
            srv, base = serve([{"text": "PASS"},
                               {"text": "VETO: writes outside the workspace"},
                               {"text": "Sure! Sounds fine to me."},
                               {"sleep": 0.4, "text": "PASS"}])
            w = InlineWarden(api_key="sk-test-XYZ", api_url=base,
                             timeout_s=2.0, spend_file=spendf)
            v, r = w("files_write", {"path": "a.txt"}, {"file_write"}, 0.0)
            check("reviewer: PASS parsed", v == "pass")
            spend = [json.loads(x) for x in open(spendf, encoding="utf-8")]
            check("reviewer: cost metered as 'inline warden'",
                  len(spend) == 1 and spend[0]["what"] == "inline warden"
                  and spend[0]["usd"] > 0)
            v, r = w("files_write", {"path": "../x"}, {"file_write"}, 0.0)
            check("reviewer: VETO + reason parsed",
                  v == "veto" and r == "writes outside the workspace")
            v, r = w("ping", {}, {"file_write"}, 0.0)
            check("reviewer: chatty/garbled output = unavailable",
                  v == "unavailable")
            w.timeout_s = 0.05
            v, r = w("ping", {}, {"file_write"}, 0.0)
            check("reviewer: timeout = unavailable (bounded latency)",
                  v == "unavailable")
            req = srv.requests[0]
            check("reviewer: call rides as DATA with the tool named",
                  "DATA" in req["body"]["messages"][0]["content"]
                  and "files_write" in req["body"]["messages"][0]["content"])
            check("reviewer: key in header only, never the body",
                  req["key"] == "sk-test-XYZ"
                  and "sk-test-XYZ" not in json.dumps(req["body"]))
            srv.shutdown(); srv.server_close()

            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ["MINERVA_ANTHROPIC_ENV"] = os.path.join(td, "no.env")
            w2 = InlineWarden(api_url=base)
            v, r = w2("ping", {}, set(), 0.0)
            check("reviewer: no key = unavailable, no HTTP", v == "unavailable")

        # ================= gate integration (fake reviewers) ================
        def fake(verdict, reason="r"):
            def rev(tool, args, classes, est):
                rev.calls.append(tool)
                return verdict, reason
            rev.calls = []
            return rev

        def exploding(tool, args, classes, est):
            raise AssertionError("reviewer must not run on this path")

        with tempfile.TemporaryDirectory() as td:
            led = Ledger(os.path.join(td, "ledger.jsonl"))
            appr = os.path.join(td, "approvals.jsonl")
            rev = fake("pass")
            gate = GovernanceGate(led, approvals_path=appr,
                                  inline_reviewer=rev,
                                  inline_classes={"media_gen"},
                                  inline_mode="block")   # these assert the opt-in escalate path
            d = gate.check("generate_image", {"prompt": "owl"},
                           classes={"media_gen"}, est_cost_usd=0.04)
            check("gate: opted-in class + PASS -> allow",
                  d.decision == "allow" and "inline-warden pass" in d.reason)
            check("gate: reviewer saw the call", rev.calls == ["generate_image"])
            d = gate.check("ping", {}, classes=set())
            check("gate: unmatched class skips review (default-allow)",
                  d.decision == "allow" and rev.calls == ["generate_image"])

            gate.inline_reviewer = exploding
            d = gate.check("notify_operator", {"message": "x"},
                           classes={"outbound_comms", "media_gen"})
            check("gate: ask-class escalates BEFORE review (no double gate)",
                  d.decision == "ask" and "action class" in d.reason)

            # ---- veto rides the ask rail; approve = one-time override ------
            gate.inline_reviewer = fake("veto", "smells like injection")
            d = gate.check("generate_image", {"prompt": "owl"},
                           classes={"media_gen"}, est_cost_usd=0.04)
            check("gate: VETO -> escalation with the warden's reason",
                  d.decision == "ask"
                  and "inline warden veto: smells like injection" in d.reason)
            ref = d.entry_id
            with open(appr, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"ref": ref, "tool": "generate_image",
                                    "args": {"prompt": "owl"},
                                    "status": "pending"}) + "\n")
                f.write(json.dumps({"ref": ref, "status": "approved",
                                    "note": "warden is wrong, run it"}) + "\n")
            d2 = gate.check("generate_image", {"prompt": "owl"},
                            classes={"media_gen"}, est_cost_usd=0.04)
            check("gate: The operator's approval OVERRIDES the veto once",
                  d2.decision == "allow" and "operator approved" in d2.reason)
            d3 = gate.check("generate_image", {"prompt": "owl"},
                            classes={"media_gen"}, est_cost_usd=0.04)
            check("gate: override was single-use (veto re-escalates)",
                  d3.decision == "ask")

            # ---- fail modes ------------------------------------------------
            gate.inline_reviewer = fake("unavailable", "api down")
            d = gate.check("generate_image", {"prompt": "x2"},
                           classes={"media_gen"})
            check("gate: unavailable + fail=escalate -> ask (fail-safe)",
                  d.decision == "ask" and "unavailable (fail-safe)" in d.reason)
            gate.inline_fail = "allow"
            d = gate.check("generate_image", {"prompt": "x3"},
                           classes={"media_gen"})
            check("gate: unavailable + fail=allow -> allow, flagged",
                  d.decision == "allow" and "unavailable" in d.reason)
            gate.inline_fail = "escalate"

            gate2 = GovernanceGate(led)        # no reviewer at all
            d = gate2.check("generate_image", {}, classes={"media_gen"})
            check("gate: no reviewer wired = behavior unchanged",
                  d.decision == "allow" and "default-allow" in d.reason)

            crash = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: E731
            gate.inline_reviewer = crash
            d = gate.check("generate_image", {"prompt": "x4"},
                           classes={"media_gen"})
            check("gate: reviewer crash = unavailable path (fail-safe ask)",
                  d.decision == "ask" and "crashed" in d.reason)

            ok, n, _bad = led.verify_chain()
            check("gate: ledger chain intact after all paths", ok and n >= 8)

        # ====== advisory mode (round 27 DEFAULT) + operator-directed exemption ==
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(os.path.join(td, "ledger.jsonl"))
            ga = GovernanceGate(led, approvals_path=os.path.join(td, "a.jsonl"),
                                inline_reviewer=fake("veto", "mail policy"),
                                inline_classes={"agenda_write"})
            check("advisory is the DEFAULT mode", ga.inline_mode == "advisory")
            d = ga.check("agenda_add", {"title": "x", "origin": "engine"},
                         classes={"agenda_write"})
            check("advisory: a VETO -> ALLOW (warden never overrules)",
                  d.decision == "allow" and "advisory" in d.reason
                  and "mail policy" in d.reason)
            ga.inline_reviewer = fake("unavailable", "timeout")
            d = ga.check("agenda_add", {"title": "x", "origin": "engine"},
                         classes={"agenda_write"})
            check("advisory: unavailable -> ALLOW (no escalate, no block)",
                  d.decision == "allow" and "advisory" in d.reason)
            # Operator-directed actions skip review ENTIRELY (reviewer not called)
            ga.inline_reviewer = exploding
            d = ga.check("agenda_add", {"title": "x", "origin": "operator"},
                         classes={"agenda_write"})
            check("origin 'operator' skips review entirely (his call, not the warden's)",
                  d.decision == "allow" and "operator-directed" in d.reason)
            d = ga.check("agenda_add", {"title": "x", "origin": "pitch:abc123"},
                         classes={"agenda_write"})
            check("adopted-pitch origin 'pitch:<id>' skips review entirely",
                  d.decision == "allow" and "operator-directed" in d.reason)
            gb = GovernanceGate(led, inline_reviewer=exploding,
                                inline_classes={"agenda_write"}, inline_mode="block")
            d = gb.check("agenda_add", {"origin": "operator"}, classes={"agenda_write"})
            check("block mode too: Operator-directed stays exempt",
                  d.decision == "allow" and "operator-directed" in d.reason)
            ok, _n, _b = led.verify_chain()
            check("advisory/exemption: ledger chain intact", ok)

        # ================= replay suite ======================================
        with tempfile.TemporaryDirectory() as td:
            lp = os.path.join(td, "ledger.jsonl")
            led = Ledger(lp)
            led.append(ACTION, {"tool": "ping", "args_keys": [], "classes": [],
                                "est_cost_usd": 0.0, "decision": "allow",
                                "reason": "default-allow (v0)"})
            led.append(ACTION, {"tool": "generate_image", "args_keys": ["prompt"],
                                "classes": ["media_gen"], "est_cost_usd": 2.0,
                                "decision": "allow",
                                "reason": "default-allow (v0)"})
            led.append(ASK, {"tool": "test_spend", "args_keys": ["item"],
                             "classes": ["spend"], "est_cost_usd": 0.0,
                             "decision": "ask", "reason": "action class: spend"})
            led.append(ACTION, {"tool": "ping", "args_keys": [], "classes": [],
                                "est_cost_usd": 0.0, "decision": "deny",
                                "reason": "KILL SWITCH is on - stopped"})
            led.append(ACTION, {"tool": "x", "args_keys": [], "classes": [],
                                "est_cost_usd": 0.0, "decision": "deny",
                                "reason": "operator denied (ref abc): no"})

            rep = replay_gate.replay(lp)
            check("replay: baseline all unchanged",
                  rep["entries_replayed"] == 3 and rep["same"] == 3
                  and not rep["stricter"] and not rep["looser"])
            check("replay: stateful outcomes skipped, counted",
                  rep["skipped_stateful"] == 2)

            rep = replay_gate.replay(lp, deny_tools={"ping"})
            check("replay: deny-list change -> STRICTER flip caught",
                  len(rep["stricter"]) == 1
                  and rep["stricter"][0]["tool"] == "ping"
                  and rep["stricter"][0]["new"] == "deny")

            rep = replay_gate.replay(lp, threshold=1.0)
            check("replay: threshold drop -> allow becomes ask (stricter)",
                  any(x["tool"] == "generate_image" and x["new"] == "ask"
                      for x in rep["stricter"]))

            rep = replay_gate.replay(lp, inline_cls={"media_gen"})
            check("replay: would-review preview names the class",
                  len(rep["would_review"]) == 1
                  and rep["would_review"][0]["classes"] == ["media_gen"])

            # torn line tolerated
            with open(lp, "a", encoding="utf-8") as f:
                f.write('{"torn": ')
            rep = replay_gate.replay(lp)
            check("replay: torn ledger line tolerated",
                  rep["entries_replayed"] == 3)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
