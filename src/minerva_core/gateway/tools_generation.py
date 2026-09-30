"""Governed GENERATION tools (round 13) - image + video THROUGH the gate.

The operator's round-12 steer: "arrange for her to use image and video models" -
generation lanes alongside the LLM lanes, with the broker idea intact. Venice
exposes image generation (POST /image/generate) and an async video queue
(POST /video/queue -> poll /video/retrieve), both billed in real money - so
these are gateway tools: every call passes GovernanceGate and lands in the
hash-chained ledger, and the outputs land ONLY in Minerva's contained
workspace (same realpath containment as files_*; the engine gets a small text
result with the file path, never raw bytes/base64).

Cost honesty:
  * generate_image: cheap (cents); est_cost_usd rides the ToolSpec for audit.
  * generate_video: REAL dollars per call. The tool calls /video/quote FIRST
    and refuses when the quote exceeds MINERVA_VIDEO_QUOTE_CAP (default $2.50)
    - an accurate, per-call cost gate (the GovernanceGate spend-ask works from
    a static estimate, so the live quote is the honest guard). If the quote
    endpoint itself fails the tool refuses too (no blind spending) unless
    MINERVA_VIDEO_SKIP_QUOTE=1.

Safe-mode stance: safe_mode defaults FALSE here (Venice's API default is
true = blur adult content). This lane exists because the operator routes
low-restriction jobs to Venice on purpose (governance/content policy is
Minerva's layer, his call); Venice's own terms still apply upstream
(x-venice-is-content-violation refusals are surfaced plainly).

Key handling (standing rule): VENICE_API_KEY env, else config/venice.env -
filled HOST-SIDE by the operator, never pasted into a Claude session, never written
to logs or results. Model pins: VENICE_IMAGE_MODEL / VENICE_VIDEO_MODEL (env
or venice.env); otherwise the tool auto-picks from /models?type=... once per
process (skips e2ee-*; see state/venice-gen-models.txt from the probe).

Stdlib only. ASCII-safe returns (cp1252 rule).
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .tools_capability import _resolve

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

DEFAULT_API_BASE = "https://api.venice.ai/api/v1"
GEN_SUBDIR = "generated"
IMG_BYTE_CAP = 30_000_000          # decoded image bytes (per image)
VID_BYTE_CAP = 200_000_000         # video bytes
IMG_TIMEOUT_S = 180.0
VIDEO_QUOTE_CAP_USD = 2.50         # MINERVA_VIDEO_QUOTE_CAP overrides
VIDEO_POLL_S = 5.0                 # MINERVA_VIDEO_POLL_S overrides
VIDEO_TIMEOUT_S = 600.0            # MINERVA_VIDEO_TIMEOUT_S overrides
IMG_FORMATS = ("webp", "png", "jpeg")
DATAURL_INPUT_CAP = 10_000_000     # workspace image fed to image-to-video

_MODEL_CACHE: dict[tuple[str, str], str] = {}


def _ascii(s: str) -> str:
    return str(s).encode("ascii", "backslashreplace").decode("ascii")


def _venice_env_path() -> str:
    return (os.environ.get("MINERVA_VENICE_ENV", "").strip()
            or os.path.join(ROOT, "config", "venice.env"))


def _cfg(name: str) -> str:
    """Env var first, else KEY=VALUE line in config/venice.env. ''=missing.
    The file is read host-side at call time; values never enter results."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    path = _venice_env_path()
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name and v.strip():
                    return v.strip()
    except OSError:
        return ""
    return ""


