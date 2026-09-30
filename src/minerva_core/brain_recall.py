"""Brain recall v1 (round 12) - REAL manifest-routed READ-ONLY Brain search.

Replaces the naive substring grep that backed the brain_recall gateway tool.
Honors the operator's brain_access lock: READ-ONLY (this module only ever opens files
for reading; there is deliberately no write/delete API here), size-capped, and
engines never get a Brain write path (episodic_note -> consolidation is the
only road in).

How it routes (the "manifest-routed" part):
  1. MANIFEST.md is parsed for the "Core facts about the operator" block and the
     "## Routing" table. Matching core-fact lines ALWAYS surface first - they
     are the highest-signal answers ("vehicle:", "residence:", ...).
  2. Routing entries whose description matches the query boost the files they
     point at (exact file strongly, same top-level branch weakly) - so a
     question about food finds preferences/food.md even when the filename
     never appears in the query.
  3. Every content .md in the Brain is then scanned line-by-line with simple
     token scoring (+ whole-phrase bonus). Top files win; top lines within a
     file are quoted with file + line citations so Minerva can say where a
     fact came from.

Exclusions: backups/ (stale by definition), inbox-drop/ (unreviewed candidate
facts - the review gate has not passed), system/ + log.md (automation
bookkeeping, noise), .git/__pycache__. Files over MAX_FILE_BYTES are skipped.

Output is ASCII-safe (cp1252 pipe rule) and hard-capped in size.
Stdlib only.
"""
from __future__ import annotations

import os
import re

DEFAULT_BRAIN = os.environ.get("MINERVA_BRAIN") or os.path.join(os.path.expanduser("~"), "Brain")

MANIFEST_NAME = "MANIFEST.md"
SKIP_DIRS = {"backups", "inbox-drop", ".git", "__pycache__", "system"}
SKIP_FILES = {"log.md"}          # root bookkeeping log: noise for recall

MAX_FILE_BYTES = 1_000_000       # skip anything bigger (Brain files are small)
LINE_CAP = 240                   # chars of a quoted line
PER_FILE_LINES = 4               # quoted lines per file
CORE_FACT_CAP = 6                # core-fact lines quoted
DEFAULT_MAX_FILES = 6            # files quoted per recall
HARD_MAX_FILES = 20
DEFAULT_CHAR_BUDGET = 6_000      # total output cap (default)
HARD_CHAR_BUDGET = 9_000

ROUTE_BOOST_FILE = 4.0           # route desc hit -> the exact routed file
ROUTE_BOOST_BRANCH = 1.5         # route desc hit -> same top-level branch
PHRASE_BONUS = 2.0

STOPWORDS = {
    "a", "an", "and", "any", "are", "about", "as", "at", "be", "but", "by",
    "can", "do", "does", "did", "for", "from", "get", "had", "has", "have",
    "he", "her", "his", "how", "i", "in", "is", "it", "its", "operator", "operators",
    "know", "like", "me", "my", "of", "on", "or", "our", "she", "tell",
    "that", "the", "their", "them", "they", "this", "to", "us", "was", "we",
    "what", "when", "where", "which", "who", "why", "will", "with", "you",
    "your",
}


def _ascii(s: str) -> str:
    return str(s).encode("ascii", "backslashreplace").decode("ascii")


def tokenize(query: str) -> list[str]:
    """Lowercase alnum tokens (len >= 2), stopwords dropped - unless that
    would drop everything (so a query like 'who is she about' still works)."""
    toks = re.findall(r"[a-z0-9]{2,}", str(query).lower())
    kept = [t for t in toks if t not in STOPWORDS]
    return kept or toks


def parse_manifest(text: str) -> tuple[list[tuple[int, str]], list[tuple[str, str]]]:
    """-> (core_facts [(lineno, line)], routes [(path, desc_lower)]).

    Core facts: `key: value` lines inside the '## Core facts' section.
    Routes: `- path <dash> when-to-open` bullets inside the '## Routing'
    section (the manifest uses an em-dash; ' - ' also accepted)."""
    core: list[tuple[int, str]] = []
    routes: list[tuple[str, str]] = []
    section = ""
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("## "):
            head = line[3:].lower()
            if head.startswith("core facts"):
                section = "core"
            elif head.startswith("routing"):
                section = "routing"
            else:
                section = ""
            continue
        if section == "core" and re.match(r"^[a-z0-9_]+:", line):
            core.append((i, line))
        elif section == "routing" and line.startswith("- "):
            body = line[2:]
            # split path from description on em-dash (—) or " - "
            m = re.split(r"\s+—\s+|\s+-\s+", body, maxsplit=1)
            if len(m) == 2:
                path = m[0].strip().strip("`")
                routes.append((path.replace("\\", "/"), m[1].lower()))
    return core, routes


