"""Watchdog (round 18) - notices when the daemon stops noticing.

host_loop --watch writes state/heartbeat.json every poll cycle. This script
- run by hand or every 15 minutes via Scheduled Task - checks the pulse:

  fresh    -> prints ok, exits 0 (silent in scheduled use)
  stale    -> ALERTS the operator and exits 1
  missing  -> same (the daemon has never run, or someone deleted the pulse)

Independence: the alert is its OWN Discord DM, sent directly with the bot
token from config/discord.env - NOT through host_loop (whose hang is exactly
the news) and NOT through the gateway/outbox (delivery of which needs the
hung daemon). A hung daemon cannot suppress its own obituary. No Discord
config -> the alert still prints + logs (scheduled console window).

Anti-spam: one alert per stale episode per MINERVA_WATCHDOG_COOLDOWN_S
(default 3600); a recovered-then-stale-again daemon alerts afresh.
Staleness threshold: MINERVA_WATCHDOG_STALE_S (default 180 - many multiples
of the daemon's poll cycle). State: state/watchdog-state.json; log:
state/watchdog-log.jsonl. Read-only over everything that is hers; this is
the operator's tool, like the warden - it runs fine while she is STOPPED.

Schedule (after a manual first run proves it):
  python scripts\\watchdog.py --show-schedule
Stdlib only. ASCII.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.environ.get("MINERVA_STATE_DIR", "").strip() or \
    os.path.join(ROOT, "state")
DISCORD_ENV = os.path.join(ROOT, "config", "discord.env")
DISCORD_API = os.environ.get("MINERVA_WATCHDOG_DISCORD_API", "").strip() or \
    "https://discord.com/api/v10"


def say(s: str) -> None:
    print(str(s).encode("ascii", "backslashreplace").decode("ascii"))


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def load_discord() -> dict | None:
    """{token, user_id} or None. Mirrors host_loop's env-file format."""
    vals = {}
    if os.path.exists(DISCORD_ENV):
        with open(DISCORD_ENV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip() or \
        vals.get("DISCORD_BOT_TOKEN", "")
    user = os.environ.get("DISCORD_USER_ID", "").strip() or \
        vals.get("DISCORD_USER_ID", "")
    return {"token": token, "user_id": user} if token and user else None


def _post(url: str, token: str, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=True).encode("ascii"),
        method="POST", headers={"Authorization": f"Bot {token}",
                                "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def send_alert_dm(cfg: dict, text: str) -> bool:
    """Direct DM, independent of host_loop. False on any failure."""
    try:
        ch = _post(DISCORD_API + "/users/@me/channels", cfg["token"],
                   {"recipient_id": cfg["user_id"]}).get("id")
        if not ch:
            return False
        _post(DISCORD_API + f"/channels/{ch}/messages", cfg["token"],
              {"content": text[:1900]})
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, ValueError):
        return False


def heartbeat_age(state_dir: str) -> float | None:
    """Seconds since the last pulse; None when there has never been one."""
    p = os.path.join(state_dir, "heartbeat.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return max(0.0, time.time() - float(json.load(f).get("ts", 0)))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return float("inf")                     # unreadable pulse = bad pulse


def check(state_dir: str = STATE, quiet: bool = False) -> dict:
    stale_s = _f("MINERVA_WATCHDOG_STALE_S", 180.0)
    cooldown_s = _f("MINERVA_WATCHDOG_COOLDOWN_S", 3600.0)
    age = heartbeat_age(state_dir)
    report: dict = {"ts": time.time(), "age_s": age, "stale": False,
                    "alerted": False}

    if age is not None and age <= stale_s:
        report["ok"] = True
        if not quiet:
            say(f"daemon pulse OK ({age:.0f}s old, threshold {stale_s:.0f}s)")
        return report

    report["stale"] = True
    report["ok"] = False
    if age is None:
        what = ("Minerva's daemon has NO pulse file - Start Minerva is not "
                "running (or has never run since the watchdog was added).")
    else:
        mins = age / 60.0
        what = (f"Minerva's daemon looks HUNG or DOWN - last pulse "
                f"{mins:.0f} minutes ago (threshold {stale_s / 60:.0f}m).")
    advice = (" Check the Start Minerva window; close and double-click it "
              "again to restart. Deliveries and chat are paused until then.")

    sp = os.path.join(state_dir, "watchdog-state.json")
    last_alert = 0.0
    try:
        with open(sp, encoding="utf-8") as f:
            last_alert = float(json.load(f).get("last_alert_ts", 0))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    if time.time() - last_alert < cooldown_s:
        report["suppressed"] = "cooldown"
        if not quiet:
            say(what + " (already alerted recently - cooldown)")
    else:
        cfg = load_discord()
        sent = send_alert_dm(cfg, "[WATCHDOG] " + what + advice) if cfg \
            else False
        report["alerted"] = sent
        try:
            with open(sp, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"last_alert_ts": time.time()}, f)
        except OSError:
            pass
        if not quiet:
            say(what)
            say("  alert " + ("DM'd to your Discord." if sent else
                              "NOT deliverable by DM (no/bad discord.env) - "
                              "this console line is the alert."))
    try:
        with open(os.path.join(state_dir, "watchdog-log.jsonl"), "a",
                  encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(report, ensure_ascii=True) + "\n")
    except OSError:
        pass
    return report


def schedule_command() -> str:
    py = sys.executable or "python"
    script = os.path.join(ROOT, "scripts", "watchdog.py")
    return ('schtasks /Create /TN "Minerva Watchdog" /SC MINUTE /MO 15 '
            f'/TR "\\"{py}\\" \\"{script}\\""')


if __name__ == "__main__":
    if "--show-schedule" in sys.argv:
        print("after a manual run proves it, register with:")
        print("  " + schedule_command())
        sys.exit(0)
    sys.exit(0 if check().get("ok") else 1)
