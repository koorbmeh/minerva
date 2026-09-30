"""Chat v1 (round 11) — transcript, prompt build, run_chat semantics, and the
host_loop free-text chat hook. No network, no real engines (fake adapters).
Run: python tests/test_chat.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import host_loop                                      # noqa: E402
import minerva_chat                                   # noqa: E402
from minerva_core.engine import SessionConfig         # noqa: E402
from minerva_core.events import EngineEvent, EventType  # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def ev(t: EventType, payload: dict) -> EngineEvent:
    return EngineEvent(t, "fake-1", payload)


class FakeSession:
    def __init__(self, events: list[EngineEvent]) -> None:
        self.id = "fake-1"
        self._events = events
        self.sent = ""
        self.closed = False

    def send(self, text: str):
        self.sent = text
        yield from self._events

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeAdapter:
    name = "fake"

    def __init__(self, events: list[EngineEvent]) -> None:
        self._events = events
        self.session: FakeSession | None = None
        self.cfg: SessionConfig | None = None

    def capabilities(self) -> set[str]:
        return {"frontier"}

    def create_session(self, cfg: SessionConfig) -> FakeSession:
        self.cfg = cfg
        self.session = FakeSession(self._events)
        return self.session


def factory_for(events: list[EngineEvent], cfg: SessionConfig | None = None,
                lane: str = "fake"):
    holder: dict = {}

    def factory(state_dir: str):
        a = FakeAdapter(events)
        holder["adapter"] = a
        return a, (cfg if cfg is not None else SessionConfig()), lane

    return factory, holder


def chat_lines(state_dir: str) -> list[dict]:
    return minerva_chat.read_transcript(
        minerva_chat.chat_log_path(state_dir), n=1000)


def main() -> None:
    # ---------- transcript + prompt ------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        log = os.path.join(td, "chat-log.jsonl")
        for i in range(14):
            minerva_chat.append_chat(log, "operator" if i % 2 == 0 else "minerva",
                                     f"u{i}")
        with open(log, "a", encoding="utf-8") as f:
            f.write("{TORN\n")
        tail = minerva_chat.read_transcript(log)
        check("transcript keeps last N in order",
              [r["text"] for r in tail] == [f"u{i}" for i in range(2, 14)])
        check("torn transcript line tolerated", len(tail) == 12)

        prompt = minerva_chat.build_chat_prompt(
            "what about cafés?", [{"who": "operator", "text": "hello"},
                                       {"who": "minerva", "text": "hi"}])
        check("prompt carries transcript speakers",
              "Operator: hello" in prompt and "Minerva: hi" in prompt)
        check("prompt carries the new message",
              "The operator just texted you:" in prompt)
        check("prompt is ascii-sanitized",
              prompt.encode("ascii", "strict") is not None
              and "\\xe9" in prompt)

    # ---------- run_chat: happy path -----------------------------------------
    with tempfile.TemporaryDirectory() as td:
        events = [ev(EventType.TEXT_DELTA, {"text": "Hi "}),
                  ev(EventType.TEXT_DELTA, {"text": "The operator."}),
                  ev(EventType.MESSAGE, {"text": "Hi there."}),
                  ev(EventType.COST, {"usd": 0.0123}),
                  ev(EventType.DONE, {"stop_reason": "end_turn"})]
        factory, holder = factory_for(events)
        reply = minerva_chat.run_chat("hello there", state_dir=td,
                                      engine_factory=factory)
        check("happy reply is the MESSAGE text", reply == "Hi there.")
        lines = chat_lines(td)
        check("transcript gained operator + minerva lines",
              [r["who"] for r in lines] == ["operator", "minerva"]
              and lines[0]["text"] == "hello there")
        check("usd + lane recorded on her line",
              lines[1].get("usd") == 0.0123 and lines[1].get("lane") == "fake")
        check("session closed after the turn", holder["adapter"].session.closed)
        check("engine lock released",
              not os.path.exists(os.path.join(td, "drive-tick.lock")))
        check("persona prepended when lane lacks persona support",
              holder["adapter"].session.sent.startswith("You are Minerva"))

        # deltas-only fallback (no MESSAGE event)
        factory2, _h2 = factory_for([
            ev(EventType.TEXT_DELTA, {"text": "partial"}),
            ev(EventType.DONE, {"stop_reason": "end_turn"})])
        check("deltas assemble when MESSAGE missing",
              minerva_chat.run_chat("again", state_dir=td,
                                    engine_factory=factory2) == "partial")

    # ---------- run_chat: persona via SessionConfig --------------------------
    with tempfile.TemporaryDirectory() as td:
        factory, holder = factory_for(
            [ev(EventType.MESSAGE, {"text": "ok"}),
             ev(EventType.DONE, {"stop_reason": "end_turn"})],
            cfg=SessionConfig(persona=minerva_chat.PERSONA), lane="byok")
        minerva_chat.run_chat("hi", state_dir=td, engine_factory=factory)
        check("persona NOT duplicated when cfg carries it",
              not holder["adapter"].session.sent.startswith("You are Minerva")
              and "The operator just texted you:" in holder["adapter"].session.sent)

    # ---------- run_chat: asks / errors / empty / cap -------------------------
    with tempfile.TemporaryDirectory() as td:
        factory, _h = factory_for([
            ev(EventType.MESSAGE, {"text": "trying a tool"}),
            ev(EventType.ASK_USER, {"reason": "governance escalation",
                                    "question": "ESCALATED to the operator: spend",
                                    "ref": "abc123"}),
            ev(EventType.DONE, {"stop_reason": "end_turn"})])
        reply = minerva_chat.run_chat("spend it", state_dir=td,
                                      engine_factory=factory)
        check("escalation surfaces in the reply",
              "needs your approval - ref abc123" in reply)

        factory, _h = factory_for([
            ev(EventType.ERROR, {"message": "api error: boom"}),
            ev(EventType.DONE, {"stop_reason": "error"})])
        reply = minerva_chat.run_chat("hi", state_dir=td,
                                      engine_factory=factory)
        check("error with no text -> honest error reply",
              reply == minerva_chat.ERROR_REPLY)
        check("errors recorded on her line",
              chat_lines(td)[-1].get("errors") == ["api error: boom"])

        factory, _h = factory_for([ev(EventType.DONE,
                                      {"stop_reason": "end_turn"})])
        check("empty engine output -> honest empty reply",
              minerva_chat.run_chat("hi2", state_dir=td,
                                    engine_factory=factory)
              == minerva_chat.EMPTY_REPLY)

        factory, _h = factory_for([
            ev(EventType.MESSAGE, {"text": "x" * 3000}),
            ev(EventType.DONE, {"stop_reason": "end_turn"})])
        reply = minerva_chat.run_chat("long", state_dir=td,
                                      engine_factory=factory)
        check("reply capped under the DM limit",
              len(reply) <= minerva_chat.REPLY_CAP + 20
              and reply.endswith("...[truncated]"))

        factory_raises_called = {"n": 0}

        def factory_raises(state_dir):
            factory_raises_called["n"] += 1
            raise RuntimeError("engine exploded")

        reply = minerva_chat.run_chat("hi3", state_dir=td,
                                      engine_factory=factory_raises)
        check("factory exception -> error reply, no crash",
              reply == minerva_chat.ERROR_REPLY
              and factory_raises_called["n"] == 1)
        check("lock released after factory exception",
              not os.path.exists(os.path.join(td, "drive-tick.lock")))

    # ---------- run_chat: engine lock (tick running) --------------------------
    with tempfile.TemporaryDirectory() as td:
        lock = os.path.join(td, "drive-tick.lock")
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 1, "ts": time.time()}, f)
        called = {"n": 0}

        def must_not_run(state_dir):
            called["n"] += 1
            raise AssertionError("engine must not start while lock is held")

        reply = minerva_chat.run_chat("you there?", state_dir=td,
                                      engine_factory=must_not_run)
        check("fresh lock -> busy reply, engine untouched",
              reply == minerva_chat.BUSY_REPLY and called["n"] == 0)
        check("foreign lock NOT released", os.path.exists(lock))
        check("busy exchange still logged",
              [r.get("kind") for r in chat_lines(td)][-1] == "busy")

        # stale lock (dead tick) gets displaced, chat proceeds
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 1, "ts": time.time() - 7200}, f)
        factory, _h = factory_for([ev(EventType.MESSAGE, {"text": "back"}),
                                   ev(EventType.DONE,
                                      {"stop_reason": "end_turn"})])
        check("stale lock displaced, chat proceeds",
              minerva_chat.run_chat("hi", state_dir=td,
                                    engine_factory=factory) == "back")

    # ---------- host_loop hook: free text -> chat reply ------------------------
    with tempfile.TemporaryDirectory() as td:
        ledger = os.path.join(td, "ledger.jsonl")
        verdicts = os.path.join(td, "verdicts.jsonl")
        approvals = os.path.join(td, "approvals.jsonl")
        inbox = os.path.join(td, "inbox.jsonl")
        files_dir = os.path.join(td, "files")
        calls = {"n": 0}

        def handler(text: str) -> str:
            calls["n"] += 1
            return "HI THERE"

        kw = dict(ledger_path=ledger, verdicts_path=verdicts,
                  approvals_path=approvals, inbox_path=inbox,
                  files_dir=files_dir)
        reply, kind = host_loop.process_reply("m1", "hello there", [],
                                              chat_handler=handler, **kw)
        check("free text -> live chat reply",
              (reply, kind) == ("HI THERE", "chat") and calls["n"] == 1)
        check("inbox line still written first",
              host_loop.read_jsonl(inbox)[0]["text"] == "hello there")

        reply, kind = host_loop.process_reply("m1", "hello there", [],
                                              chat_handler=handler, **kw)
        check("replayed msg dedupes silently, no second chat",
              (reply, kind) == (None, "duplicate") and calls["n"] == 1)

        def broken(text: str) -> str:
            raise RuntimeError("chat down")

        reply, kind = host_loop.process_reply("m2", "second msg", [],
                                              chat_handler=broken, **kw)
        check("handler failure falls back to queue ack",
              (reply, kind) == ("noted - Minerva reads this at her next run",
                                "message"))

        att = {"filename": "pic.jpg", "size": 10, "url": "http://x/y"}
        reply, kind = host_loop.process_reply(
            "m3", "look at this", [att], chat_handler=handler,
            fetch=lambda url: b"0123456789", **kw)
        check("files note + chat reply combine",
              kind == "chat"
              and reply.startswith("got 1 file(s) - queued for Minerva's next run")
              and reply.endswith("HI THERE"))

        calls_before = calls["n"]
        reply, kind = host_loop.process_reply("m4", "never", [],
                                              chat_handler=handler, **kw)
        check("command words still route to decisions, not chat",
              kind == "hint" and "no pitches are waiting" in reply
              and calls["n"] == calls_before)

        reply, kind = host_loop.process_reply("m5", "plain note", [], **kw)
        check("no handler -> old queue-ack behavior",
              (reply, kind) == ("noted - Minerva reads this at her next run",
                                "message"))

    # ---------- switches -------------------------------------------------------
    old = os.environ.get("MINERVA_CHAT")
    try:
        os.environ["MINERVA_CHAT"] = "0"
        check("MINERVA_CHAT=0 disables the handler",
              minerva_chat.build_chat_handler() is None)
        os.environ["MINERVA_CHAT"] = "1"
        check("MINERVA_CHAT=1 yields a callable",
              callable(minerva_chat.build_chat_handler()))
    finally:
        if old is None:
            os.environ.pop("MINERVA_CHAT", None)
        else:
            os.environ["MINERVA_CHAT"] = old
    check("_build_chat(False) is None", host_loop._build_chat(False) is None)

    # ---------- round 20: spend_status on the chat allowlist ----------
    check("CHAT_TOOLS carries spend_status (round-20 live find)",
          "spend_status" in minerva_chat.CHAT_TOOLS)
    check("persona tells her to use spend_status, never guess costs",
          "spend_status" in minerva_chat.PERSONA)

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
