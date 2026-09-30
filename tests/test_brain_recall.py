"""Brain recall v1 tests (round 12): manifest routing, caps, exclusion zones,
and the READ-ONLY guarantee. Stdlib only:  python3 tests/test_brain_recall.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core import brain_recall as br
from minerva_core.adapters.anthropic_byok import AnthropicByokAdapter
from minerva_core.gateway.tools_builtin import build_default_registry
from minerva_core.ledger import Ledger, ProposalIndex

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def w(root: str, rel: str, text: str) -> None:
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def build_brain(tmp: str) -> str:
    b = os.path.join(tmp, "brain")
    w(b, "MANIFEST.md", "\n".join([
        "# The operator's Brain — Manifest",
        "",
        "## Core facts about the operator",
        "name: Operator",
        "vehicle: owns a 2019 Honda Civic  (2026-05)",
        "residence: 100 Example St, Springfield, IL  (2026-05)",
        "",
        "## Routing — open deeper files only when relevant",
        "- preferences/food.md — meals, dinner planning, foods the operator likes",
        "- people/_index.md - anything about a specific person in the operator's life",
        "",
        "## How to RECALL",
        "ignored: this section is not core facts",
    ]) + "\n")
    w(b, "preferences/food.md", "likes: lentil soup\nplan: dinner on Sundays is pasta\n")
    w(b, "topics/competitor.md", "note: dinner was mentioned here once\n")
    w(b, "topics/a.md", "operator thinks blue cheese is great\n")
    w(b, "topics/b.md", "the blue sky\na cheese plate\n")
    w(b, "people/robin.md", "relationship_to_me: neighbor\nunicode: café — ok\n")
    # exclusion zones - the ONLY places the decoy token lives
    w(b, "backups/old.md", "zanzibar in a backup\n")
    w(b, "inbox-drop/drop-x.md", "zanzibar in an unreviewed drop\n")
    w(b, "system/notes.md", "zanzibar in automation files\n")
    w(b, "log.md", "zanzibar in the audit log\n")
    w(b, "bigfile.md", ("zanzibar then filler\n" + "x" * br.MAX_FILE_BYTES) + "\n")
    return b


def snapshot(root: str) -> dict:
    out = {}
    for r, _d, files in os.walk(root):
        for fn in files:
            p = os.path.join(r, fn)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def test_tokenize() -> None:
    check("stopwords dropped", br.tokenize("What do you know about my car") == ["car"])
    check("all-stopword query keeps raw tokens",
          br.tokenize("who is she") == ["who", "is", "she"])


def test_manifest_parse(brain: str) -> None:
    with open(os.path.join(brain, "MANIFEST.md"), encoding="utf-8") as f:
        core, routes = br.parse_manifest(f.read())
    check("core facts parsed", any(l.startswith("vehicle:") for _n, l in core))
    check("core lineno is int", all(isinstance(n, int) for n, _l in core))
    paths = [p for p, _d in routes]
    check("em-dash route parsed", "preferences/food.md" in paths)
    check("hyphen route parsed", "people/_index.md" in paths)
    check("non-route sections ignored",
          all(not l.startswith("ignored") for _n, l in core))


def test_search(brain: str) -> None:
    out = br.search(brain, "civic")
    check("core fact surfaces", "Core facts" in out and "Honda Civic" in out)
    check("citation cites manifest line", "MANIFEST.md L" in out)

    out = br.search(brain, "dinner")
    fpos = out.find("preferences/food.md")
    cpos = out.find("topics/competitor.md")
    check("routed file boosted above plain match", 0 <= fpos < cpos)
    check("line citations present", " L" in out and "dinner" in out.lower())

    out = br.search(brain, "blue cheese")
    check("phrase match outranks scattered tokens",
          0 <= out.find("topics/a.md") < out.find("topics/b.md"))

    out = br.search(brain, "zanzibar")
    check("backups/inbox-drop/system/log/bigfile all excluded",
          "no Brain matches" in out)

    out = br.search(brain, "neighbor café")
    check("output is ASCII-safe", all(ord(c) < 128 for c in out))
    check("unicode content still findable", "people/robin.md" in out)


def test_caps(brain: str) -> None:
    for i in range(10):
        w(brain, f"topics/cap{i}.md", "pelican facts here\n")
    out = br.search(brain, "pelican", max_results=2)
    check("max_results honored", out.count("-- topics/cap") == 2 and "[capped" in out)
    out = br.search(brain, "pelican", max_results=99999)
    check("max_results clamped", out.count("-- topics/cap") <= br.HARD_MAX_FILES)
    out = br.search(brain, "pelican", char_budget=600)
    check("char budget enforced", len(out) <= 600)
    long_line = "pelican " * 200
    w(brain, "topics/longline.md", long_line + "\n")
    out = br.search(brain, "pelican", max_results=20)
    check("quoted lines truncated",
          all(len(l) < br.LINE_CAP + 40 for l in out.splitlines()))


def test_read_only(brain: str) -> None:
    before = snapshot(brain)
    br.search(brain, "civic")
    br.search(brain, "zanzibar")
    br.search(brain, "")
    check("recall never writes (sizes+mtimes identical)", snapshot(brain) == before)


def test_edges(tmp: str, brain: str) -> None:
    check("missing brain dir is honest",
          "brain unavailable" in br.search(os.path.join(tmp, "nope"), "x"))
    check("empty query is honest", "empty query" in br.search(brain, "  "))
    check("no-match message names the query",
          "wombat" in br.search(brain, "wombat"))


def test_wiring(tmp: str, brain: str) -> None:
    old = os.environ.pop("MINERVA_BRAIN", None)
    try:
        a = AnthropicByokAdapter(api_key="sk-test", state_dir=tmp)
        i = a.gateway_argv.index("--brain")
        check("BYOK default lane wires the Brain",
              a.gateway_argv[i + 1] == br.DEFAULT_BRAIN)
        os.environ["MINERVA_BRAIN"] = brain
        a2 = AnthropicByokAdapter(api_key="sk-test", state_dir=tmp)
        check("MINERVA_BRAIN env overrides the default",
              a2.gateway_argv[a2.gateway_argv.index("--brain") + 1] == brain)
        explicit = [sys.executable, "x.py", "--state-dir", tmp]
        a3 = AnthropicByokAdapter(api_key="sk-test", gateway_argv=explicit)
        check("explicit gateway_argv untouched", a3.gateway_argv == explicit)
    finally:
        os.environ.pop("MINERVA_BRAIN", None)
        if old is not None:
            os.environ["MINERVA_BRAIN"] = old

    state = os.path.join(tmp, "state-reg")
    os.makedirs(state, exist_ok=True)
    ledger = Ledger(os.path.join(state, "ledger.jsonl"))
    reg = build_default_registry(ledger, ProposalIndex(ledger),
                                 outbox_path=os.path.join(state, "outbox.jsonl"),
                                 brain_dir=brain)
    spec = reg.get("brain_recall")
    check("registry registers brain_recall", spec is not None)
    check("tool description states READ-ONLY", "READ-ONLY" in spec.description)
    check("no workspace -> no capability tools", reg.get("web_fetch") is None)
    out = spec.handler({"query": "civic"})
    check("gateway handler reaches real search", "Honda Civic" in out)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        brain = build_brain(tmp)
        test_tokenize()
        test_manifest_parse(brain)
        test_search(brain)
        test_caps(brain)
        test_read_only(brain)
        test_edges(tmp, brain)
        test_wiring(tmp, brain)
    print(f"\nall {PASSED} checks passed")
