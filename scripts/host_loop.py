"""Host loop v0.6 — the operator's side of the governed conversation.

Delivers what the engines produced and collects the operator's decisions:
  outbox.jsonl     notify_operator messages -> console (cursor-tracked) and, when
                   config\\discord.env is filled, DISCORD DM (Minerva texts the operator;
                   separate cursor, so console reads don't eat phone deliveries)
  ledger.jsonl     PITCH entries (pitch_idea) -> console + Discord DM, cursor-
                   tracked like the outbox. READ-ONLY on the ledger (single-
                   appender rule: the gateway owns writes; this loop only looks).
  verdicts.jsonl   the operator's PITCH VERDICTS (v0.4, 2026-06-11): this loop appends
                   decision lines (adopt / never / later / park + note); the
                   GATEWAY folds them into the hash-chained ledger as
                   pitch_verdict entries at its next startup and before any
                   pitch_idea call — same appender-safe pattern as approvals.
                   Verdicts feed the anti-nag ProposalIndex + taste learning.
  approvals.jsonl  gateway ASK escalations -> approve/deny interactively

DISCORD TWO-WAY (v0.5, 2026-06-11, per Minerva-Discord-Two-Way-Design.md):
The operator can now REPLY to Minerva's DMs. poll_discord_inbound() (in --watch and
the one-shot pass) REST-polls the DM channel and parses each reply:
  approve / deny [ref] [note]            -> decision line in approvals.jsonl
  adopt / never / later / park [id] [note] -> decision line in verdicts.jsonl
  anything else                          -> state/discord-inbox.jsonl (context
                                            Minerva reads at her next tick)
Rails: ONLY messages whose author id == DISCORD_USER_ID count (the bot's own
and anyone else's are skipped); the poller writes DECISION FILES only — the
gateway stays the single ledger appender; every decision line carries
{"via": "discord", "msg_id": ...} so a re-poll can never double-record (the
msg_id is checked before recording); the snowflake cursor advances per
message. A bare command word applies when exactly ONE item is waiting,
otherwise the bot DMs back asking which. Confirmation DMs reuse the CLI's
plain-language strings. Attachments auto-download to state/discord-files/
(size-capped, sha256 recorded, never executed). Free text is DATA, never
commands the host loop executes. First poll ever: the cursor bootstraps to
"now" without processing history (no surprise decisions from old messages).

CHAT (v0.6, round 11 — "Minerva answers"): free text no longer just queues.
When a chat handler is wired (the default in --watch, the one-shot pass and
--poll-inbound; see scripts/minerva_chat.py), a free-text DM ALSO opens one
governed engine session and Minerva's actual reply rides back as the DM
(kind "chat"). The inbox line is still written FIRST, so the tick-seed and
consolidation lanes are unchanged, and a crash mid-chat replays as a silent
no-op (msg_id dedupe) rather than a double-reply. Chat failures fall back to
the old "noted" ack — a chat problem can never break decision recording.
Disable with --no-chat or MINERVA_CHAT=0.

Usage (from the project root):
  python scripts\\host_loop.py                  one-shot: new messages + pitches + approvals + verdicts
  python scripts\\host_loop.py verdict          decide on open pitches interactively
  python scripts\\host_loop.py verdict <pitch> <adopt|never|later|park> [--note ".."]
                                                one-line verdict; <pitch> may be any
                                                unique piece of the pitch id
  python scripts\\host_loop.py --status         counts only; advances nothing
  python scripts\\host_loop.py --watch          follow mode: poll outbox + pitches, DM new
                                                ones to Discord, announce pending asks
  python scripts\\host_loop.py --test-discord   send a test DM now ("Minerva online")
  python scripts\\host_loop.py --deliver-discord  one-shot DM delivery of new outbox + pitches
  python scripts\\host_loop.py --poll-inbound   one-shot: read + process Discord replies now
  python scripts\\host_loop.py --no-chat        free text only queues (no live chat replies)

Decision recording (v0.1 semantics):
* approvals.jsonl is an append-only event log. The gateway appends
  {ref, ts, tool, args, reason, status:"pending"}; this loop appends
  {ref, ts, status:"approved"|"denied", by:"operator", note}; the gateway appends
  {ref, status:"consumed", by:"gateway"} when it honors an approval. Latest
  line per ref wins. Nothing is ever rewritten in place.
* The hash-chained ledger is NOT touched here (single-appender rule); the GATE
  ledgers the VERDICT itself on the engine's retry (governance v0.1).
* Approvals are SINGLE-USE; denials STAND per exact call fingerprint.

Discord (v0.2): bot DM via REST. config\\discord.env carries DISCORD_BOT_TOKEN +
DISCORD_USER_ID (gitignored; the bot must share a server with the operator). The DM
channel id is cached in the cursor file. 2000-char API limit -> truncated at
1900. One polite retry on 429. Stdlib urllib only.

Cursor state lives in state/host-loop-cursor.json.
ASCII console output (cp1252 rule); DM content may carry UTF-8.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
OUTBOX = os.path.join(STATE, "outbox.jsonl")
LEDGER = os.path.join(STATE, "ledger.jsonl")
APPROVALS = os.path.join(STATE, "approvals.jsonl")
VERDICTS = os.path.join(STATE, "verdicts.jsonl")
CURSOR = os.path.join(STATE, "host-loop-cursor.json")
DISCORD_INBOX = os.path.join(STATE, "discord-inbox.jsonl")
DISCORD_FILES = os.path.join(STATE, "workspace", "incoming")  # round 19: she can SEE them (files_list -> incoming/...)
HEARTBEAT = os.path.join(STATE, "heartbeat.json")


def write_heartbeat(path: str = HEARTBEAT) -> None:
    """The daemon's pulse (round 18): --watch touches this every cycle; the
    separate watchdog (scripts/watchdog.py) alerts the operator when it goes stale.
    Never raises - a pulse problem must not break delivery."""
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"ts": time.time(), "pid": os.getpid()}, f)
    except OSError:
        pass

# Plain-language aliases -> canonical verdicts. MIRRORS minerva_core/verdicts.py
# (this script stays stdlib-standalone); change BOTH or CLI and fold disagree.
VERDICT_ALIASES = {
    "adopt": "adopt", "yes": "adopt",
    "never": "decline_forever", "decline-forever": "decline_forever",
    "decline_forever": "decline_forever",
    "later": "decline_for_now", "not-now": "decline_for_now",
    "notnow": "decline_for_now", "decline-for-now": "decline_for_now",
    "decline_for_now": "decline_for_now",
    "park": "park",
}
VERDICT_PLAIN = {"adopt": "ADOPT - she may pursue it",
                 "decline_forever": "NEVER - dead forever, similar ideas blocked",
                 "decline_for_now": "LATER - cooldown, then it may come back",
                 "park": "PARK - shelved until you reopen it"}
DISCORD_ENV = os.path.join(ROOT, "config", "discord.env")
DISCORD_API = "https://discord.com/api/v10"     # tests point this at a local mock
DM_CHAR_LIMIT = 1900                            # Discord caps content at 2000

# Inbound two-way (v0.5)
INBOUND_LIMIT = 50                              # messages per poll page
FILE_CAP_BYTES = 8 * 1024 * 1024                # skip larger attachments
APPROVAL_WORDS = {"approve": True, "deny": False}
# an id "piece" the operator pasted: hex chars with optional dash, e.g. c6151454,
# 19eb74af71a-c6151454. Short hexy words ("dead") fall back to note text
# unless they actually match a waiting item.
_ID_PIECE_RE = re.compile(r"[0-9a-f]+(-[0-9a-f]+)?", re.IGNORECASE)

# Confirmation strings — SAME plain language the CLI prints (design rule:
# Discord replies and the Inbox prompt answer in one voice).
CONFIRM_APPROVED = ("recorded: approved - the engine's next IDENTICAL call "
                    "passes the gate once (single-use; gateway ledgers the "
                    "VERDICT)")
CONFIRM_DENIED = ("recorded: denied - denial STANDS for this exact call; "
                  "changed arguments would escalate fresh")
CONFIRM_FOLD_TAIL = ("Minerva folds it into her ledger at her next run - the "
                     "anti-nag machine honors it from then on.")


# ---------- shared plumbing (imported by tests + probes) --------------------
def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue                      # tolerate a torn/garbled line
            if isinstance(rec, dict):
                out.append(rec)
    return out


def load_cursor(path: str | None = None) -> dict:
    try:
        with open(path or CURSOR, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cursor(cur: dict, path: str | None = None) -> None:
    path = path or CURSOR
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cur, f)
    os.replace(tmp, path)                     # atomic: temp + rename


def new_outbox(outbox_path: str | None = None, cursor: dict | None = None,
               cursor_key: str = "outbox_delivered") -> tuple[list[dict], int]:
    """Returns (undelivered messages, total message count) for the given cursor."""
    msgs = read_jsonl(outbox_path or OUTBOX)
    seen = int((cursor or {}).get(cursor_key, 0))
    return msgs[seen:], len(msgs)


def new_pitches(ledger_path: str | None = None, cursor: dict | None = None,
                cursor_key: str = "pitches_announced") -> tuple[list[dict], int]:
    """Unannounced PITCH entries from the hash-chained ledger. READ-ONLY:
    the gateway is the single ledger appender; this loop only looks."""
    pitches = [e for e in read_jsonl(ledger_path or LEDGER)
               if e.get("type") == "pitch"]
    seen = int((cursor or {}).get(cursor_key, 0))
    return pitches[seen:], len(pitches)


def fmt_pitch(e: dict) -> str:
    p = e.get("payload") or {}
    return (f"[PITCH] {p.get('title', '?')} ({p.get('domain', 'general')})\n"
            f"{str(p.get('detail', ''))[:600]}\n"
            f"(pitch {e.get('id', '?')} - your verdict: adopt / never / "
            "later / park - decide in Minerva - Inbox)")


def pending_approvals(approvals_path: str | None = None) -> list[dict]:
    """Latest status per ref wins; only refs still 'pending' come back,
    oldest first, each carrying the original ask fields."""
    asks: dict[str, dict] = {}
    status: dict[str, str] = {}
    order: list[str] = []
    for rec in read_jsonl(approvals_path or APPROVALS):
        ref = rec.get("ref")
        if not ref:
            continue
        if rec.get("tool") is not None and ref not in asks:
            asks[ref] = rec
            order.append(ref)
        status[ref] = rec.get("status", "pending")
    return [asks[r] for r in order if status.get(r) == "pending" and r in asks]


def record_decision(ref: str, approved: bool, note: str = "",
                    approvals_path: str | None = None,
                    extra: dict | None = None) -> dict:
    rec = {"ref": ref, "ts": time.time(),
           "status": "approved" if approved else "denied",
           "by": "operator", "note": note}
    if extra:
        rec.update(extra)                     # provenance, e.g. via/msg_id
    with open(approvals_path or APPROVALS, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=True) + "\n")
    return rec


def record_verdict_line(pitch_id: str, verdict_word: str, note: str = "",
                        cooldown_days: float = 30.0, reopen_condition: str = "",
                        verdicts_path: str | None = None,
                        extra: dict | None = None) -> dict:
    """Append the operator's decision to verdicts.jsonl (NEVER the ledger — the
    gateway folds it in). Accepts plain-language aliases."""
    v = VERDICT_ALIASES.get(verdict_word.strip().lower().replace(" ", "-"))
    if v is None:
        raise ValueError(f"unknown verdict '{verdict_word}' "
                         "(use: adopt / never / later / park)")
    rec = {"pitch": pitch_id, "ts": time.time(), "verdict": v,
           "note": str(note), "by": "operator", "status": "decided"}
    if v == "decline_for_now":
        rec["cooldown_days"] = float(cooldown_days)
    if v == "park":
        rec["reopen_condition"] = str(reopen_condition)
    if extra:
        rec.update(extra)                     # provenance, e.g. via/msg_id
    with open(verdicts_path or VERDICTS, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=True, sort_keys=True) + "\n")
    return rec


def verdict_state(ledger_path: str | None = None,
                  verdicts_path: str | None = None) -> tuple[list[dict], int]:
    """(pitches still awaiting the operator's verdict, decisions recorded but not yet
    folded by the gateway). A pitch is 'awaiting' if the ledger has no
    pitch_verdict child AND verdicts.jsonl has no pending decided line."""
    entries = read_jsonl(ledger_path or LEDGER)
    pitches = [e for e in entries if e.get("type") == "pitch"]
    verdicted = {e.get("parent_id") for e in entries
                 if e.get("type") == "pitch_verdict"}
    last: dict[str, dict] = {}
    for rec in read_jsonl(verdicts_path or VERDICTS):
        pid = rec.get("pitch")
        if pid:
            last[pid] = rec
    awaiting_fold = sum(1 for r in last.values() if r.get("status") == "decided")
    waiting = [e for e in pitches
               if e["id"] not in verdicted
               and last.get(e["id"], {}).get("status") != "decided"]
    return waiting, awaiting_fold


def resolve_pitch_ref(ref: str, ledger_path: str | None = None) -> tuple[str | None, str]:
    """Match any unique piece of a pitch id (prefix, tail, substring).
    Returns (pitch_id, reason_if_none)."""
    ref = ref.strip()
    pitches = [e for e in read_jsonl(ledger_path or LEDGER)
               if e.get("type") == "pitch"]
    hits = [e for e in pitches if ref and ref in e.get("id", "")]
    if len(hits) == 1:
        return hits[0]["id"], ""
    if not hits:
        return None, f"no pitch matches '{ref}'"
    return None, (f"'{ref}' matches {len(hits)} pitches - paste more of the id: "
                  + ", ".join(e["id"] for e in hits[:5]))


def fmt_age(ts: float) -> str:
    d = max(0, time.time() - ts)
    if d < 90:
        return f"{int(d)}s ago"
    if d < 5400:
        return f"{int(d / 60)}m ago"
    return f"{d / 3600:.1f}h ago"


# ---------- discord DM lane (v0.2) -------------------------------------------
def load_discord_config(path: str | None = None) -> dict | None:
    """Parse KEY=VALUE lines; None unless both token and user id are present."""
    path = path or DISCORD_ENV
    if not os.path.exists(path):
        return None
    cfg: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    if cfg.get("DISCORD_BOT_TOKEN") and cfg.get("DISCORD_USER_ID"):
        return cfg
    return None


def _discord_request(method: str, path: str, token: str,
                     body: dict | None = None) -> tuple[int, dict | list]:
    """One request + one polite retry on 429. Returns (status, parsed_json) —
    a dict for most endpoints, a LIST for GET channel messages."""
    url = DISCORD_API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in (1, 2):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (minerva-host-loop, 0.2)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                try:
                    retry_after = float(json.loads(e.read().decode("utf-8", "replace"))
                                        .get("retry_after", 1.0))
                except Exception:                                 # noqa: BLE001
                    retry_after = 1.0
                time.sleep(min(retry_after, 10.0))
                continue
            return e.code, {}
        except (urllib.error.URLError, OSError):
            return 0, {}
    return 0, {}


def discord_dm_channel(cfg: dict, cursor: dict) -> str | None:
    """DM channel id, cached in the cursor (create-DM is idempotent anyway)."""
    cached = cursor.get("discord_channel_id")
    if cached:
        return str(cached)
    status, d = _discord_request("POST", "/users/@me/channels",
                                 cfg["DISCORD_BOT_TOKEN"],
                                 {"recipient_id": cfg["DISCORD_USER_ID"]})
    if status == 200 and isinstance(d, dict) and d.get("id"):
        cursor["discord_channel_id"] = str(d["id"])
        return str(d["id"])
    print(f"discord: create-DM failed (http {status}) - token ok? bot shares a "
          "server with you?", file=sys.stderr)
    return None


def send_discord_dm(cfg: dict, channel_id: str, text: str) -> bool:
    if len(text) > DM_CHAR_LIMIT:
        text = text[:DM_CHAR_LIMIT] + " ...[truncated]"
    status, _ = _discord_request("POST", f"/channels/{channel_id}/messages",
                                 cfg["DISCORD_BOT_TOKEN"], {"content": text})
    return status == 200


def send_discord_file(cfg: dict, channel_id: str, path: str,
                      content: str = "") -> bool:
    """Round 19: DM a real ATTACHMENT (multipart upload). One 429 retry.
    Used for generated images/videos; size is pre-checked by the queueing
    tool, but Discord has the final say."""
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return False
    name = _safe_filename(os.path.basename(path))
    boundary = ("MinervaFile"
                + hashlib.sha256((name + str(len(blob))).encode()).hexdigest()[:20])
    payload = json.dumps({"content": content[:DM_CHAR_LIMIT]},
                         ensure_ascii=True)
    head = (f"--{boundary}\r\nContent-Disposition: form-data; "
            f"name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n"
            f"{payload}\r\n--{boundary}\r\nContent-Disposition: form-data; "
            f"name=\"files[0]\"; filename=\"{name}\"\r\nContent-Type: "
            "application/octet-stream\r\n\r\n").encode("ascii")
    body = head + blob + f"\r\n--{boundary}--\r\n".encode("ascii")
    for attempt in (1, 2):
        req = urllib.request.Request(
            DISCORD_API + f"/channels/{channel_id}/messages", data=body,
            method="POST", headers={
                "Authorization": f"Bot {cfg['DISCORD_BOT_TOKEN']}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "DiscordBot (minerva-host-loop, 0.2)"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                time.sleep(1.0)
                continue
            return False
        except (urllib.error.URLError, OSError):
            return False
    return False


def deliver_outbox_discord(cfg: dict | None = None, quiet: bool = False) -> int:
    """DM every not-yet-DMed outbox message; cursor advances per success so a
    mid-batch failure resumes exactly where it stopped."""
    cfg = cfg or load_discord_config()
    if cfg is None:
        if not quiet:
            print("discord: not configured (fill config\\discord.env)")
        return 0
    cur = load_cursor()
    fresh, total = new_outbox(cursor=cur, cursor_key="discord_delivered")
    if not fresh:
        return 0
    channel = discord_dm_channel(cfg, cur)
    if channel is None:
        save_cursor(cur)                       # may have cached nothing; harmless
        return 0
    sent = 0
    base = int(cur.get("discord_delivered", 0))
    for m in fresh:
        flag = "[HIGH] " if m.get("priority") == "high" else ""
        if m.get("file"):                      # round 19: attachment lane
            rel = str(m["file"])
            fpath = os.path.join(os.path.dirname(OUTBOX), "workspace",
                                 rel.replace("/", os.sep))
            note = f"{flag}{m.get('message', '')}".strip()
            if os.path.isfile(fpath):
                ok_send = send_discord_file(cfg, channel, fpath, note)
            else:
                ok_send = send_discord_dm(
                    cfg, channel, (note + f" (file missing: {rel})").strip())
        else:
            ok_send = send_discord_dm(cfg, channel,
                                      f"{flag}{m.get('message', '')}")
        if not ok_send:
            print(f"discord: send failed after {sent} message(s); will retry "
                  "next pass", file=sys.stderr)
            break
        sent += 1
    cur["discord_delivered"] = base + sent
    save_cursor(cur)
    if sent and not quiet:
        print(f"discord: delivered {sent} message(s)")
    return sent


def deliver_pitches_discord(cfg: dict | None = None, quiet: bool = False) -> int:
    """DM every not-yet-DMed pitch; per-pitch cursor advance like the outbox."""
    cfg = cfg or load_discord_config()
    if cfg is None:
        return 0
    cur = load_cursor()
    fresh, _total = new_pitches(cursor=cur, cursor_key="pitches_dmed")
    if not fresh:
        return 0
    channel = discord_dm_channel(cfg, cur)
    if channel is None:
        save_cursor(cur)
        return 0
    sent = 0
    base = int(cur.get("pitches_dmed", 0))
    for e in fresh:
        if not send_discord_dm(cfg, channel, fmt_pitch(e)):
            print(f"discord: pitch DM failed after {sent}; will retry next pass",
                  file=sys.stderr)
            break
        sent += 1
    cur["pitches_dmed"] = base + sent
    save_cursor(cur)
    if sent and not quiet:
        print(f"discord: delivered {sent} pitch(es)")
    return sent


# ---------- discord two-way: inbound replies (v0.5) ---------------------------
def parse_reply(text: str, pitch_ids: list[str] | tuple = (),
                approval_refs: list[str] | tuple = ()) -> dict:
    """Parse one Discord reply against the small command grammar. PURE — the
    caller supplies what is currently waiting; nothing here touches files.

    Returns one of:
      {"kind": "approval", "approve": bool, "ref": str|None, "note": str,
       "error": str|None}
      {"kind": "verdict", "word": str, "pitch": str|None, "note": str,
       "error": str|None}
      {"kind": "message"}                      (free text -> queued as context)

    ref/pitch stays None for a bare command word — the dispatcher applies the
    exactly-one-waiting rule. An id piece must look hexy (see _ID_PIECE_RE)
    AND match uniquely; a short hexy word that matches nothing ("dead", in
    'never dead simple') is treated as note text, but 6+ chars of hex that
    match nothing get an error so a typoed id never silently becomes a note.
    Free text is DATA — it is never executed, only queued.
    """
    tokens = str(text or "").split()
    if not tokens:
        return {"kind": "message"}

    def clean(tok: str) -> str:
        return tok.lower().strip(".,!:;")

    first = clean(tokens[0])
    rest = tokens[1:]
    if first == "not" and rest and clean(rest[0]) == "now":   # "not now"
        first, rest = "not-now", rest[1:]

    if first in APPROVAL_WORDS:
        lane, pool = "approval", list(approval_refs)
    elif first in VERDICT_ALIASES:
        lane, pool = "verdict", list(pitch_ids)
    else:
        return {"kind": "message"}

    ref: str | None = None
    error: str | None = None
    note_tokens = rest
    if rest:
        cand = rest[0].strip(".,;:")
        if _ID_PIECE_RE.fullmatch(cand):
            hits = [i for i in pool if cand.lower() in i.lower()]
            if len(hits) == 1:
                ref, note_tokens = hits[0], rest[1:]
            elif len(hits) > 1:
                error = (f"'{cand}' matches {len(hits)} - paste more of the id: "
                         + ", ".join(hits[:5]))
            elif len(cand) >= 6:
                error = f"nothing waiting matches '{cand}'"
            # short hexy word, no match -> it was note text, not an id
    note = " ".join(note_tokens).strip()
    if lane == "approval":
        return {"kind": "approval", "approve": APPROVAL_WORDS[first],
                "ref": ref, "note": note, "error": error}
    return {"kind": "verdict", "word": first, "pitch": ref, "note": note,
            "error": error}


def _already_recorded(msg_id: str, path: str) -> bool:
    """Idempotence rail: has this Discord message already produced a line?"""
    return any(r.get("via") == "discord" and str(r.get("msg_id")) == str(msg_id)
               for r in read_jsonl(path))


def _safe_filename(name: str) -> str:
    base = os.path.basename(str(name) or "file")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    return base[:120]


def _fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "DiscordBot (minerva-host-loop, 0.5)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(FILE_CAP_BYTES + 1)


def download_attachment(att: dict, msg_id: str, files_dir: str | None = None,
                        fetch=None) -> dict:
    """Save one DM attachment under state/discord-files/. Size-capped, sha256
    recorded, NEVER executed — it is bytes on disk, nothing more."""
    files_dir = files_dir or DISCORD_FILES
    name = _safe_filename(att.get("filename", "file"))
    size = int(att.get("size", 0) or 0)
    if size > FILE_CAP_BYTES:
        return {"filename": name, "size": size,
                "skipped": f"exceeds {FILE_CAP_BYTES // (1024 * 1024)}MB cap"}
    try:
        data = (fetch or _fetch_url)(str(att.get("url", "")))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"filename": name, "error": f"download failed: {exc}"}
    if len(data) > FILE_CAP_BYTES:
        return {"filename": name, "size": len(data),
                "skipped": f"exceeds {FILE_CAP_BYTES // (1024 * 1024)}MB cap"}
    os.makedirs(files_dir, exist_ok=True)
    path = os.path.join(files_dir, f"{msg_id}-{name}")
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return {"filename": name, "path": path, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def process_reply(msg_id: str, text: str, attachments: list[dict] | None = None,
                  ledger_path: str | None = None,
                  verdicts_path: str | None = None,
                  approvals_path: str | None = None,
                  inbox_path: str | None = None,
                  files_dir: str | None = None,
                  fetch=None, chat_handler=None) -> tuple[str | None, str]:
    """Route one of THE OPERATOR's messages. Returns (reply_dm_text_or_None, kind).
    kind: approval | verdict | message | chat | hint | duplicate | empty.
    Writes DECISION FILES + the inbox only — never the ledger. chat_handler
    (text -> reply), when given, turns free text into a live chat reply."""
    attachments = attachments or []
    # Idempotence FIRST, before any parsing: a replayed message (cursor
    # rollback after a crash) must be a silent no-op even though its item
    # is no longer in the waiting pools (it was decided the first time).
    for dedup_path in (verdicts_path or VERDICTS, approvals_path or APPROVALS,
                       inbox_path or DISCORD_INBOX):
        if _already_recorded(msg_id, dedup_path):
            return (None, "duplicate")
    waiting, _ = verdict_state(ledger_path, verdicts_path)
    pend = pending_approvals(approvals_path)
    # Explicit ids resolve against ALL pitches (CLI parity: changing your
    # mind on a decided pitch appends a fresh line, newest wins at fold);
    # the bare-word rule below stays strict to the WAITING list.
    all_pitch_ids = [e["id"] for e in read_jsonl(ledger_path or LEDGER)
                     if e.get("type") == "pitch"]
    parsed = parse_reply(text, all_pitch_ids,
                         [p["ref"] for p in pend])

    if parsed["kind"] == "approval":
        if parsed["error"]:
            return (f"could not record that: {parsed['error']}", "hint")
        ref = parsed["ref"]
        if ref is None:
            if len(pend) == 1:
                ref = pend[0]["ref"]
            elif not pend:
                return ("nothing is waiting for approval right now", "hint")
            else:
                return (f"{len(pend)} approvals are waiting - reply "
                        "'approve <ref>' or 'deny <ref>': "
                        + ", ".join(p["ref"] for p in pend), "hint")
        record_decision(ref, parsed["approve"], parsed["note"], approvals_path,
                        extra={"via": "discord", "msg_id": str(msg_id)})
        base = CONFIRM_APPROVED if parsed["approve"] else CONFIRM_DENIED
        return (f"{base} (ref {ref})", "approval")

    if parsed["kind"] == "verdict":
        if parsed["error"]:
            return (f"could not record that: {parsed['error']}", "hint")
        pid = parsed["pitch"]
        if pid is None:
            if len(waiting) == 1:
                pid = waiting[0]["id"]
            elif not waiting:
                msg = "no pitches are waiting on a verdict right now"
                if pend:
                    msg += (f" - but {len(pend)} approval(s) are: reply "
                            "'approve' or 'deny'")
                return (msg, "hint")
            else:
                return (f"{len(waiting)} pitches are waiting - say which, e.g. "
                        "'never <id piece>': "
                        + ", ".join(e["id"] for e in waiting), "hint")
        rec = record_verdict_line(pid, parsed["word"], parsed["note"],
                                  verdicts_path=verdicts_path,
                                  extra={"via": "discord",
                                         "msg_id": str(msg_id)})
        return (f"recorded: {VERDICT_PLAIN[rec['verdict']]}. "
                f"{CONFIRM_FOLD_TAIL} (pitch {pid})", "verdict")

    # free text and/or files -> context for Minerva's next run
    text = str(text or "").strip()
    files = [download_attachment(a, str(msg_id), files_dir, fetch)
             for a in attachments]
    if not text and not files:
        return (None, "empty")
    line = {"ts": time.time(), "via": "discord", "msg_id": str(msg_id),
            "text": text}
    if files:
        line["files"] = files
    with open(inbox_path or DISCORD_INBOX, "a", encoding="utf-8",
              newline="\n") as f:
        f.write(json.dumps(line, ensure_ascii=True, sort_keys=True) + "\n")
    note = None
    if files:
        saved = sum(1 for x in files if x.get("path"))
        skipped = len(files) - saved
        note = f"got {saved} file(s) - queued for Minerva's next run"
        if skipped:
            note += f" ({skipped} skipped: too large or failed)"
    if text and chat_handler is not None:    # chat v1 (round 11): she answers
        try:
            chat_reply = chat_handler(text)
        except Exception as exc:                                  # noqa: BLE001
            print(f"chat: handler failed ({exc!r}) - queue-ack fallback",
                  file=sys.stderr)
            chat_reply = None
        if chat_reply:
            return ((f"{note}\n{chat_reply}" if note else chat_reply), "chat")
    if note:
        return (note, "message")
    return ("noted - Minerva reads this at her next run", "message")


def poll_discord_inbound(cfg: dict | None = None, quiet: bool = True,
                         chat_handler=None) -> int:
    """Read new DM-channel messages and process THE OPERATOR's (author id must equal
    DISCORD_USER_ID; the bot's own confirmations and anyone else are skipped).
    Snowflake cursor advances per message AFTER its decision line is written
    (duplicate-beats-lost; msg_id dedupe makes the duplicate a no-op). First
    ever poll only bootstraps the cursor to the newest message — old history
    must never turn into surprise decisions."""
    cfg = cfg or load_discord_config()
    if cfg is None:
        return 0
    cur = load_cursor()
    channel = discord_dm_channel(cfg, cur)
    if channel is None:
        save_cursor(cur)
        return 0
    after = cur.get("discord_inbound_last_id")
    path = f"/channels/{channel}/messages?limit={INBOUND_LIMIT}"
    if after:
        path += f"&after={after}"
    status, msgs = _discord_request("GET", path, cfg["DISCORD_BOT_TOKEN"])
    if status != 200 or not isinstance(msgs, list):
        if not quiet and status != 200:
            print(f"discord-in: poll failed (http {status})", file=sys.stderr)
        save_cursor(cur)                       # keep a freshly cached channel
        return 0
    msgs.sort(key=lambda m: int(m.get("id", 0)))
    if not after:
        if msgs:
            cur["discord_inbound_last_id"] = str(msgs[-1]["id"])
            if not quiet:
                print("discord-in: two-way starts NOW (older messages left "
                      "alone)")
        save_cursor(cur)
        return 0
    processed = 0
    for m in msgs:
        mid = str(m.get("id", ""))
        author = str((m.get("author") or {}).get("id", ""))
        if author == str(cfg["DISCORD_USER_ID"]):
            reply, kind = process_reply(mid, m.get("content", ""),
                                        m.get("attachments") or [],
                                        chat_handler=chat_handler)
            if reply:
                send_discord_dm(cfg, channel, reply)
            if kind not in ("empty", "duplicate"):
                processed += 1
                if not quiet:
                    print(f"discord-in: {kind} (msg {mid})")
        elif not quiet:
            print(f"discord-in: skipped non-operator author (msg {mid})")
        cur["discord_inbound_last_id"] = mid   # decision written; now advance
        save_cursor(cur)
    return processed


# ---------- interaction ------------------------------------------------------
def show_history(limit: int = 20, ledger_path: str | None = None,
                 outbox_path: str | None = None,
                 approvals_path: str | None = None) -> None:
    """Look BACK at everything already delivered. Read-only: no cursor moves,
    nothing gets marked read (the operator asked where his earlier pitch went -
    delivery is one-time, this is the rewind)."""
    pitches = [e for e in read_jsonl(ledger_path or LEDGER)
               if e.get("type") == "pitch"]
    print(f"--- all pitches so far ({len(pitches)}) ---")
    if not pitches:
        print("  (none yet)")
    for e in pitches:
        print("  " + fmt_pitch(e).replace("\n", "\n  "))
        print()
    msgs = read_jsonl(outbox_path or OUTBOX)
    shown = msgs[-limit:]
    print(f"--- last {len(shown)} of {len(msgs)} message(s) ---")
    if not msgs:
        print("  (none yet)")
    for m in shown:
        flag = "[HIGH] " if m.get("priority") == "high" else ""
        print(f"  {fmt_age(m.get('ts', 0)):>9}  {flag}{m.get('message', '')}")
    pend = pending_approvals(approvals_path)
    if pend:
        print(f"--- {len(pend)} approval(s) still waiting - use Minerva - Inbox ---")


def deliver_pitches(advance: bool = True) -> int:
    cur = load_cursor()
    fresh, total = new_pitches(cursor=cur)
    if fresh:
        print(f"\n--- pitches: {len(fresh)} new ---")
        for e in fresh:
            print("  " + fmt_pitch(e).replace("\n", "\n  "))
        if advance:
            cur["pitches_announced"] = total
            save_cursor(cur)
    else:
        print("pitches: nothing new")
    return len(fresh)


def deliver_outbox(advance: bool = True) -> int:
    cur = load_cursor()
    fresh, total = new_outbox(cursor=cur)
    if fresh:
        print(f"\n--- outbox: {len(fresh)} new message(s) ---")
        for m in fresh:
            pri = m.get("priority", "normal")
            flag = "[HIGH] " if pri == "high" else ""
            print(f"  {fmt_age(m.get('ts', 0)):>9}  {flag}{m.get('message', '')}")
        if advance:
            cur["outbox_delivered"] = total
            save_cursor(cur)
    else:
        print("outbox: nothing new")
    return len(fresh)


def resolve_approvals(interactive: bool = True,
                      approvals_path: str | None = None) -> int:
    pend = pending_approvals(approvals_path)
    if not pend:
        print("approvals: none pending")
        return 0
    print(f"\n--- approvals: {len(pend)} pending ---")
    for p in pend:
        print(f"\n  ref    {p.get('ref')}   ({fmt_age(p.get('ts', 0))})")
        print(f"  tool   {p.get('tool')}")
        print(f"  args   {json.dumps(p.get('args', {}), ensure_ascii=True)[:300]}")
        print(f"  reason {p.get('reason')}")
        if not interactive:
            continue
        while True:
            ans = input("  [a]pprove / [d]eny / [s]kip > ").strip().lower()
            if ans in ("a", "d"):
                note = input("  note (optional) > ").strip()
                rec = record_decision(p["ref"], ans == "a", note, approvals_path)
                if rec["status"] == "approved":
                    print("  recorded: approved - the engine's next IDENTICAL call"
                          " passes the gate once (single-use; gateway ledgers the"
                          " VERDICT)")
                else:
                    print("  recorded: denied - denial STANDS for this exact call;"
                          " changed arguments would escalate fresh")
                break
            if ans == "s" or ans == "":
                print("  skipped (stays pending)")
                break
    return len(pend)


def resolve_verdicts(interactive: bool = True, ledger_path: str | None = None,
                     verdicts_path: str | None = None) -> int:
    """Prompt the operator for a verdict on each pitch that still has none.
    Skipping keeps a pitch open (it will ask again next time)."""
    waiting, awaiting_fold = verdict_state(ledger_path, verdicts_path)
    if awaiting_fold:
        print(f"verdicts: {awaiting_fold} recorded - Minerva folds them in at "
              "her next run (Brainstorm Now / daily tick)")
    if not waiting:
        print("verdicts: no pitches waiting on you")
        return 0
    print(f"\n--- verdicts: {len(waiting)} pitch(es) waiting on you ---")
    for e in waiting:
        p = e.get("payload") or {}
        print(f"\n  pitch  {e.get('id')}   ({fmt_age(e.get('ts', 0))})")
        print(f"  title  {p.get('title', '?')}  [{p.get('domain', 'general')}]")
        detail = str(p.get("detail", ""))[:300]
        if detail:
            print(f"  detail {detail}")
        if not interactive:
            continue
        while True:
            ans = input("  [a]dopt / [n]ever / [l]ater / [p]ark / [s]kip > ").strip().lower()
            if ans in ("s", ""):
                print("  skipped (stays open, asks again next time)")
                break
            word = {"a": "adopt", "n": "never", "l": "later", "p": "park"}.get(ans)
            if word is None:
                continue
            note = input("  note for Minerva (optional) > ").strip()
            days = 30.0
            reopen = ""
            if word == "later":
                d = input("  cooldown days before she may re-pitch [30] > ").strip()
                try:
                    days = float(d) if d else 30.0
                except ValueError:
                    days = 30.0
            if word == "park":
                reopen = input("  reopen when... (optional) > ").strip()
            record_verdict_line(e["id"], word, note, days, reopen, verdicts_path)
            print(f"  recorded: {VERDICT_PLAIN[VERDICT_ALIASES[word]]}.")
            print("  Minerva folds it into her ledger at her next run - the "
                  "anti-nag machine honors it from then on.")
            break
    return len(waiting)


def _build_chat(enabled: bool = True):
    """Import the chat lane lazily; a chat problem must never break delivery."""
    if not enabled:
        return None
    try:
        import minerva_chat
        return minerva_chat.build_chat_handler(quiet=False)
    except Exception as exc:                                      # noqa: BLE001
        print(f"chat: unavailable ({exc!r}) - free text queues only",
              file=sys.stderr)
        return None


def _mail_events_check() -> int:
    """Round 21: the mail-event lane. Lazy import + blanket catch - an event
    problem must never break delivery (same rule as the chat lane)."""
    try:
        import mail_events
        return mail_events.check(quiet=False)
    except Exception as exc:                                      # noqa: BLE001
        print(f"mail-events: skipped ({exc!r})", file=sys.stderr)
        return 0


def watch(poll_s: float, chat: bool = True) -> None:
    cfg = load_discord_config()
    chat_handler = _build_chat(chat) if cfg else None
    lane = ("console + Discord DM (two-way: your replies count)" if cfg
            else "console only (discord.env not filled)")
    chat_lane = ("chat ON - she replies to free text" if chat_handler
                 else "chat off - free text queues for her next tick")
    print(f"watching outbox + pitches + approvals every {poll_s:.0f}s - {lane} - {chat_lane} (Ctrl+C to stop)")
    announced_refs: set[str] = set()
    try:
        while True:
            write_heartbeat()                      # round 18: watchdog pulse
            n_mail = _mail_events_check()          # round 21: mail events
            n_out = deliver_outbox(advance=True)
            n_dm = deliver_outbox_discord(cfg, quiet=False) if cfg else 0
            n_pitch = deliver_pitches(advance=True)
            n_pitch_dm = deliver_pitches_discord(cfg, quiet=False) if cfg else 0
            n_in = (poll_discord_inbound(cfg, quiet=False,
                                         chat_handler=chat_handler)
                    if cfg else 0)
            pend = pending_approvals()
            new_refs = [p["ref"] for p in pend if p["ref"] not in announced_refs]
            if pend:
                print(f"approvals: {len(pend)} PENDING - run without --watch to resolve")
                if cfg and new_refs:
                    cur = load_cursor()
                    ch = discord_dm_channel(cfg, cur)
                    save_cursor(cur)
                    if ch:
                        for p in pend:
                            if p["ref"] in new_refs:
                                send_discord_dm(cfg, ch,
                                                f"[APPROVAL NEEDED] {p.get('tool')} "
                                                f"{json.dumps(p.get('args', {}), ensure_ascii=True)[:200]} "
                                                f"- {p.get('reason')} (ref {p['ref']}; "
                                                "decide in host_loop)")
                                announced_refs.add(p["ref"])
            if (not n_out and not n_dm and not n_pitch and not n_pitch_dm
                    and not n_in and not pend and not n_mail):
                sys.stdout.write(".")
                sys.stdout.flush()
            time.sleep(poll_s)
    except KeyboardInterrupt:
        print("\nstopped")


def verdict_command(argv: list[str], note: str, days: float, reopen: str) -> int:
    """`host_loop.py verdict [...]` - no args: interactive; else one-liner."""
    if not argv:
        resolve_verdicts()
        return 0
    if len(argv) < 2:
        print("usage: host_loop.py verdict <pitch-id-piece> <adopt|never|later|park>"
              " [--note ..] [--days N] [--reopen-when ..]")
        return 1
    pid, why = resolve_pitch_ref(argv[0])
    if pid is None:
        print(f"verdict: {why}")
        return 1
    try:
        rec = record_verdict_line(pid, argv[1], note, days, reopen)
    except ValueError as exc:
        print(f"verdict: {exc}")
        return 1
    print(f"recorded for pitch {pid}: {VERDICT_PLAIN[rec['verdict']]}.")
    print("Minerva folds it into her ledger at her next run (Brainstorm Now / "
          "daily tick) - the anti-nag machine honors it from then on.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="minerva-host-loop")
    ap.add_argument("cmd", nargs="*",
                    help="optional subcommand: verdict [pitch-id-piece] [decision]")
    ap.add_argument("--note", default="", help="verdict: note for Minerva")
    ap.add_argument("--days", type=float, default=30.0,
                    help="verdict 'later': cooldown days (default 30)")
    ap.add_argument("--reopen-when", default="", dest="reopen_when",
                    help="verdict 'park': reopen condition")
    ap.add_argument("--status", action="store_true", help="counts only; no cursor advance")
    ap.add_argument("--watch", action="store_true", help="follow outbox; DM + announce asks")
    ap.add_argument("--poll", type=float, default=10.0, help="watch poll seconds")
    ap.add_argument("--test-discord", action="store_true",
                    help="send a test DM now and exit")
    ap.add_argument("--deliver-discord", action="store_true",
                    help="one-shot: DM all new outbox messages + pitches and exit")
    ap.add_argument("--poll-inbound", action="store_true",
                    help="one-shot: read + process your Discord replies and exit")
    ap.add_argument("--history", action="store_true",
                    help="re-show everything already delivered (read-only)")
    ap.add_argument("--no-chat", action="store_true",
                    help="free text only queues (no live chat replies)")
    args = ap.parse_args()

    if args.cmd:
        if args.cmd[0] == "verdict":
            return verdict_command(args.cmd[1:], args.note, args.days,
                                   args.reopen_when)
        print(f"unknown command: {args.cmd[0]} (did you mean 'verdict'?)")
        return 1
    if args.test_discord:
        cfg = load_discord_config()
        if cfg is None:
            print("discord: fill config\\discord.env first (token + user id)")
            return 1
        cur = load_cursor()
        ch = discord_dm_channel(cfg, cur)
        save_cursor(cur)
        if ch and send_discord_dm(cfg, ch, "Minerva online - delivery channel test."):
            print("discord: test DM sent - check your phone")
            return 0
        print("discord: test DM FAILED (see stderr above)")
        return 1
    if args.history:
        show_history()
        return 0
    if args.deliver_discord:
        deliver_outbox_discord()
        deliver_pitches_discord()
        return 0
    if args.poll_inbound:
        cfg = load_discord_config()
        if cfg is None:
            print("discord: fill config\\discord.env first (token + user id)")
            return 1
        n = poll_discord_inbound(cfg, quiet=False,
                                 chat_handler=_build_chat(not args.no_chat))
        print(f"discord-in: {n} repl{'y' if n == 1 else 'ies'} processed")
        return 0
    if args.status:
        cur = load_cursor()
        fresh, total = new_outbox(cursor=cur)
        dm_fresh, _ = new_outbox(cursor=cur, cursor_key="discord_delivered")
        p_fresh, p_total = new_pitches(cursor=cur)
        p_dm, _ = new_pitches(cursor=cur, cursor_key="pitches_dmed")
        print(f"outbox: {len(fresh)} new / {total} total (console); "
              f"{len(dm_fresh)} undelivered to Discord")
        print(f"pitches: {len(p_fresh)} new / {p_total} total (console); "
              f"{len(p_dm)} not yet DMed")
        print(f"approvals pending: {len(pending_approvals())}")
        waiting, awaiting_fold = verdict_state()
        print(f"verdicts: {len(waiting)} pitch(es) waiting on you; "
              f"{awaiting_fold} recorded, folds at Minerva's next run")
        print(f"discord: {'configured' if load_discord_config() else 'NOT configured'}")
        inbox = read_jsonl(DISCORD_INBOX)
        two_way = ("live" if cur.get("discord_inbound_last_id")
                   else "starts at next watch/poll")
        print(f"discord two-way: {two_way}; {len(inbox)} queued message(s) "
              "for Minerva's next run")
        chat_log = read_jsonl(os.path.join(STATE, "chat-log.jsonl"))
        chat_off = (args.no_chat
                    or os.environ.get("MINERVA_CHAT", "1").strip().lower()
                    in ("0", "off", "no", "false"))
        print(f"chat: {'OFF' if chat_off else 'on (watch/poll replies live)'}; "
              f"{len(chat_log)} transcript line(s)")
        return 0
    if args.watch:
        watch(args.poll, chat=not args.no_chat)
        return 0
    deliver_outbox()
    deliver_pitches()
    cfg = load_discord_config()
    if cfg:                      # record phone replies BEFORE prompting here,
        poll_discord_inbound(cfg, quiet=False,   # so the Inbox never re-asks
                             chat_handler=_build_chat(not args.no_chat))
    resolve_approvals()
    resolve_verdicts()
    return 0


if __name__ == "__main__":
    sys.exit(main())
