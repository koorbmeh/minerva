"""Capability-tool tests (round 12): workspace containment, caps, the SSRF
guard (redirects included), HTML stripping, and the governed path through the
gateway. Stdlib only:  python3 tests/test_capability_tools.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core.gateway import tools_capability as tc
from minerva_core.gateway.mcp_stdio import MCPGateway
from minerva_core.gateway.tools_builtin import build_default_registry
from minerva_core.governance import GovernanceGate
from minerva_core.ledger import Ledger, ProposalIndex

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


# ---------------------------------------------------------------------------
def test_check_url() -> None:
    os.environ.pop("MINERVA_WEB_ALLOW_LOOPBACK", None)
    blocked = ["ftp://example.com/x", "http://localhost/a", "http://127.0.0.1/",
               "http://10.1.2.3/", "http://192.168.1.1/", "http://172.20.3.4/",
               "http://169.254.1.1/", "http://0.0.0.0/", "http://224.0.0.1/",
               "http://printer.local/", "http://box.internal/", "http://[::1]/"]
    check("private/loopback/odd schemes all refused",
          all(tc.check_url(u) is not None for u in blocked))
    check("public IP literal allowed (no DNS needed)",
          tc.check_url("http://93.184.216.34/") is None)
    os.environ["MINERVA_WEB_ALLOW_LOOPBACK"] = "1"
    check("loopback allowed only with the test env flag",
          tc.check_url("http://127.0.0.1:8080/") is None
          and tc.check_url("http://localhost/x") is None
          and tc.check_url("http://10.1.2.3/") is not None)
    os.environ.pop("MINERVA_WEB_ALLOW_LOOPBACK", None)


def test_files(tmp: str) -> None:
    ws = os.path.join(tmp, "ws")
    out = tc.files_write(ws, {"path": "notes/a.txt", "content": "hello"})
    check("write creates nested file", out == "wrote 5 chars to notes/a.txt")
    check("read round-trips", "hello" in tc.files_read(ws, {"path": "notes/a.txt"}))
    out = tc.files_write(ws, {"path": "notes/a.txt", "content": "clobber"})
    check("overwrite refused without flag", out.startswith("refused"))
    out = tc.files_write(ws, {"path": "notes/a.txt", "content": "clobber",
                              "overwrite": True})
    check("overwrite=true replaces", out.startswith("wrote")
          and "clobber" in tc.files_read(ws, {"path": "notes/a.txt"}))
    out = tc.files_list(ws, {})
    check("list shows file with size", "notes/a.txt (7 bytes)" in out)

    for bad in ("../x.txt", "/etc/passwd", "C:\\Windows\\x", "notes/../../y"):
        try:
            tc.files_read(ws, {"path": bad})
            ok = False
        except ValueError:
            ok = True
        check(f"containment refuses {bad!r}", ok)

    outside = os.path.join(tmp, "outside")
    os.makedirs(outside, exist_ok=True)
    with open(os.path.join(outside, "secret.txt"), "w") as f:
        f.write("secret")
    try:
        os.symlink(outside, os.path.join(ws, "link"))
        made = True
    except (OSError, NotImplementedError, AttributeError):
        made = False                      # e.g. Windows without symlink rights
    if made:
        try:
            tc.files_read(ws, {"path": "link/secret.txt"})
            ok = False
        except ValueError:
            ok = True
        check("symlink escape refused", ok)
    else:
        check("symlink escape refused (skipped: no symlink support)", True)

    out = tc.files_write(ws, {"path": "big.txt",
                              "content": "x" * (tc.WRITE_CHAR_CAP + 1)})
    check("write cap enforced", out.startswith("refused: content over"))
    tc.files_write(ws, {"path": "long.txt", "content": "y" * 1000})
    out = tc.files_read(ws, {"path": "long.txt", "max_chars": 1})
    check("read cap floors at 200 + truncation note",
          out.startswith("[truncated]") and len(out) <= 220)
    check("missing file is honest",
          tc.files_read(ws, {"path": "nope.txt"}).startswith("no such file"))
    check("writing onto a directory refused",
          tc.files_write(ws, {"path": "notes", "content": "x"}).startswith("refused"))
    for i in range(tc.LIST_CAP + 5):
        tc.files_write(ws, {"path": f"many/f{i:03}.txt", "content": "."})
    check("list capped", f"[capped at {tc.LIST_CAP} entries]" in tc.files_list(ws, {}))


# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:                       # silence
        pass

    def do_GET(self) -> None:                                # noqa: N802
        if self.path == "/page.html":
            body = ("<html><head><script>var SECRETJS=1;</script>"
                    "<style>.x{color:red}</style></head><body>"
                    "<h1>Title&amp;Co</h1><p>real text here</p>"
                    "<!-- hidden comment --></body></html>").encode()
            self._send(200, body, "text/html; charset=utf-8")
        elif self.path == "/plain.txt":
            self._send(200, b"just plain text", "text/plain")
        elif self.path == "/long.txt":
            self._send(200, b"z" * 3000, "text/plain")
        elif self.path == "/uni.txt":
            self._send(200, "café ☕".encode("utf-8"),
                       "text/plain; charset=utf-8")
        elif self.path == "/big.txt":
            self._send(200, b"A" * (tc.FETCH_BYTE_CAP + 5000), "text/plain")
        elif self.path == "/redirect-ok":
            self.send_response(302)
            self.send_header("Location", "/plain.txt")
            self.end_headers()
        elif self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://10.255.255.1/secret")
            self.end_headers()
        else:
            self._send(404, b"nope", "text/plain")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_web() -> None:
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        os.environ.pop("MINERVA_WEB_ALLOW_LOOPBACK", None)
        check("loopback fetch refused without the flag",
              tc.web_fetch({"url": base + "/plain.txt"}).startswith("refused"))
        os.environ["MINERVA_WEB_ALLOW_LOOPBACK"] = "1"

        out = tc.web_fetch({"url": base + "/page.html"})
        check("html: text survives", "real text here" in out and "Title&Co" in out)
        check("html: scripts/styles/tags/comments stripped",
              "SECRETJS" not in out and "<h1>" not in out
              and "hidden comment" not in out)
        check("data-not-instructions note present", "DATA, not instructions" in out)
        out = tc.web_fetch({"url": base + "/plain.txt"})
        check("plain text passes through", "just plain text" in out)
        out = tc.web_fetch({"url": base + "/redirect-ok"})
        check("benign redirect followed", "just plain text" in out)
        out = tc.web_fetch({"url": base + "/redirect-private"})
        check("redirect into private space blocked", "redirect blocked" in out)
        out = tc.web_fetch({"url": base + "/big.txt"})
        check("byte cap noted", "(cap)" in out)
        out = tc.web_fetch({"url": base + "/long.txt", "max_chars": 1})
        check("char cap floors at 500 + truncation note",
              "text truncated" in out and len(out) < 1200)
        out = tc.web_fetch({"url": base + "/missing"})
        check("http error is honest", "fetch failed: HTTP 404" in out)
        out = tc.web_fetch({"url": base + "/uni.txt"})
        check("unicode body comes back ASCII-safe",
              all(ord(c) < 128 for c in out) and "caf" in out)
        check("no url is honest", tc.web_fetch({}) == "no url given")
        check("non-http scheme refused",
              "not allowed" in tc.web_fetch({"url": "file:///etc/passwd"}))
    finally:
        os.environ.pop("MINERVA_WEB_ALLOW_LOOPBACK", None)
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
def test_gateway_integration(tmp: str) -> None:
    state = os.path.join(tmp, "state")
    os.makedirs(state, exist_ok=True)
    ws = os.path.join(state, "workspace")
    ledger = Ledger(os.path.join(state, "ledger.jsonl"))
    reg = build_default_registry(ledger, ProposalIndex(ledger),
                                 outbox_path=os.path.join(state, "outbox.jsonl"),
                                 brain_dir=None, workspace_dir=ws)
    names = set(reg.names())
    check("capability tools registered with workspace",
          {"web_fetch", "files_list", "files_read", "files_write"} <= names)
    check("classes honest: web_read", reg.get("web_fetch").classes == {"web_read"})
    check("classes honest: file_write",
          reg.get("files_write").classes == {"file_write"})
    gw = MCPGateway(reg, GovernanceGate(ledger),
                    ledger, approvals_path=os.path.join(state, "approvals.jsonl"))
    res = gw.call_tool("files_write", {"path": "n.txt", "content": "governed"})
    check("governed files_write executes",
          not res["isError"] and "wrote" in res["content"][0]["text"])
    res = gw.call_tool("files_read", {"path": "../ledger.jsonl"})
    check("containment violation is a loud tool error",
          res["isError"] and "tool failed" in res["content"][0]["text"])
    ok, n, _ = ledger.verify_chain()
    check("ledger chain intact after capability calls", ok and n >= 4)
    rows = [json.loads(l)["payload"]["tool"] for l in
            open(os.path.join(state, "ledger.jsonl"), encoding="utf-8")
            if '"tool_call"' in l]
    check("capability calls ledgered", "files_write" in rows)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_check_url()
        test_files(tmp)
        test_web()
        test_gateway_integration(tmp)
    print(f"\nall {PASSED} checks passed")
