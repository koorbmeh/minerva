"""Generation tools (round 13): governed generate_image + generate_video
against a scripted Venice API mock, registry/chat/broker wiring, and one
round-trip through the REAL gateway subprocess. Stdlib only:
  python3 tests/test_generation_tools.py
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core.engine import (BrokerPolicy, EngineRegistry, RoutingHints)
from minerva_core.gateway import tools_generation as tg
from minerva_core.gateway.tools_builtin import build_default_registry
from minerva_core.ledger import Ledger, ProposalIndex

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
KEY = "fake-gen-key-XYZ"
PNG = b"\x89PNG-fake-image-bytes-for-tests"
MP4 = b"\x00\x00\x00\x18ftypmp42-fake-video-bytes"

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


# ---------------------------------------------------------------------------
class _MockGen(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def _send(self, step: dict) -> None:
        if "raw" in step:
            payload, ctype = step["raw"], step.get("ctype", "video/mp4")
        else:
            payload = json.dumps(step.get("json", {})).encode()
            ctype = "application/json"
        self.send_response(step.get("status", 200))
        for k, v in step.get("headers", {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _step(self, path: str) -> dict:
        q = self.server.script.get(path)                  # type: ignore[attr-defined]
        if q:
            return q.pop(0)
        if path == "/video/retrieve":                     # default: still working
            return {"json": {"status": "PROCESSING",
                             "average_execution_time": 1, "execution_duration": 1}}
        return {"status": 500, "json": {"error": f"script exhausted: {path}"}}

    def do_GET(self) -> None:                             # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        self.server.calls.append(("GET", parsed.path, {}))  # type: ignore[attr-defined]
        if parsed.path == "/models":
            kind = urllib.parse.parse_qs(parsed.query).get("type", [""])[0]
            cat = self.server.catalogs.get(kind, [])      # type: ignore[attr-defined]
            self._send({"json": {"data": [{"id": i} for i in cat]}})
            return
        if parsed.path.startswith("/dl/"):
            self._send(self._step("/dl"))
            return
        self._send({"status": 404, "json": {"error": "nope"}})

    def do_POST(self) -> None:                            # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        path = urllib.parse.urlsplit(self.path).path
        self.server.calls.append(("POST", path, body))    # type: ignore[attr-defined]
        self._send(self._step(path))


def serve(script: dict | None = None, catalogs: dict | None = None):
    srv = HTTPServer(("127.0.0.1", 0), _MockGen)
    srv.script = {k: list(v) for k, v in (script or {}).items()}  # type: ignore[attr-defined]
    srv.catalogs = catalogs or {                                  # type: ignore[attr-defined]
        "image": ["e2ee-img-x", "venice-sd35", "qwen-image"],
        "video": ["topaz-video-upscale", "seedance-2-0-text-to-video",
                  "seedance-2-0-fast-text-to-video"]}
    srv.calls = []                                                # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@contextlib.contextmanager
def genv(base: str, **extra):
    """Test env: key set, real config/venice.env NEVER read, fast timers."""
    vals = {"VENICE_API_KEY": KEY, "VENICE_API_BASE": base,
            "MINERVA_VENICE_ENV": os.path.join(tempfile.gettempdir(), "no-such.env"),
            "MINERVA_VIDEO_POLL_S": "0.01", "MINERVA_GEN_RETRY_S": "0.01",
            "VENICE_IMAGE_MODEL": "", "VENICE_VIDEO_MODEL": ""}
    vals.update(extra)
    saved = {k: os.environ.get(k) for k in vals}
    for k, v in vals.items():
        if v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    tg._MODEL_CACHE.clear()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        tg._MODEL_CACHE.clear()


def img_step(n: int = 1, headers: dict | None = None) -> dict:
    return {"json": {"id": "gen-1", "images":
                     [base64.b64encode(PNG).decode() for _ in range(n)],
                     "timing": {}}, "headers": headers or {}}


# ---------------------------------------------------------------------------
def test_image(ws: str) -> None:
    srv, base = serve({"/image/generate": [img_step()]})
    with genv(base):
        out = tg.generate_image(ws, {"prompt": "a golden owl"})
        check("image: happy path reports saved path",
              out.startswith("image saved: generated/img-") and ".webp" in out)
        rel = out.split("image saved: ", 1)[1].split(" ", 1)[0]
        p = os.path.join(ws, rel.replace("/", os.sep))
        check("image: file on disk with exact bytes",
              os.path.isfile(p) and open(p, "rb").read() == PNG)
        gen = [c for c in srv.calls if c[1] == "/image/generate"]
        body = gen[0][2]
        check("image: auto-pick chose venice-sd35 (skipping e2ee-*)",
              body["model"] == "venice-sd35")
        check("image: safe_mode defaults FALSE on this lane",
              body["safe_mode"] is False)
        check("image: hide_watermark defaults TRUE",
              body["hide_watermark"] is True)
        check("image: /models consulted once for the auto-pick",
              len([c for c in srv.calls if c[1] == "/models"]) == 1)
        srv.script["/image/generate"] = [img_step()]
        tg.generate_image(ws, {"prompt": "again"})
        check("image: model pick cached per process (no second /models)",
              len([c for c in srv.calls if c[1] == "/models"]) == 1)
        check("image: key never leaks into the result", KEY not in out)
    srv.shutdown(); srv.server_close()

    srv, base = serve({"/image/generate": [img_step(), img_step()]})
    with genv(base):
        out = tg.generate_image(ws, {"prompt": "p", "model": "qwen-image",
                                     "width": 2000, "height": 512,
                                     "format": "png", "seed": 7})
        body = srv.calls[-1][2]
        check("image: explicit model wins, no catalog call",
              body["model"] == "qwen-image"
              and not [c for c in srv.calls if c[1] == "/models"])
        check("image: width clamped to 1280, height passed",
              body["width"] == 1280 and body["height"] == 512)
        check("image: format png honored end-to-end",
              body["format"] == "png" and ".png" in out)
        check("image: seed rides the body and the result text",
              body["seed"] == 7 and "seed 7" in out)
        out = tg.generate_image(ws, {"prompt": "p", "model": "m",
                                     "aspect_ratio": "16:9", "resolution": "2K",
                                     "quality": "medium"})
        body = srv.calls[-1][2]
        check("image: aspect_ratio/resolution/quality passthrough",
              body.get("aspect_ratio") == "16:9" and body.get("resolution") == "2K"
              and body.get("quality") == "medium")
    srv.shutdown(); srv.server_close()


def test_image_edges(ws: str) -> None:
    srv, base = serve()
    with genv(base, VENICE_IMAGE_MODEL="pinned-img"):
        srv.script["/image/generate"] = [img_step()]
        tg.generate_image(ws, {"prompt": "p"})
        check("image: VENICE_IMAGE_MODEL pin wins over auto-pick",
              srv.calls[-1][2]["model"] == "pinned-img")
    with genv(base):
        check("image: empty prompt refused",
              tg.generate_image(ws, {}) == "no prompt given")
        srv.script["/image/generate"] = [
            {"status": 400, "json": {"error": "bad params",
                                     "details": {"width": {"_errors": ["too big"]}}}}]
        out = tg.generate_image(ws, {"prompt": "p", "model": "m"})
        check("image: HTTP 400 surfaced with details",
              "HTTP 400" in out and "bad params" in out and "too big" in out)
        srv.script["/image/generate"] = [
            {"status": 429, "json": {"error": "slow down"},
             "headers": {"retry-after": "0"}}, img_step()]
        out = tg.generate_image(ws, {"prompt": "p", "model": "m"})
        check("image: 429 retried once then succeeds",
              out.startswith("image saved:"))
        srv.script["/image/generate"] = [
            img_step(headers={"x-venice-is-blurred": "true"})]
        out = tg.generate_image(ws, {"prompt": "p", "model": "m",
                                     "safe_mode": True})
        check("image: blur header surfaced plainly",
              srv.calls[-1][2]["safe_mode"] is True and "BLURRED" in out)
        srv.script["/image/generate"] = [img_step(n=2)]
        out = tg.generate_image(ws, {"prompt": "p", "model": "m", "variants": 2})
        check("image: 2 variants -> 2 files (-v1/-v2)",
              "-v1.webp" in out and "-v2.webp" in out)
        srv.script["/image/generate"] = [
            {"json": {"images": ["!!!not-base64???"]}}]
        out = tg.generate_image(ws, {"prompt": "p", "model": "m"})
        check("image: undecodable data is an honest error",
              "undecodable" in out)
        srv.script["/image/generate"] = [img_step()]
        os.environ["MINERVA_IMG_BYTE_CAP"] = "10"
        out = tg.generate_image(ws, {"prompt": "p", "model": "m"})
        os.environ.pop("MINERVA_IMG_BYTE_CAP", None)
        check("image: byte cap refusal (env knob)", out.startswith("refused:"))
        rel1 = tg._save(ws, "img-clash", "webp", b"a")
        rel2 = tg._save(ws, "img-clash", "webp", b"b")
        check("image: no-clobber suffixing on name collision",
              rel1.endswith("img-clash.webp") and rel2.endswith("img-clash-2.webp"))
    with genv(base, VENICE_API_KEY=""):
        out = tg.generate_image(ws, {"prompt": "p"})
        check("image: no key -> plain refusal naming venice.env",
              out.startswith("refused: no Venice key"))
    srv.shutdown(); srv.server_close()


def test_video(ws: str) -> None:
    srv, base = serve()
    dl = base + "/dl/ok"
    srv.script.update({
        "/video/quote": [{"json": {"quote": 0.42}}],
        "/video/queue": [{"json": {"model": "seedance-2-0-fast-text-to-video",
                                   "queue_id": "q-123"}}],
        "/video/retrieve": [
            {"json": {"status": "PROCESSING", "average_execution_time": 1,
                      "execution_duration": 1}},
            {"json": {"status": "COMPLETED", "average_execution_time": 1,
                      "execution_duration": 2, "download_url": dl}}],
        "/dl": [{"raw": MP4, "ctype": "video/mp4"}]})
    with genv(base):
        out = tg.generate_video(ws, {"prompt": "owl takes flight"})
        check("video: happy path (download_url flavor) saves mp4",
              out.startswith("video saved: generated/vid-") and ".mp4" in out)
        rel = out.split("video saved: ", 1)[1].split(" ", 1)[0]
        check("video: file on disk with exact bytes",
              open(os.path.join(ws, rel.replace("/", os.sep)), "rb").read() == MP4)
        check("video: quote rides the result text", "$0.42" in out)
        paths = [c[1] for c in srv.calls if c[0] == "POST"]
        check("video: quote called BEFORE queue",
              paths.index("/video/quote") < paths.index("/video/queue"))
        qbody = [c[2] for c in srv.calls if c[1] == "/video/queue"][0]
        check("video: duration defaults '5s', audio OMITTED unless given",
              qbody["duration"] == "5s" and "audio" not in qbody)
        check("video: auto-pick prefers the fast text-to-video model",
              qbody["model"] == "seedance-2-0-fast-text-to-video")
        with open(os.path.join(ws, "generated", "video-jobs.jsonl"),
                  encoding="utf-8") as jf:
            jobs = [json.loads(x) for x in jf if x.strip()]
        check("video: queue_id persisted to disk BEFORE polling",
              any(j.get("queue_id") == "q-123"
                  and j.get("model") == "seedance-2-0-fast-text-to-video"
                  for j in jobs))
    srv.shutdown(); srv.server_close()

    srv, base = serve({
        "/video/quote": [{"json": {"quote": 0.10}}],
        "/video/queue": [{"json": {"queue_id": "q-bin"}}],
        "/video/retrieve": [{"raw": MP4, "ctype": "video/mp4"}]})
    with genv(base):
        out = tg.generate_video(ws, {"prompt": "p", "model": "m",
                                     "duration": "3s", "resolution": "720p",
                                     "audio": False})
        check("video: binary-retrieve flavor saves mp4",
              out.startswith("video saved:"))
        qbody = [c[2] for c in srv.calls if c[1] == "/video/queue"][0]
        check("video: duration/resolution passthrough",
              qbody["duration"] == "3s" and qbody["resolution"] == "720p")
        check("video: explicit audio=false rides the body",
              qbody["audio"] is False)
    srv.shutdown(); srv.server_close()


def test_video_guards(ws: str) -> None:
    srv, base = serve({"/video/quote": [{"json": {"quote": 0.75}}]})
    with genv(base, MINERVA_VIDEO_QUOTE_CAP="0.50"):
        out = tg.generate_video(ws, {"prompt": "p", "model": "m"})
        check("video: quote over cap -> refusal naming both numbers",
              out.startswith("refused:") and "$0.75" in out and "$0.50" in out)
        check("video: refused job never queued",
              not [c for c in srv.calls if c[1] == "/video/queue"])
    srv.shutdown(); srv.server_close()

    srv, base = serve({"/video/quote": [{"status": 500, "json": {"error": "no quote"}},
                                        {"status": 500, "json": {"error": "no quote"}}]})
    with genv(base):
        out = tg.generate_video(ws, {"prompt": "p", "model": "m"})
        check("video: quote failure -> refuses to spend blind",
              out.startswith("refused:") and "quote" in out)
    srv.shutdown(); srv.server_close()

    srv, base = serve({
        "/video/quote": [{"status": 500, "json": {"error": "x"}},
                         {"status": 500, "json": {"error": "x"}}],
        "/video/queue": [{"json": {"queue_id": "q-s"}}],
        "/video/retrieve": [{"raw": MP4, "ctype": "video/mp4"}]})
    with genv(base, MINERVA_VIDEO_SKIP_QUOTE="1"):
        out = tg.generate_video(ws, {"prompt": "p", "model": "m"})
        check("video: MINERVA_VIDEO_SKIP_QUOTE=1 overrides the quote gate",
              out.startswith("video saved:"))
    srv.shutdown(); srv.server_close()

    srv, base = serve({"/video/quote": [{"json": {"quote": 0.01}}],
                       "/video/queue": [{"json": {"queue_id": "q-slow"}}]})
    with genv(base, MINERVA_VIDEO_TIMEOUT_S="0.05"):
        out = tg.generate_video(ws, {"prompt": "p", "model": "m-slow"})
        check("video: timeout names queue_id + model + resume path",
              "q-slow" in out and "m-slow" in out and "resume" in out)
    srv.shutdown(); srv.server_close()

    srv, base = serve({"/video/retrieve": [{"raw": MP4, "ctype": "video/mp4"}]})
    with genv(base):
        out = tg.generate_video(ws, {"queue_id": "q-res", "model": "m"})
        check("video: resume(queue_id+model) skips quote AND queue",
              out.startswith("video saved:")
              and not [c for c in srv.calls if c[1] in ("/video/queue",
                                                        "/video/quote")])
        check("video: resume without model refused",
              "BOTH" in tg.generate_video(ws, {"queue_id": "q-res"}))
        check("video: empty prompt refused",
              tg.generate_video(ws, {}) == "no prompt given")
    srv.shutdown(); srv.server_close()

    srv, base = serve({"/video/quote": [{"json": {"quote": 0.10}}],
                       "/video/queue": [{"status": 422, "json": {
                           "error": "content policy",
                           "suggested_prompt": "a calmer owl"}}]})
    with genv(base):
        out = tg.generate_video(ws, {"prompt": "p", "model": "m"})
        check("video: 422 surfaces policy + suggested_prompt",
              "HTTP 422" in out and "a calmer owl" in out)
    srv.shutdown(); srv.server_close()

    srv, base = serve({"/video/quote": [{"json": {"quote": 0.10}}],
                       "/video/queue": [{"json": {"queue_id": "q-i2v"}}],
                       "/video/retrieve": [{"raw": MP4, "ctype": "video/mp4"}]})
    with genv(base):
        os.makedirs(os.path.join(ws, "generated"), exist_ok=True)
        with open(os.path.join(ws, "generated", "seed.png"), "wb") as f:
            f.write(PNG)
        out = tg.generate_video(ws, {"prompt": "animate it", "model": "m",
                                     "image": "generated/seed.png"})
        qbody = [c[2] for c in srv.calls if c[1] == "/video/queue"][0]
        check("video: workspace image rides as data URL (image-to-video)",
              out.startswith("video saved:")
              and qbody["image_url"].startswith("data:image/png;base64,"))
        check("video: image path escaping the workspace refused",
              tg.generate_video(ws, {"prompt": "p", "model": "m",
                                     "image": "../outside.png"})
              .startswith("refused:"))
    srv.shutdown(); srv.server_close()


def test_video_selfheal(ws: str) -> None:
    wan_400 = {"status": 400, "json": {"error": "", "details": {
        "_errors": [],
        "aspect_ratio": {"_errors": ["Required"]},
        "audio": {"_errors": ["This model does not support audio configuration"]}}}}
    srv, base = serve({
        "/video/quote": [{"json": {"quote": 0.10}}, {"json": {"quote": 0.12}}],
        "/video/queue": [wan_400, {"json": {"queue_id": "q-heal"}}],
        "/video/retrieve": [{"raw": MP4, "ctype": "video/mp4"}]})
    with genv(base):
        out = tg.generate_video(ws, {"prompt": "p", "model": "wan-x",
                                     "audio": True})
        check("video: self-heal retry saves after a param 400",
              out.startswith("video saved:") and "auto-adjusted" in out)
        queues = [c[2] for c in srv.calls if c[1] == "/video/queue"]
        check("video: exactly two queue calls (one heal cycle)",
              len(queues) == 2)
        check("video: retry added required aspect_ratio + dropped audio",
              queues[1].get("aspect_ratio") == "16:9"
              and "audio" not in queues[1] and queues[0].get("audio") is True)
        check("video: re-quoted price rides the result",
              "$0.12" in out and "$0.10" not in out)
    srv.shutdown(); srv.server_close()

    srv, base = serve({
        "/video/quote": [{"json": {"quote": 0.10}}, {"json": {"quote": 0.80}}],
        "/video/queue": [{"status": 400, "json": {"error": "", "details": {
            "resolution": {"_errors": ["Required"]}}}}]})
    with genv(base, MINERVA_VIDEO_QUOTE_CAP="0.50"):
        out = tg.generate_video(ws, {"prompt": "p", "model": "m"})
        check("video: adjusted re-quote over cap -> refused naming $0.80",
              out.startswith("refused:") and "$0.80" in out)
        check("video: no second queue after the cap refusal",
              len([c for c in srv.calls if c[1] == "/video/queue"]) == 1)
    srv.shutdown(); srv.server_close()

    srv, base = serve({
        "/video/quote": [{"json": {"quote": 0.10}}],
        "/video/queue": [{"status": 400, "json": {"error": "needs an image", "details": {
            "image_url": {"_errors": ["Required"]}}}}]})
    with genv(base):
        out = tg.generate_video(ws, {"prompt": "p", "model": "m"})
        check("video: unfixable 400 surfaced raw with the field name",
              "HTTP 400" in out and "image_url" in out)
        check("video: no blind retry without a usable adjustment",
              len([c for c in srv.calls if c[1] == "/video/queue"]) == 1)
    srv.shutdown(); srv.server_close()


def test_client_timeouts() -> None:
    from minerva_core.adapters.anthropic_byok import (TOOL_TIMEOUT_DEFAULT,
                                                      tool_call_timeout)
    saved = os.environ.get("MINERVA_VIDEO_TIMEOUT_S")
    os.environ.pop("MINERVA_VIDEO_TIMEOUT_S", None)
    try:
        check("client: video allowance outlasts the tool deadline (+60s)",
              tool_call_timeout("generate_video") == 660.0
              and tool_call_timeout("generate_image") == 240.0
              and tool_call_timeout("ping") == TOOL_TIMEOUT_DEFAULT)
        os.environ["MINERVA_VIDEO_TIMEOUT_S"] = "30"
        check("client: video allowance tracks MINERVA_VIDEO_TIMEOUT_S",
              tool_call_timeout("generate_video") == 90.0)
    finally:
        if saved is None:
            os.environ.pop("MINERVA_VIDEO_TIMEOUT_S", None)
        else:
            os.environ["MINERVA_VIDEO_TIMEOUT_S"] = saved


def test_wiring(tmp: str) -> None:
    state = os.path.join(tmp, "wiring")
    ws = os.path.join(state, "workspace")
    os.makedirs(ws, exist_ok=True)
    ledger = Ledger(os.path.join(state, "ledger.jsonl"))
    reg = build_default_registry(ledger, ProposalIndex(ledger),
                                 outbox_path=os.path.join(state, "outbox.jsonl"),
                                 brain_dir=None, workspace_dir=ws)
    check("registry: generation tools present with a workspace",
          {"generate_image", "generate_video"} <= set(reg.names()))
    check("registry: classes honest (media_gen)",
          reg.get("generate_image").classes == {"media_gen"}
          and reg.get("generate_video").classes == {"media_gen"})
    reg2 = build_default_registry(ledger, ProposalIndex(ledger),
                                  outbox_path=os.path.join(state, "outbox.jsonl"),
                                  brain_dir=None)
    check("registry: no workspace -> no generation tools",
          not {"generate_image", "generate_video"} & set(reg2.names()))
    chat_src = open(os.path.join(ROOT_DIR, "scripts", "minerva_chat.py"),
                    encoding="utf-8").read()
    head = chat_src.split("BUSY_REPLY", 1)[0]
    check("chat: CHAT_TOOLS includes the generation tools",
          '"generate_image"' in head and '"generate_video"' in head)

    class _Fake:
        name = "fake-frontier"
        def capabilities(self):
            return {"frontier", "tools_mcp"}
        def create_session(self, cfg):
            raise NotImplementedError
    regE = EngineRegistry(BrokerPolicy())
    regE.register(_Fake(), default=True)
    a = regE.pick(RoutingHints())
    check("routing: default modality text -> default engine",
          a.name == "fake-frontier")
    try:
        regE.pick(RoutingHints(modality="image"))
        loud = False
    except RuntimeError as e:
        loud = "image_gen" in str(e) and "venice" in str(e).lower()
    check("routing: modality image with no provider -> loud refusal", loud)
    from minerva_core.adapters.venice import VeniceAdapter
    venice = VeniceAdapter(api_key=KEY, gateway_argv=["x"])
    regE.register(venice)
    check("routing: modality video routes to the Venice lane",
          regE.pick(RoutingHints(modality="video")) is venice)


def test_real_gateway(tmp: str) -> None:
    from minerva_core.adapters.anthropic_byok import GatewayClient
    srv, base = serve({"/image/generate": [img_step()]})
    state = os.path.join(tmp, "gwstate")
    os.makedirs(state, exist_ok=True)
    gw = GatewayClient(
        [sys.executable, os.path.join(ROOT_DIR, "scripts", "run_gateway.py"),
         "--state-dir", state],
        extra_env={"VENICE_API_KEY": KEY, "VENICE_API_BASE": base,
                   "MINERVA_VENICE_ENV": os.path.join(tmp, "no-such.env"),
                   "MINERVA_TOOLS": "generate_image"})
    try:
        gw.initialize()
        names = [t["name"] for t in gw.list_tools()]
        check("gateway: allowlist exposes exactly [generate_image]",
              names == ["generate_image"])
        text, err = gw.call("generate_image",
                            {"prompt": "owl", "model": "venice-sd35"})
        check("gateway: governed generation round-trip saves the file",
              not err and text.startswith("image saved:"))
        rel = text.split("image saved: ", 1)[1].split(" ", 1)[0]
        check("gateway: output landed inside the contained workspace",
              os.path.isfile(os.path.join(state, "workspace",
                                          rel.replace("/", os.sep))))
    finally:
        gw.close()
        srv.shutdown(); srv.server_close()
    ok, n, _ = Ledger(os.path.join(state, "ledger.jsonl")).verify_chain()
    check("gateway: call ledgered, hash chain intact", ok and n >= 1)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        test_image(ws)
        test_image_edges(ws)
        test_video(ws)
        test_video_guards(ws)
        test_video_selfheal(ws)
        test_client_timeouts()
        test_wiring(tmp)
        test_real_gateway(tmp)
    print(f"\nall {PASSED} checks passed")
