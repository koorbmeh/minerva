"""Send a REAL governed notify_operator through the production gateway, from CLI.

  python scripts\\say_to_operator.py "message text" [low|normal|high]

Spawns the gateway (MCP stdio, real state dir), calls notify_operator -> the gate
ledgers it and the message lands in state/outbox.jsonl. Deliver it with:
  python scripts\\host_loop.py --deliver-discord   (or --watch / plain run)

Single-writer rule applies: don't run while a Hermes session is alive.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from probe_approval_retry import GatewayClient  # noqa: E402  (reuses the MCP client)


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print('usage: python scripts\\say_to_operator.py "message" [low|normal|high]')
        return 1
    message = sys.argv[1]
    priority = sys.argv[2] if len(sys.argv) > 2 else "normal"
    if priority not in ("low", "normal", "high"):
        priority = "normal"
    gw = GatewayClient()
    try:
        gw.request("initialize", {"protocolVersion": "2025-06-18",
                                  "clientInfo": {"name": "say-to-operator"}})
        gw.notify("notifications/initialized")
        is_err, txt = gw.call("notify_operator", {"message": message,
                                               "priority": priority})
        print(("FAILED: " if is_err else "queued: ") + txt)
        return 1 if is_err else 0
    finally:
        gw.close()


if __name__ == "__main__":
    sys.exit(main())