def api_base() -> str:
    return (os.environ.get("VENICE_API_BASE", "").strip()
            or DEFAULT_API_BASE).rstrip("/")


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _err_text(status: int, raw: bytes) -> str:
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    msg = ""
    if isinstance(body, dict):
        e = body.get("error")
        msg = e if isinstance(e, str) else json.dumps(e) if e else ""
        det = body.get("details")
        if det:
            msg += " | details: " + json.dumps(det)[:200]
        sp = body.get("suggested_prompt")
        if sp:
            msg += f" | Venice suggests this prompt instead: {str(sp)[:200]}"
    hint = {401: " (check the Venice key in config/venice.env)",
            402: " (Venice balance empty - top up at venice.ai)",
            403: " (model unavailable in your region)",
            422: " (content policy of Venice/the model provider)",
            429: " (rate limited)",
            503: " (model at capacity - try again later)"}.get(status, "")
    return _ascii(f"Venice API error HTTP {status}{hint}: {msg[:400]}".strip())


def _request(method: str, url: str, key: str, body: dict | None = None,
             timeout: float = IMG_TIMEOUT_S,
             byte_cap: int = VID_BYTE_CAP) -> tuple[int, bytes, dict]:
    """(status, raw_bytes, headers-ish dict). One polite retry on 429/5xx.
    The key rides only the Authorization header - never logs, never results."""
    data = (json.dumps(body, ensure_ascii=True).encode("ascii")
            if body is not None else None)
    last: tuple[int, bytes, dict] = (0, b"", {})
    for attempt in (1, 2):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                blob = resp.read(byte_cap + 1)
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return getattr(resp, "status", 200), blob, hdrs
        except urllib.error.HTTPError as e:
            raw = e.read()
            last = (e.code, raw, {k.lower(): v for k, v in e.headers.items()})
            if e.code in (429, 500, 502, 503, 529) and attempt == 1:
                time.sleep(min(_f("MINERVA_GEN_RETRY_S", 2.0), 15.0))
                continue
            return last
        except (urllib.error.URLError, OSError) as e:
            last = (0, _ascii(f"network error: {e}").encode("ascii"), {})
            if attempt == 1:
                time.sleep(min(_f("MINERVA_GEN_RETRY_S", 2.0), 15.0))
                continue
            return last
    return last


def _list_models(kind: str, key: str) -> list[str]:
    status, raw, _h = _request(
        "GET", api_base() + "/models?type=" + urllib.parse.quote(kind), key,
        timeout=30.0, byte_cap=4_000_000)
    if status != 200:
        raise RuntimeError(_err_text(status, raw))
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise RuntimeError("Venice /models returned non-JSON")
    return [str(m.get("id", "")) for m in data.get("data", []) if m.get("id")]


def _resolve_model(kind: str, arg_model, key: str) -> str:
    """Explicit arg > VENICE_IMAGE_MODEL/VENICE_VIDEO_MODEL pin > auto-pick
    from the live catalog (cached per process; e2ee-* and upscale-* skipped)."""
    m = str(arg_model or "").strip()
    if m:
        return m
    pin = _cfg("VENICE_IMAGE_MODEL" if kind == "image" else "VENICE_VIDEO_MODEL")
    if pin:
        return pin
    cached = _MODEL_CACHE.get((api_base(), kind))
    if cached:
        return cached
    ids = [i for i in _list_models(kind, key) if not i.startswith("e2ee-")]
    pick = ""
    if kind == "image":
        if "venice-sd35" in ids:
            pick = "venice-sd35"
        else:
            pick = next((i for i in ids if "uncensored" in i), ids[0] if ids else "")
    else:
        usable = [i for i in ids if "upscale" not in i]
        pick = (next((i for i in usable if "fast" in i and "text-to-video" in i), "")
                or next((i for i in usable if "text-to-video" in i), "")
                or (usable[0] if usable else ""))
    if not pick:
        raise RuntimeError(f"no usable {kind} model in your Venice catalog "
                           "(see the generation probe / venice-gen-models.txt)")
    _MODEL_CACHE[(api_base(), kind)] = pick
    return pick


