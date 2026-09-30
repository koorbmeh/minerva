r"""Episodic consolidation v1.1 — the only path from Minerva's state toward the Brain.

Engines record observations via the gateway's episodic_note tool -> NOTE
entries in the hash-chained ledger (Minerva's own episodic memory). Engines
have NO Brain write path (locked decision 2026-06-09). This host-side pass
promotes durable CANDIDATES out of the episodic ledger into Brain/inbox-drop/
— the Brain's existing cross-surface capture pattern — where the daily
consolidation job (the filer) + the operator review, file, or reject them. Nothing
here touches Brain files proper: inbox-drop IS the review gate.

Ledger access is READ-ONLY (single-appender rule: the gateway owns ledger
writes; a second appender forks the chain). This pass writes only drop files
and its own cursor (state/consolidation-cursor.json).

Selection: NOTE entries with no parent_id (parented notes are pitch-reopen
controls, not episodic facts). Filters are mechanical only — length floor,
exact-dupe hash, near-dupe token overlap (ledger.similar) against this batch
plus a rolling memory of prior promotions. Durability JUDGMENT stays with the
filer and the operator; a host script cannot make that call.

v1.1 (2026-06-11, operator_input_loop): The operator's OWN words are the highest-signal
facts in Minerva's state, so three more lanes promote them as operator-attributed
candidates (same review gate, same dedupe memory):
  * pitch_verdict ledger entries (actor=operator)  -> his verdict + reason, with
    the parent pitch's title for context. OWN CURSOR starting at 0, so
    verdicts the old notes-cursor already passed are picked up retroactively.
  * approvals.jsonl approve/deny decision lines whose note is substantial
    (bare yes/no clicks are not facts).
  * discord-inbox.jsonl free-text messages the operator sent Minerva.
Each lane keeps its own cursor key (verdicts_consumed / approvals_consumed /
inbox_consumed); the notes lane and its cap behave exactly as v0. The new
lanes are uncapped — they are operator-paced and rare.

Usage (from the project root):
  python scripts\consolidate.py             one-shot: promote new notes
  python scripts\consolidate.py --status    counts only; advances nothing
  python scripts\consolidate.py --dry-run   show would-be drop; advances nothing

Crash ordering: drop file FIRST, cursor second — a crash in between re-emits
duplicate candidates next run (the filer dedupes); a fact is never lost.
Torn ledger tail (gateway mid-append): stop before it; cursor never passes
bytes that may still be in flight.
ASCII console output (cp1252 rule); drop files are UTF-8.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from minerva_core.ledger import similar, slugify  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STATE = os.path.join(ROOT, "state")
DEFAULT_BRAIN = os.environ.get("MINERVA_BRAIN") or os.path.join(os.path.expanduser("~"), "Brain")

MIN_CHARS = 10            # shorter than this is noise, not a fact
ROLLING_CAP = 500         # cross-run dedupe memory ([hash, slug] pairs)
DEFAULT_MAX_PER_RUN = 20  # don't flood the filer; the rest waits for next run

# Plain labels for composed verdict facts (v1.1)
VERDICT_LABELS = {"adopt": "ADOPT", "decline_forever": "NEVER (dead forever)",
                  "decline_for_now": "LATER (cooldown)", "park": "PARK (shelved)"}


# ---------- plumbing (imported by tests) ------------------------------------
def read_ledger_entries(path: str) -> tuple[list[dict], bool]:
    """All COMPLETE entries in order. Stops at the first torn/garbled line and
    reports clean=False — the cursor must never advance past bytes the gateway
    may still be appending."""
    if not os.path.exists(path):
        return [], True
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return out, False
            if isinstance(rec, dict):
                out.append(rec)
            else:
                return out, False
    return out, True


def load_cursor(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cursor(cur: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cur, f)
    os.replace(tmp, path)                     # atomic: temp + rename


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def fact_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def _ascii(s: str) -> str:
    return s.encode("ascii", "backslashreplace").decode("ascii")


# ---------- selection ---------------------------------------------------------
def select_candidates(entries: list[dict], start: int, seen: list[list],
                      max_notes: int = DEFAULT_MAX_PER_RUN
                      ) -> tuple[list[dict], int, dict]:
    """Walk entries[start:]; collect promotable note payloads.

    Returns (candidates, n_entries_consumed, skip_counters). `seen` is the
    rolling [hash, slug] memory from the cursor — MUTATED: each promoted fact
    is appended so later batches and later runs dedupe against it.
    """
    cands: list[dict] = []
    counters = {"skipped_short": 0, "skipped_dupe": 0,
                "skipped_parented": 0, "skipped_other_types": 0}
    seen_hashes = {h for h, _ in seen}
    seen_slugs = [s for _, s in seen]
    consumed = 0
    for e in entries[start:]:
        if len(cands) >= max_notes:
            break                              # cap hit: stop BEFORE consuming
        consumed += 1
        if e.get("type") != "note":
            counters["skipped_other_types"] += 1
            continue
        if e.get("parent_id"):                 # pitch-reopen control, not episodic
            counters["skipped_parented"] += 1
            continue
        payload = e.get("payload") or {}
        text = str(payload.get("text", "")).strip()
        if len(text) < MIN_CHARS:
            counters["skipped_short"] += 1
            continue
        h = fact_hash(text)
        slug = slugify(text)[:120]
        if h in seen_hashes or any(similar(slug, s) for s in seen_slugs):
            counters["skipped_dupe"] += 1
            continue
        cands.append({"id": e.get("id", ""), "ts": float(e.get("ts", 0.0)),
                      "text": text,
                      "about": str(payload.get("about", "")).strip(),
                      "source": str(payload.get("source", "engine"))})
        seen_hashes.add(h)
        seen_slugs.append(slug)
        seen.append([h, slug])
    return cands, consumed, counters


def _is_dupe(text: str, seen: list[list]) -> bool:
    """Shared rolling-memory dedupe for the v1.1 lanes. Appends when fresh."""
    h = fact_hash(text)
    slug = slugify(text)[:120]
    if h in {hh for hh, _ in seen} or any(similar(slug, s) for _, s in seen):
        return True
    seen.append([h, slug])
    return False


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def select_verdict_candidates(entries: list[dict], start: int,
                              seen: list[list]) -> tuple[list[dict], int, int]:
    """The operator's pitch_verdict entries -> composed, operator-attributed facts.
    Walks entries[start:] on its OWN cursor (a different lane over the same
    ledger — starting at 0 on first v1.1 run picks up older verdicts the
    notes cursor already passed). Returns (candidates, consumed, dupes)."""
    pitches = {e.get("id"): (e.get("payload") or {})
               for e in entries if e.get("type") == "pitch"}
    cands: list[dict] = []
    consumed = 0
    dupes = 0
    for e in entries[start:]:
        consumed += 1
        if e.get("type") != "pitch_verdict" or e.get("actor") != "operator":
            continue
        p = e.get("payload") or {}
        parent = pitches.get(e.get("parent_id") or "", {})
        title = str(parent.get("title", "?"))
        domain = str(parent.get("domain", "general"))
        verdict = str(p.get("verdict", "?"))
        label = VERDICT_LABELS.get(verdict, verdict)
        text = (f'Operator\'s verdict on Minerva\'s pitch "{title}" [{domain}]: '
                f"{label}")
        reason = str(p.get("reason", "")).strip()
        if reason:
            text += f' - his words: "{reason[:500]}"'
        if verdict == "park" and str(p.get("reopen_condition", "")).strip():
            text += f" (reopen when: {str(p['reopen_condition'])[:200]})"
        if verdict == "decline_for_now" and p.get("cooldown_days"):
            text += f" (cooldown {p['cooldown_days']}d)"
        if _is_dupe(text, seen):
            dupes += 1
            continue
        cands.append({"id": e.get("id", ""), "ts": float(e.get("ts", 0.0)),
                      "text": text, "about": "Operator", "source": "operator-verdict",
                      "origin": (f"minerva ledger {e.get('id', '')} "
                                 f"(pitch_verdict by the operator, "
                                 f"{_when(float(e.get('ts', 0.0)))})")})
    return cands, consumed, dupes


def select_approval_candidates(recs: list[dict], start: int,
                               seen: list[list]) -> tuple[list[dict], int, int]:
    """The operator's approve/deny decision lines with a substantial note. The note
    is the fact (a bare yes/no click is not); the asked tool gives context.
    Returns (candidates, consumed, dupes)."""
    tools: dict[str, str] = {}
    for r in recs:                      # ask lines carry the tool, any position
        if r.get("ref") and r.get("tool") is not None:
            tools.setdefault(str(r["ref"]), str(r["tool"]))
    cands: list[dict] = []
    consumed = 0
    dupes = 0
    for r in recs[start:]:
        consumed += 1
        if r.get("by") != "operator" or r.get("status") not in ("approved",
                                                             "denied"):
            continue
        note = str(r.get("note", "")).strip()
        if len(note) < MIN_CHARS:
            continue                    # bare clicks are not facts
        ref = str(r.get("ref", ""))
        tool = tools.get(ref, "a tool")
        text = (f"Operator {r['status']} Minerva's request to run {tool}: "
                f'"{note[:500]}"')
        if _is_dupe(text, seen):
            dupes += 1
            continue
        via = " via Discord" if r.get("via") == "discord" else ""
        cands.append({"id": ref, "ts": float(r.get("ts", 0.0)), "text": text,
                      "about": "Operator", "source": "operator-approval",
                      "origin": (f"approvals.jsonl ref {ref} (the operator's "
                                 f"decision{via}, "
                                 f"{_when(float(r.get('ts', 0.0)))})")})
    return cands, consumed, dupes


def select_inbox_candidates(recs: list[dict], start: int,
                            seen: list[list]) -> tuple[list[dict], int, int]:
    """Free text the operator sent Minerva over Discord -> operator-attributed
    candidates (his words to her are Brain-eligible by standing direction).
    Returns (candidates, consumed, dupes)."""
    cands: list[dict] = []
    consumed = 0
    dupes = 0
    for r in recs[start:]:
        consumed += 1
        text = str(r.get("text", "")).strip()
        if len(text) < MIN_CHARS:
            continue                    # file-only or trivial lines
        composed = f'Operator to Minerva (Discord): "{text[:500]}"'
        if _is_dupe(composed, seen):
            dupes += 1
            continue
        mid = str(r.get("msg_id", ""))
        cands.append({"id": mid, "ts": float(r.get("ts", 0.0)),
                      "text": composed, "about": "Operator",
                      "source": "operator-discord",
                      "origin": (f"discord-inbox msg {mid} "
                                 f"({_when(float(r.get('ts', 0.0)))})")})
    return cands, consumed, dupes


# ---------- drop rendering ----------------------------------------------------
def render_drop(cands: list[dict], now: float) -> str:
    """inbox-drop README template: fact/about/source per candidate; about
    falls back to UNSURE (never guess the subject). v1.1 candidates carry a
    precise 'origin'; engine notes keep the v0 source line."""
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    lines = [f"# capture {stamp} (minerva consolidation)",
             "Candidates promoted from Minerva's state: engine episodic notes",
             "+ the operator's own decisions/messages (operator-attributed lanes).",
             "Verify before filing per inbox-drop/README.md; if ambiguous,",
             "leave with a needs-review note.",
             ""]
    for c in cands:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["ts"]))
        lines.append(f"- fact: {c['text']}")
        lines.append(f"- about: {c['about'] or 'UNSURE'}")
        origin = c.get("origin") or (f"minerva episodic ledger {c['id']} "
                                     f"({c['source']} note, {when})")
        lines.append(f"- source: {origin}")
        lines.append("")
    return "\n".join(lines)


def drop_filename(now: float) -> str:
    stamp = time.strftime("%Y-%m-%d-%H%M", time.localtime(now))
    return f"drop-{stamp}-minerva-notes-{secrets.token_hex(2)}.md"


# ---------- the pass ----------------------------------------------------------
def run(state_dir: str, brain_dir: str, dry_run: bool = False,
        max_per_run: int = DEFAULT_MAX_PER_RUN, quiet: bool = False) -> dict:
    ledger_path = os.path.join(state_dir, "ledger.jsonl")
    approvals_path = os.path.join(state_dir, "approvals.jsonl")
    inbox_path = os.path.join(state_dir, "discord-inbox.jsonl")
    cursor_path = os.path.join(state_dir, "consolidation-cursor.json")
    drop_dir = os.path.join(brain_dir, "inbox-drop")

    entries, clean = read_ledger_entries(ledger_path)
    a_recs, _a_clean = read_ledger_entries(approvals_path)   # stop-at-torn too
    i_recs, _i_clean = read_ledger_entries(inbox_path)
    cur = load_cursor(cursor_path)
    start = int(cur.get("ledger_consumed", 0))
    v_start = int(cur.get("verdicts_consumed", 0))   # absent pre-v1.1 -> 0:
    a_start = int(cur.get("approvals_consumed", 0))  # old verdicts picked up
    i_start = int(cur.get("inbox_consumed", 0))      # retroactively, by design
    seen = [list(x) for x in cur.get("seen", [])
            if isinstance(x, (list, tuple)) and len(x) == 2]

    cands, consumed, counters = select_candidates(entries, start, seen, max_per_run)
    v_cands, v_consumed, v_dupes = select_verdict_candidates(entries, v_start, seen)
    a_cands, a_consumed, a_dupes = select_approval_candidates(a_recs, a_start, seen)
    i_cands, i_consumed, i_dupes = select_inbox_candidates(i_recs, i_start, seen)
    all_cands = cands + v_cands + a_cands + i_cands

    summary: dict = {"ledger_entries": len(entries), "cursor_before": start,
                     "consumed": consumed, "promoted": len(all_cands),
                     "torn_tail": not clean, "drop_file": None,
                     "promoted_notes": len(cands),
                     "promoted_verdicts": len(v_cands),
                     "promoted_approvals": len(a_cands),
                     "promoted_inbox": len(i_cands),
                     "consumed_verdicts": v_consumed,
                     "consumed_approvals": a_consumed,
                     "consumed_inbox": i_consumed}
    summary.update(counters)
    summary["skipped_dupe"] += v_dupes + a_dupes + i_dupes

    if dry_run:
        if not quiet:
            _print_summary(summary, all_cands, dry_run=True)
        return summary

    if all_cands:
        os.makedirs(drop_dir, exist_ok=True)
        name = drop_filename(time.time())
        path = os.path.join(drop_dir, name)
        tmp = os.path.join(drop_dir, f".tmp-{name}")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(render_drop(all_cands, time.time()))
        os.replace(tmp, path)              # the filer never sees a half-written drop
        summary["drop_file"] = path

    if consumed or v_consumed or a_consumed or i_consumed:
        cur["ledger_consumed"] = start + consumed   # drop FIRST, cursor second
        cur["verdicts_consumed"] = v_start + v_consumed
        cur["approvals_consumed"] = a_start + a_consumed
        cur["inbox_consumed"] = i_start + i_consumed
        cur["seen"] = seen[-ROLLING_CAP:]
        save_cursor(cur, cursor_path)

    if not quiet:
        _print_summary(summary, all_cands, dry_run=False)
    return summary


def _print_summary(summary: dict, cands: list[dict], dry_run: bool) -> None:
    mode = "DRY RUN - " if dry_run else ""
    print(f"{mode}ledger entries: {summary['ledger_entries']} | "
          f"cursor: {summary['cursor_before']} -> "
          f"{summary['cursor_before'] + summary['consumed']} | "
          f"promoted: {summary['promoted']} | "
          f"dupes: {summary['skipped_dupe']} short: {summary['skipped_short']}")
    operator_n = (summary.get("promoted_verdicts", 0)
               + summary.get("promoted_approvals", 0)
               + summary.get("promoted_inbox", 0))
    if operator_n:
        print(f"  including {operator_n} thing(s) THE OPERATOR said (verdicts: "
              f"{summary['promoted_verdicts']}, approval notes: "
              f"{summary['promoted_approvals']}, discord messages: "
              f"{summary['promoted_inbox']}) - his words become Brain "
              "candidates")
    if summary["torn_tail"]:
        print("note: torn ledger tail detected - stopped before it; "
              "next run will resume there")
    for c in cands:
        print(f"  + {_ascii(c['text'])[:120]}  (about: {_ascii(c['about'] or 'UNSURE')})")
    if summary.get("drop_file"):
        print(f"drop written: {summary['drop_file']}")
    elif not dry_run and not cands:
        print("nothing to promote")


def main() -> int:
    env = os.environ
    ap = argparse.ArgumentParser(prog="minerva-consolidate")
    ap.add_argument("--state-dir", default=env.get("MINERVA_STATE_DIR") or DEFAULT_STATE)
    ap.add_argument("--brain", default=env.get("MINERVA_BRAIN") or DEFAULT_BRAIN)
    ap.add_argument("--dry-run", action="store_true",
                    help="show would-be promotions; write nothing")
    ap.add_argument("--status", action="store_true",
                    help="counts only; write nothing")
    ap.add_argument("--max-per-run", type=int, default=DEFAULT_MAX_PER_RUN)
    args = ap.parse_args()

    if args.status:
        s = run(args.state_dir, args.brain, dry_run=True,
                max_per_run=args.max_per_run, quiet=True)
        print(f"unconsumed ledger entries: {s['ledger_entries'] - s['cursor_before']} | "
              f"would promote: {s['promoted']} | torn tail: {s['torn_tail']}")
        return 0
    run(args.state_dir, args.brain, dry_run=args.dry_run,
        max_per_run=args.max_per_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
