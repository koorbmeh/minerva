"""Engine event taxonomy — the contract every adapter translates into.

Spec §2. Nothing upstream of an adapter may depend on engine-native event
shapes; if it isn't expressible here, extend THIS, don't leak.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class EventType(str, Enum):
    TEXT_DELTA = "text_delta"    # streamed chunk            payload: {text}
    THOUGHT = "thought"          # reasoning chunk (not user-facing)  payload: {text}
    MESSAGE = "message"          # completed message         payload: {text}
    TOOL_CALL = "tool_call"      # engine invoked a tool     payload: {tool, args, call_id}
    TOOL_RESULT = "tool_result"  # result handed back        payload: {call_id, ok, result}
    ASK_USER = "ask_user"        # needs the operator               payload: {reason, question, ref}
    COST = "cost"                # metering tick             payload: {input_tokens, output_tokens, usd}
    ERROR = "error"              # engine fault              payload: {message, fatal}
    DONE = "done"                # turn finished             payload: {}


@dataclass
class EngineEvent:
    type: EventType
    session_id: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"type": self.type.value, "session_id": self.session_id,
                "payload": self.payload, "ts": self.ts}