def _save(workspace: str, stem: str, ext: str, blob: bytes) -> str:
    rel = f"{GEN_SUBDIR}/{stem}.{ext}"
    path = _resolve(workspace, rel)
    n = 1
    while os.path.exists(path):
        n += 1
        rel = f"{GEN_SUBDIR}/{stem}-{n}.{ext}"
        path = _resolve(workspace, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(blob)
    return rel


# ---------------------------------------------------------------------------
# generate_image
# ---------------------------------------------------------------------------
def generate_image(workspace: str, args: dict, spend_meter=None) -> str:
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return "no prompt given"
    key = _cfg("VENICE_API_KEY")
    if not key:
        return ("refused: no Venice key - fill config/venice.env host-side "
                "(VENICE_API_KEY=...) and retry")
    try:
        model = _resolve_model("image", args.get("model"), key)
    except RuntimeError as e:
        return _ascii(str(e))[:400]

    fmt = str(args.get("format", "webp")).strip().lower()
    if fmt not in IMG_FORMATS:
        fmt = "webp"
    body: dict = {"model": model, "prompt": prompt[:7500], "format": fmt,
                  "safe_mode": bool(args.get("safe_mode", False)),
                  "hide_watermark": bool(args.get("hide_watermark", True)),
                  "return_binary": False}
    for k in ("negative_prompt", "style_preset", "aspect_ratio",
              "resolution", "quality"):
        v = args.get(k)
        if v:
            body[k] = str(v)[:7500]
    for k, cap in (("width", 1280), ("height", 1280)):
        v = args.get(k)
        if v is not None:
            try:
                body[k] = max(1, min(int(v), cap))
            except (TypeError, ValueError):
                pass
    for k in ("seed", "cfg_scale", "variants"):
        v = args.get(k)
        if v is not None:
            try:
                body[k] = (float(v) if k == "cfg_scale"
                           else max(1, min(int(v), 4)) if k == "variants"
                           else int(v))
            except (TypeError, ValueError):
                pass

    img_cap = _i("MINERVA_IMG_BYTE_CAP", IMG_BYTE_CAP)
    status, raw, hdrs = _request("POST", api_base() + "/image/generate", key,
                                 body, byte_cap=max(img_cap * 4, 1_000_000))
    if status != 200:
        return _err_text(status, raw)
    try:
        resp = json.loads(raw.decode("utf-8", "replace"))
        images = resp.get("images") or []
    except json.JSONDecodeError:
        return "Venice image API returned non-JSON (truncated?) - not saved"
    if not images:
        return "Venice returned no image data"

    stamp = time.strftime("%Y%m%d-%H%M%S")
    saved: list[str] = []
    for i, b64 in enumerate(images):
        try:
            blob = base64.b64decode(str(b64), validate=False)
        except (ValueError, TypeError):
            return "Venice returned undecodable image data - not saved"
        if not blob or len(blob) > img_cap:
            return (f"refused: image #{i + 1} is {len(blob)} bytes "
                    f"(cap {img_cap}) - not saved")
        stem = f"img-{stamp}" if len(images) == 1 else f"img-{stamp}-v{i + 1}"
        saved.append(f"{_save(workspace, stem, fmt, blob)} ({len(blob)} bytes)")

    notes = []
    if str(hdrs.get("x-venice-is-blurred", "")).lower() == "true":
        notes.append("Venice BLURRED this image (safe_mode) - pass "
                     "safe_mode=false if that was not wanted")
    if str(hdrs.get("x-venice-is-content-violation", "")).lower() == "true":
        notes.append("Venice flagged this as a content violation of its terms")
    if spend_meter is not None:                  # est: Venice bills credits
        spend_meter.add(0.04 * len(saved), f"image x{len(saved)} ({model})",
                        kind="est")
    out = "image saved: " + "; ".join(saved) + f" | model {model}"
    if "seed" in body:
        out += f" | seed {body['seed']}"
    if notes:
        out += " | " + " | ".join(notes)
    return _ascii(out + " | (files live in your workspace - files_list shows them)")


# ---------------------------------------------------------------------------
# generate_video
# ---------------------------------------------------------------------------
_FIELD_DEFAULTS = {"aspect_ratio": "16:9", "resolution": "720p",
                   "duration": "5s", "audio": True}


def _adjust_from_details(body: dict, raw: bytes) -> list[str]:
    """Self-heal ONE 400: Venice's DetailedError names fields per model -
    add a sensible default for "Required" fields, drop fields the model
    "does not support" (live case: wan-2-7 requires aspect_ratio and rejects
    the audio key entirely). Mutates body; returns plain notes ([] = nothing
    usable, let the original error surface)."""
    try:
        det = json.loads(raw.decode("utf-8", "replace")).get("details") or {}
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        return []
    if not isinstance(det, dict):
        return []
    notes: list[str] = []
    for field, info in det.items():
        if field == "_errors" or not isinstance(info, dict):
            continue
        errs = " ".join(str(e) for e in info.get("_errors", []))
        if "Required" in errs and field in _FIELD_DEFAULTS and field not in body:
            body[field] = _FIELD_DEFAULTS[field]
            notes.append(f"added {field}={_FIELD_DEFAULTS[field]} (model requires it)")
        elif "does not support" in errs and field in body:
            del body[field]
            notes.append(f"dropped {field} (model does not support it)")
    return notes


def _video_bytes_from(url: str, key: str, cap: int) -> tuple[bytes | None, str]:
    if not str(url).lower().startswith(("http://", "https://")):
        return None, "refused: Venice returned a non-http download_url"
    status, raw, hdrs = _request("GET", url, key, timeout=300.0, byte_cap=cap + 1)
    if status != 200:
        return None, _err_text(status, raw)
    if len(raw) > cap:
        return None, f"refused: video exceeds the {cap}-byte cap - not saved"
    return raw, ""


def generate_video(workspace: str, args: dict, spend_meter=None) -> str:
    key = _cfg("VENICE_API_KEY")
    if not key:
        return ("refused: no Venice key - fill config/venice.env host-side "
                "(VENICE_API_KEY=...) and retry")
    vid_cap = _i("MINERVA_VID_BYTE_CAP", VID_BYTE_CAP)
    poll_s = _f("MINERVA_VIDEO_POLL_S", VIDEO_POLL_S)
    deadline_s = _f("MINERVA_VIDEO_TIMEOUT_S", VIDEO_TIMEOUT_S)
    base = api_base()

    queue_id = str(args.get("queue_id", "")).strip()
    model = str(args.get("model", "")).strip()
    download_url = ""
    quote_note = ""
    adjusted: list[str] = []

    if queue_id:                                    # resume a queued job
        if not model:
            return "to resume a video, pass BOTH queue_id and model"
    else:
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return "no prompt given"
        try:
            model = _resolve_model("video", args.get("model"), key)
        except RuntimeError as e:
            return _ascii(str(e))[:400]
        duration = str(args.get("duration", "5s")).strip() or "5s"
        body: dict = {"model": model, "prompt": prompt[:10000],
                      "duration": duration}
        if "audio" in args:                    # opt-in only: some models 400
            body["audio"] = bool(args.get("audio"))   # on the key itself
        for k in ("aspect_ratio", "resolution", "negative_prompt"):
            v = args.get(k)
            if v:
                body[k] = str(v)[:10000]
        img_rel = str(args.get("image", "")).strip()
        if img_rel:                                # image-to-video from workspace
            try:
                p = _resolve(workspace, img_rel)
            except ValueError as e:
                return _ascii(f"refused: {e}")
            if not os.path.isfile(p):
                return f"no such workspace image: {img_rel}"
            if os.path.getsize(p) > DATAURL_INPUT_CAP:
                return f"refused: {img_rel} is over the {DATAURL_INPUT_CAP}-byte input cap"
            ext = os.path.splitext(p)[1].lower().lstrip(".")
            mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                    "webp": "webp"}.get(ext)
            if not mime:
                return f"refused: {img_rel} is not a png/jpeg/webp image"
            with open(p, "rb") as f:
                body["image_url"] = ("data:image/" + mime + ";base64,"
                                     + base64.b64encode(f.read()).decode("ascii"))

        # ---- quote first: the honest cost gate -----------------------------
        cap_usd = _f("MINERVA_VIDEO_QUOTE_CAP", VIDEO_QUOTE_CAP_USD)

        def _quote_gate(b: dict) -> tuple[bool, str, str]:
            """(ok, refusal_text, quote_note). Re-run after any adjustment:
            aspect/resolution/duration/audio are all pricing inputs."""
            qbody = {k: b[k] for k in ("model", "duration", "aspect_ratio",
                                       "resolution", "audio") if k in b}
            qs, qraw, _qh = _request("POST", base + "/video/quote", key, qbody,
                                     timeout=30.0, byte_cap=100_000)
            if qs == 200:
                try:
                    quote = float(json.loads(
                        qraw.decode("utf-8", "replace"))["quote"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    quote = -1.0
                if quote >= 0.0:
                    if quote > cap_usd:
                        return False, (
                            f"refused: this video would cost ${quote:.2f} "
                            f"(cap ${cap_usd:.2f}). Use a cheaper model / "
                            "shorter duration / lower resolution, or raise "
                            "MINERVA_VIDEO_QUOTE_CAP."), ""
                    return True, "", f" | quoted ${quote:.2f}"
                qs = 0                          # unparseable -> treat as failed
            if os.environ.get("MINERVA_VIDEO_SKIP_QUOTE") == "1":
                return True, "", ""
            return False, (
                "refused: could not get a price quote for this video, so "
                "not spending blind (" + _err_text(qs, qraw)[:200]
                + "). Set MINERVA_VIDEO_SKIP_QUOTE=1 to override."), ""

        ok, refusal, quote_note = _quote_gate(body)
        if not ok:
            return refusal
        status, raw, _h = _request("POST", base + "/video/queue", key, body,
                                   timeout=120.0, byte_cap=1_000_000)
        if status == 400:                      # one self-heal cycle, then honest
            adjusted = _adjust_from_details(body, raw)
            if adjusted:
                ok, refusal, quote_note = _quote_gate(body)   # price may shift
                if not ok:
                    return refusal
                status, raw, _h = _request("POST", base + "/video/queue", key,
                                           body, timeout=120.0,
                                           byte_cap=1_000_000)
        if status != 200:
            return _err_text(status, raw)
        try:
            q = json.loads(raw.decode("utf-8", "replace"))
            queue_id = str(q.get("queue_id", ""))
            download_url = str(q.get("download_url", "") or "")
        except json.JSONDecodeError:
            return "Venice video queue returned non-JSON"
        if not queue_id:
            return "Venice video queue returned no queue_id"
        # Persist the job BEFORE polling: if anything dies mid-poll (client
        # timeout, crash, power), the id survives on disk for a resume.
        try:
            jobs = _resolve(workspace, GEN_SUBDIR + "/video-jobs.jsonl")
            os.makedirs(os.path.dirname(jobs), exist_ok=True)
            with open(jobs, "a", encoding="utf-8", newline="\n") as jf:
                jf.write(json.dumps({"ts": time.time(), "model": model,
                                     "queue_id": queue_id,
                                     "prompt_head": prompt[:80]},
                                    ensure_ascii=True) + "\n")
        except (OSError, ValueError):
            pass                       # never block the job on bookkeeping

    # ---- poll until COMPLETED (or deadline) --------------------------------
    t0 = time.time()
    while True:
        status, raw, hdrs = _request("POST", base + "/video/retrieve", key,
                                     {"model": model, "queue_id": queue_id},
                                     timeout=300.0, byte_cap=vid_cap + 1)
        ctype = str(hdrs.get("content-type", "")).lower()
        if status == 200 and ("video/" in ctype or "octet-stream" in ctype):
            if len(raw) > vid_cap:
                return f"refused: video exceeds the {vid_cap}-byte cap - not saved"
            blob = raw
            break
        if status != 200:
            return _err_text(status, raw)
        try:
            st = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return "Venice video retrieve returned non-JSON"
        download_url = str(st.get("download_url", "") or "") or download_url
        if str(st.get("status", "")) == "COMPLETED":
            if not download_url:
                return ("video reports COMPLETED but no file/stream arrived - "
                        f"queue_id {queue_id} (model {model}); try resuming "
                        "with those, or check venice.ai")
            blob, err = _video_bytes_from(download_url, key, vid_cap)
            if blob is None:
                return err
            break
        if time.time() - t0 > deadline_s:
            return (f"video still processing after {int(deadline_s)}s - it is "
                    f"NOT lost: resume with generate_video(queue_id="
                    f"'{queue_id}', model='{model}') (Venice keeps the job "
                    "about 24h)")
        time.sleep(max(poll_s, 0.01))

    rel = _save(workspace, "vid-" + time.strftime("%Y%m%d-%H%M%S"), "mp4", blob)
    if spend_meter is not None:
        m = re.search(r"\$([0-9.]+)", quote_note)
        if m:                                    # the quote IS the real price
            spend_meter.add(float(m.group(1)), f"video ({model})")
        else:
            spend_meter.add(1.0, f"video ({model})", kind="est")
    adj = (" | auto-adjusted: " + "; ".join(adjusted)) if adjusted else ""
    return _ascii(f"video saved: {rel} ({len(blob)} bytes) | model {model}"
                  + quote_note + adj
                  + " | (files live in your workspace - files_list shows them)")


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def register_generation_tools(reg, workspace_dir: str,
                              spend_meter=None) -> None:
    """Always registered when the gateway has a workspace; the handlers check
    the key at call time (plain refusal when config/venice.env is empty), so
    the default registry stays identical across machines."""
    from .registry import ToolSpec

    reg.register(ToolSpec(
        name="generate_image",
        description=("Generate an image with the Venice lane and save it into "
                     "your private workspace; returns the file path. Optional: "
                     "model (else the operator's pin/auto-pick), width/height or "
                     "aspect_ratio/resolution (model-specific), negative_prompt, "
                     "style_preset, seed, variants 1-4, format webp/png/jpeg. "
                     "Costs cents per image."),
        input_schema={"type": "object",
                      "properties": {
                          "prompt": {"type": "string"},
                          "model": {"type": "string"},
                          "width": {"type": "integer"},
                          "height": {"type": "integer"},
                          "aspect_ratio": {"type": "string"},
                          "resolution": {"type": "string"},
                          "quality": {"type": "string"},
                          "format": {"type": "string"},
                          "negative_prompt": {"type": "string"},
                          "style_preset": {"type": "string"},
                          "seed": {"type": "integer"},
                          "cfg_scale": {"type": "number"},
                          "variants": {"type": "integer"},
                          "safe_mode": {"type": "boolean"},
                          "hide_watermark": {"type": "boolean"}},
                      "required": ["prompt"]},
        classes={"media_gen"},
        est_cost_usd=0.04,
        handler=lambda a: generate_image(workspace_dir, a, spend_meter)))

    reg.register(ToolSpec(
        name="generate_video",
        description=("Generate a short VIDEO with the Venice lane and save it "
                     "into your private workspace; returns the file path. "
                     "COSTS REAL MONEY (a price quote is checked against a cap "
                     "first) and takes minutes. Optional: model, duration like "
                     "'5s', aspect_ratio, resolution, negative_prompt, audio, "
                     "image (a workspace image to animate). To resume a "
                     "timed-out job pass queue_id AND model."),
        input_schema={"type": "object",
                      "properties": {
                          "prompt": {"type": "string"},
                          "model": {"type": "string"},
                          "duration": {"type": "string"},
                          "aspect_ratio": {"type": "string"},
                          "resolution": {"type": "string"},
                          "negative_prompt": {"type": "string"},
                          "audio": {"type": "boolean"},
                          "image": {"type": "string"},
                          "queue_id": {"type": "string"}},
                      "required": []},
        classes={"media_gen"},
        est_cost_usd=1.00,
        handler=lambda a: generate_video(workspace_dir, a, spend_meter)))