def _score_line(line_l: str, tokens: list[str], phrase: str) -> float:
    s = float(sum(1 for t in tokens if t in line_l))
    if s and len(tokens) > 1 and all(t in line_l for t in tokens):
        s += 1.0                                   # all terms on one line
    if phrase and len(phrase) >= 4 and phrase in line_l:
        s += PHRASE_BONUS
    return s


def search(brain_dir: str, query: str, max_results: int = DEFAULT_MAX_FILES,
           char_budget: int = DEFAULT_CHAR_BUDGET) -> str:
    """One read-only recall. Returns ASCII text, never raises on bad input."""
    query = str(query or "").strip()
    if not query:
        return "empty query - give me a few words about what to recall"
    if not brain_dir or not os.path.isdir(brain_dir):
        return f"brain unavailable at {brain_dir!r} - recall is offline"
    try:
        max_results = max(1, min(int(max_results), HARD_MAX_FILES))
    except (TypeError, ValueError):
        max_results = DEFAULT_MAX_FILES
    try:
        char_budget = max(500, min(int(char_budget), HARD_CHAR_BUDGET))
    except (TypeError, ValueError):
        char_budget = DEFAULT_CHAR_BUDGET

    tokens = tokenize(query)
    phrase = query.lower().strip()

    # ---- manifest: core facts + routing boosts ---------------------------
    core_hits: list[tuple[int, str]] = []
    file_boost: dict[str, float] = {}      # relpath -> boost
    branch_boost: dict[str, float] = {}    # top-level dir -> boost
    manifest_path = os.path.join(brain_dir, MANIFEST_NAME)
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8", errors="replace") as f:
                mtext = f.read(MAX_FILE_BYTES)
        except OSError:
            mtext = ""
        core, routes = parse_manifest(mtext)
        for lineno, line in core:
            if _score_line(line.lower(), tokens, phrase) > 0:
                core_hits.append((lineno, line))
        for path, desc in routes:
            hits = sum(1 for t in tokens if t in desc or t in path.lower())
            if not hits:
                continue
            file_boost[path] = file_boost.get(path, 0.0) + ROUTE_BOOST_FILE * hits
            top = path.split("/", 1)[0]
            if "/" in path or path.endswith("/"):
                branch_boost[top.rstrip("/")] = (branch_boost.get(top.rstrip("/"), 0.0)
                                                 + ROUTE_BOOST_BRANCH * hits)

    # ---- content scan -----------------------------------------------------
    scored: list[tuple[float, str, int, list[tuple[float, int, str]]]] = []
    for root, dirs, files in os.walk(brain_dir):   # followlinks=False (default)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel_root = os.path.relpath(root, brain_dir)
        for fn in sorted(files):
            if not fn.endswith(".md") or fn == MANIFEST_NAME:
                continue
            rel = fn if rel_root == "." else f"{rel_root}/{fn}".replace("\\", "/")
            if rel in SKIP_FILES:
                continue
            path = os.path.join(root, fn)
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue
            best: list[tuple[float, int, str]] = []
            total = 0.0
            n_matched = 0
            for i, ln in enumerate(lines, 1):
                s = _score_line(ln.lower(), tokens, phrase)
                if s > 0:
                    total += s
                    n_matched += 1
                    best.append((s, i, ln.strip()))
            if not best:
                continue
            best.sort(key=lambda b: (-b[0], b[1]))
            boost = file_boost.get(rel, 0.0)
            boost += branch_boost.get(rel.split("/", 1)[0], 0.0)
            scored.append((total + boost, rel, n_matched, best[:PER_FILE_LINES]))

    if not scored and not core_hits:
        return _ascii(f"no Brain matches for '{query}' "
                      "- try different words, or it may simply not be recorded")

    scored.sort(key=lambda x: (-x[0], x[1]))

    # ---- assemble within budget -------------------------------------------
    out: list[str] = [_ascii(f"Brain recall (read-only) for: {query}")]
    used = len(out[0])

    def push(s: str) -> bool:
        nonlocal used
        s = _ascii(s)
        if used + len(s) + 1 > char_budget:
            return False
        out.append(s)
        used += len(s) + 1
        return True

    if core_hits:
        push("-- Core facts (MANIFEST.md):")
        for lineno, line in core_hits[:CORE_FACT_CAP]:
            if not push(f"  MANIFEST.md L{lineno}: {line[:LINE_CAP]}"):
                break

    files_shown = 0
    truncated = False
    for total, rel, n_matched, best in scored:
        if files_shown >= max_results:
            truncated = True
            break
        if not push(f"-- {rel} ({n_matched} matching line"
                    f"{'s' if n_matched != 1 else ''}):"):
            truncated = True
            break
        files_shown += 1
        for _s, lineno, text in best:
            if not push(f"  L{lineno}: {text[:LINE_CAP]}"):
                truncated = True
                break
        if truncated and used + 80 > char_budget:
            break
    if truncated or len(scored) > max_results:
        push(f"[capped: {len(scored)} file(s) matched; shown {files_shown}. "
             "Narrow the query for more detail]")
    return "\n".join(out)
