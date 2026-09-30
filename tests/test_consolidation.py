"""Episodic consolidation — promotion, dedupe, cursor, torn-tail, read-only-ledger.
Run: python tests/test_consolidation.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consolidate  # noqa: E402
from minerva_core.ledger import ACTION, Ledger, NOTE, PITCH, SESSION  # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def drops_in(brain: str) -> list[str]:
    d = os.path.join(brain, "inbox-drop")
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".md"))


def read_drop(brain: str, name: str) -> str:
    with open(os.path.join(brain, "inbox-drop", name), encoding="utf-8") as f:
        return f.read()


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = os.path.join(td, "state")
        brain = os.path.join(td, "brain")
        os.makedirs(state)
        os.makedirs(os.path.join(brain, "inbox-drop"))
        ledger_path = os.path.join(state, "ledger.jsonl")
        cursor_path = os.path.join(state, "consolidation-cursor.json")

        # ---- missing ledger tolerated -----------------------------------
        s = consolidate.run(state, brain, quiet=True)
        check("missing ledger tolerated",
              s["ledger_entries"] == 0 and s["promoted"] == 0 and not s["torn_tail"])
        check("no drop for empty pass", drops_in(brain) == [])

        # ---- promotion: notes flow, other types + parented + short skip ---
        led = Ledger(ledger_path)
        led.append(SESSION, {"engine": "hermes-acp"})
        e1 = led.append(NOTE, {"text": "The operator said the Civic needs winter tires "
                                       "before November", "about": "Operator",
                               "source": "engine"})
        e2 = led.append(NOTE, {"text": "A coworker mentioned they changed dentists",
                               "about": "", "source": "engine"})
        led.append(NOTE, {"text": "too short", "about": "", "source": "engine"})
        pitch = led.append(PITCH, {"domain": "testing", "title": "some idea",
                                   "slug": "some-idea"})
        led.append(NOTE, {"reopen": True}, parent_id=pitch["id"])   # pitch control
        led.append(ACTION, {"tool": "ping", "decision": "allow"})
        ledger_bytes_before = open(ledger_path, "rb").read()

        s = consolidate.run(state, brain, quiet=True)
        check("two notes promoted", s["promoted"] == 2)
        check("short skipped", s["skipped_short"] == 1)
        check("parented (pitch-reopen) excluded", s["skipped_parented"] == 1)
        check("non-note types passed over", s["skipped_other_types"] == 3)
        check("all entries consumed", s["consumed"] == 7)

        files = drops_in(brain)
        check("exactly one drop file", len(files) == 1)
        check("drop filename pattern",
              re.fullmatch(r"drop-\d{4}-\d{2}-\d{2}-\d{4}-minerva-notes-[0-9a-f]{4}\.md",
                           files[0]) is not None)
        body = read_drop(brain, files[0])
        check("fact text present", "winter tires" in body)
        check("about carried", "- about: Operator" in body)
        check("empty about becomes UNSURE", "- about: UNSURE" in body)
        check("ledger id cited", e1["id"] in body and e2["id"] in body)
        check("engine source labeled", "(engine note," in body)
        check("no temp file left behind",
              not any(f.startswith(".tmp-")
                      for f in os.listdir(os.path.join(brain, "inbox-drop"))))

        # ---- ledger untouched (read-only proof) --------------------------
        check("ledger bytes unchanged",
              open(ledger_path, "rb").read() == ledger_bytes_before)
        ok, n, bad = Ledger(ledger_path).verify_chain()
        check("ledger chain still intact", ok and n == 7 and bad is None)

        # ---- cursor: re-run promotes nothing ------------------------------
        s = consolidate.run(state, brain, quiet=True)
        check("re-run consumes nothing", s["consumed"] == 0 and s["promoted"] == 0)
        check("re-run writes no file", len(drops_in(brain)) == 1)

        # ---- only post-cursor notes; cross-run dedupe ---------------------
        led.append(NOTE, {"text": "The operator said the Civic needs winter tires "
                                  "before November arrives", "about": "Operator",
                          "source": "engine"})        # near-dupe of e1
        led.append(NOTE, {"text": "The operator prefers direct BYOK API keys over "
                                  "aggregator portals", "about": "Operator",
                          "source": "engine"})        # fresh fact
        s = consolidate.run(state, brain, quiet=True)
        check("near-dupe across runs skipped",
              s["skipped_dupe"] == 1 and s["promoted"] == 1)
        files = drops_in(brain)
        check("second drop written", len(files) == 2)
        body2 = read_drop(brain, files[-1]) + read_drop(brain, files[0])
        check("fresh fact in a drop", "BYOK" in body2)

        # ---- distinct facts survive the near-dupe filter -------------------
        led.append(NOTE, {"text": "The consolidation pass should run after "
                                  "each engine session", "about": "minerva",
                          "source": "engine"})
        led.append(NOTE, {"text": "NAS idle footprint measurement is still "
                                  "an open item", "about": "minerva",
                          "source": "engine"})
        s = consolidate.run(state, brain, quiet=True)
        check("distinct facts both promoted", s["promoted"] == 2)

        # ---- exact dupe within one batch -----------------------------------
        led.append(NOTE, {"text": "Repeated observation about the same thing X",
                          "about": "", "source": "engine"})
        led.append(NOTE, {"text": "repeated   observation about the SAME thing x",
                          "about": "", "source": "engine"})    # ws/case variant
        s = consolidate.run(state, brain, quiet=True)
        check("exact dupe within batch skipped",
              s["promoted"] == 1 and s["skipped_dupe"] == 1)

        # ---- dry run writes nothing -----------------------------------------
        led.append(NOTE, {"text": "A dry-run candidate fact that is long enough",
                          "about": "", "source": "engine"})
        n_files = len(drops_in(brain))
        cur_before = json.load(open(cursor_path, encoding="utf-8"))
        s = consolidate.run(state, brain, dry_run=True, quiet=True)
        check("dry run sees the candidate", s["promoted"] == 1)
        check("dry run writes no drop", len(drops_in(brain)) == n_files)
        cur_after = json.load(open(cursor_path, encoding="utf-8"))
        check("dry run leaves cursor alone", cur_before == cur_after)
        s = consolidate.run(state, brain, quiet=True)   # real run consumes it
        check("real run after dry run promotes", s["promoted"] == 1)

        # ---- torn tail: stop before it, resume after completion -------------
        with open(ledger_path, "a", encoding="utf-8", newline="\n") as f:
            f.write('{"id":"torn","type":"note","payload":{"te')   # mid-append
        s = consolidate.run(state, brain, quiet=True)
        check("torn tail flagged, nothing past it consumed",
              s["torn_tail"] and s["consumed"] == 0)
        # gateway finishes the append (same line completed in place)
        raw = open(ledger_path, encoding="utf-8").read()
        fixed = raw[:raw.rindex('{"id":"torn"')] + json.dumps(
            {"id": "torn-done", "ts": 5.0, "type": "note", "actor": "minerva",
             "parent_id": None, "prev_hash": "x",
             "payload": {"text": "Fact that arrived in a torn write, now whole",
                         "about": "", "source": "engine"}}) + "\n"
        with open(ledger_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(fixed)
        s = consolidate.run(state, brain, quiet=True)
        check("completed tail picked up next run",
              s["promoted"] == 1 and not s["torn_tail"])

    # ---- max_per_run cap: remainder picked up by the following run ---------
    with tempfile.TemporaryDirectory() as td:
        state = os.path.join(td, "state")
        brain = os.path.join(td, "brain")
        os.makedirs(state)
        led = Ledger(os.path.join(state, "ledger.jsonl"))
        for text in ("The NAS should hibernate when idle overnight to save power",
                     "The Discord delivery cursor lives in host-loop state",
                     "The library card renews in August"):
            led.append(NOTE, {"text": text, "about": "", "source": "engine"})
        s1 = consolidate.run(state, brain, max_per_run=2, quiet=True)
        s2 = consolidate.run(state, brain, max_per_run=2, quiet=True)
        check("cap honored on first run", s1["promoted"] == 2)
        check("remainder promoted on second run", s2["promoted"] == 1)
        check("two drop files total", len(drops_in(brain)) == 2)

        # rolling memory shape persisted
        cur = json.load(open(os.path.join(state, "consolidation-cursor.json"),
                             encoding="utf-8"))
        check("cursor counts all entries", cur["ledger_consumed"] == 3)
        check("rolling memory holds [hash, slug] pairs",
              len(cur["seen"]) == 3 and all(len(p) == 2 for p in cur["seen"]))

    # ---- v1.1: Operator-input lanes (operator_input_loop) -------------------------
    with tempfile.TemporaryDirectory() as td:
        state = os.path.join(td, "state")
        brain = os.path.join(td, "brain")
        os.makedirs(state)
        led = Ledger(os.path.join(state, "ledger.jsonl"))
        pitch = led.append(PITCH, {"domain": "autonomous-income",
                                   "title": "Sell old camera gear",
                                   "slug": "sell-old-camera-gear",
                                   "detail": "List the DSLR kit."})
        led.append(NOTE, {"text": "The NAS stays plugged in at the desk "
                                  "most days lately", "about": "Operator",
                          "source": "engine"})
        verdict = led.append(
            "pitch_verdict",
            {"verdict": "decline_forever",
             "reason": "I've listed every spare part on the shelf already, "
                       "there is nothing left I want to sell."},
            parent_id=pitch["id"], actor="operator")
        led.append("pitch_verdict", {"verdict": "adopt", "reason": ""},
                   parent_id="no-such-pitch", actor="engine")  # NOT operator: skip
        jl_path = os.path.join(state, "approvals.jsonl")
        with open(jl_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ref": "r77", "ts": 5.0, "tool": "test_spend",
                                "args": {}, "reason": "spend",
                                "status": "pending"}) + "\n")
            f.write(json.dumps({"ref": "r77", "ts": 6.0, "status": "denied",
                                "by": "operator", "via": "discord",
                                "msg_id": "555",
                                "note": "Only for testing, never actually "
                                        "buy anything without asking."}) + "\n")
            f.write(json.dumps({"ref": "r78", "ts": 7.0, "tool": "ping",
                                "args": {}, "reason": "x",
                                "status": "pending"}) + "\n")
            f.write(json.dumps({"ref": "r78", "ts": 8.0, "status": "approved",
                                "by": "operator", "note": "ok"}) + "\n")  # short
        ib_path = os.path.join(state, "discord-inbox.jsonl")
        with open(ib_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": 9.0, "via": "discord", "msg_id": "888",
                                "text": "the bike swap is this Saturday, "
                                        "after that the selling project is "
                                        "done"}) + "\n")
            f.write(json.dumps({"ts": 9.5, "via": "discord", "msg_id": "889",
                                "text": ""}) + "\n")   # file-only line: skip

        ledger_bytes = open(os.path.join(state, "ledger.jsonl"), "rb").read()
        s = consolidate.run(state, brain, quiet=True)
        check("v1.1 lanes promote alongside notes",
              s["promoted"] == 4 and s["promoted_notes"] == 1
              and s["promoted_verdicts"] == 1
              and s["promoted_approvals"] == 1 and s["promoted_inbox"] == 1)
        body = read_drop(brain, drops_in(brain)[0])
        check("verdict fact carries pitch title + the operator's words",
              "Sell old camera gear" in body
              and "NEVER (dead forever)" in body
              and "nothing left I want to sell" in body)
        check("verdict fact is operator-attributed and ledger-cited",
              "- about: Operator" in body and verdict["id"] in body
              and "pitch_verdict by the operator" in body)
        check("approval note promoted with tool context + provenance",
              "denied Minerva's request to run test_spend" in body
              and "never actually buy" in body
              and "approvals.jsonl ref r77" in body and "via Discord" in body)
        check("short approval note NOT promoted", '"ok"' not in body)
        check("discord message promoted, operator-attributed",
              'Operator to Minerva (Discord): "the bike swap' in body
              and "discord-inbox msg 888" in body)
        check("non-operator pitch_verdict skipped",
              "no-such-pitch" not in body)
        check("v1.1 ledger access still read-only",
              open(os.path.join(state, "ledger.jsonl"), "rb").read()
              == ledger_bytes)
        cur = json.load(open(os.path.join(state, "consolidation-cursor.json"),
                             encoding="utf-8"))
        check("all four lane cursors persisted",
              cur["ledger_consumed"] == 4 and cur["verdicts_consumed"] == 4
              and cur["approvals_consumed"] == 4 and cur["inbox_consumed"] == 2)

        s = consolidate.run(state, brain, quiet=True)
        check("v1.1 re-run consumes nothing",
              s["promoted"] == 0 and s["consumed"] == 0
              and s["consumed_verdicts"] == 0 and s["consumed_approvals"] == 0
              and s["consumed_inbox"] == 0)
        check("v1.1 re-run writes no drop", len(drops_in(brain)) == 1)

        # mind-change later: a fresh verdict entry promotes as a fresh fact
        led.append("pitch_verdict", {"verdict": "park",
                                     "reason": "actually, maybe after the "
                                               "bike swap weekend",
                                     "reopen_condition": "if items remain"},
                   parent_id=pitch["id"], actor="operator")
        s = consolidate.run(state, brain, quiet=True)
        check("changed verdict promotes as a fresh operator fact",
              s["promoted_verdicts"] == 1)
        body2 = "".join(read_drop(brain, n) for n in drops_in(brain))
        check("changed verdict composed with PARK + reopen",
              "PARK (shelved)" in body2 and "reopen when: if items remain" in body2)

    # ---- v1.1 retroactivity: pre-v1.1 cursor already passed the verdicts ----
    with tempfile.TemporaryDirectory() as td:
        state = os.path.join(td, "state")
        brain = os.path.join(td, "brain")
        os.makedirs(state)
        led = Ledger(os.path.join(state, "ledger.jsonl"))
        pitch = led.append(PITCH, {"domain": "testing", "title": "Old pitch",
                                   "slug": "old-pitch"})
        led.append("pitch_verdict",
                   {"verdict": "decline_for_now", "cooldown_days": 1.0,
                    "reason": "This was only ever a test pitch."},
                   parent_id=pitch["id"], actor="operator")
        led.append(NOTE, {"text": "An engine note the old cursor already "
                                  "consumed long ago", "about": "",
                          "source": "engine"})
        # pre-v1.1 cursor shape: notes cursor at END, no lane keys, no seen
        with open(os.path.join(state, "consolidation-cursor.json"), "w",
                  encoding="utf-8", newline="\n") as f:
            json.dump({"ledger_consumed": 3, "seen": []}, f)
        s = consolidate.run(state, brain, quiet=True)
        check("retroactive: old verdict promoted despite consumed notes cursor",
              s["promoted_verdicts"] == 1 and s["promoted_notes"] == 0
              and s["consumed"] == 0)
        body = read_drop(brain, drops_in(brain)[0])
        check("retroactive verdict is the test pitch",
              "Old pitch" in body and "only ever a test pitch" in body
              and "LATER (cooldown)" in body)

    # ---- v1.1 torn approvals tail: stop before it, resume after ------------
    with tempfile.TemporaryDirectory() as td:
        state = os.path.join(td, "state")
        brain = os.path.join(td, "brain")
        os.makedirs(state)
        Ledger(os.path.join(state, "ledger.jsonl"))   # empty ledger file
        a_path = os.path.join(state, "approvals.jsonl")
        with open(a_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ref": "r1", "ts": 1.0, "tool": "t", "args": {},
                                "reason": "x", "status": "pending"}) + "\n")
            f.write('{"ref": "r1", "ts": 2.0, "status": "denied", "by": "oper')
        s = consolidate.run(state, brain, quiet=True)
        check("torn approvals tail not consumed past",
              s["consumed_approvals"] == 1 and s["promoted_approvals"] == 0)
        with open(a_path, "a", encoding="utf-8", newline="\n") as f:
            # completes the torn line in place (its note is short: skipped)
            f.write('ator", "note": "half line"}\n')
            f.write(json.dumps({"ref": "r1", "ts": 3.0, "status": "denied",
                                "by": "operator",
                                "note": "Denied because this tool is only "
                                        "for testing."}) + "\n")
        s = consolidate.run(state, brain, quiet=True)
        check("approvals resume past completed tail",
              s["promoted_approvals"] == 1)

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
