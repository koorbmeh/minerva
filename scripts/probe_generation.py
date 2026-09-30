"""Guided live probe for the GENERATION lanes (round 13).

Run on the Minerva host:
  "Minerva - Test Image Gen.bat"            -> image only (costs cents)
  "Minerva - Test Video Gen.bat" (--video)  -> adds ONE short video AFTER
                                               showing you the price quote
                                               and asking yes/no (real money)

What it does:
  1. Lists the image + video models your key can use
     -> state/venice-gen-models.txt
  2. One governed generate_image call THROUGH the real gateway in its own
     sandbox state dir (state/genprobe/): proves gate + ledger + containment.
     The picture lands in state/genprobe/workspace/generated/ - open it!
  3. (--video only) quote -> your yes -> one governed generate_video.
Report: state/genprobe-report.json. The key stays host-side (config/venice.env),
never printed, never written to the report. Plain ASCII output. Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from minerva_core.adapters.anthropic_byok import GatewayClient   # noqa: E402
from minerva_core.gateway import tools_generation as tg          # noqa: E402
from minerva_core.ledger import Ledger                           # noqa: E402

STATE = os.path.join(ROOT, "state")
PROBE_STATE = os.path.join(STATE, "genprobe")
MODELS_TXT = os.path.join(STATE, "venice-gen-models.txt")
REPORT = os.path.join(STATE, "genprobe-report.json")

IMG_PROMPT = ("A small owl made of glowing golden circuitry perched on a "
              "marble column, dark background, elegant, detailed")
VID_PROMPT = "A small golden-circuitry owl blinks and turns its head, dark background"


def say(s: str) -> None:
    print(str(s).encode("ascii", "backslashreplace").decode("ascii"))


def main() -> int:
    want_video = "--video" in sys.argv
    key = tg._cfg("VENICE_API_KEY")
    if not key:
        say("No Venice key yet.")
        say("Open config\\venice.env in Notepad and paste your key after")
        say("VENICE_API_KEY=   then save and run this again.")
        return 0

    say("Step 1: listing your image + video models...")
    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": False}
    try:
        imgs = tg._list_models("image", key)
        vids = tg._list_models("video", key)
        os.makedirs(STATE, exist_ok=True)
        with open(MODELS_TXT, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Venice generation models for this key. Pin one with\n")
            f.write("# VENICE_IMAGE_MODEL= / VENICE_VIDEO_MODEL= in config\\venice.env\n")
            f.write("# (e2ee-* models are skipped by the auto-pick)\n")
            for i in imgs:
                f.write(f"image\t{i}\n")
            for v in vids:
                f.write(f"video\t{v}\n")
        say(f"  {len(imgs)} image models, {len(vids)} video models "
            "- full list: state\\venice-gen-models.txt")
        report["image_models"] = len(imgs)
        report["video_models"] = len(vids)
    except (RuntimeError, OSError) as e:
        say(f"  Could not list models: {e}")
        say("  (Check the key and your internet connection, then retry.)")
        return 1

    say("Step 2: one governed image through the gateway (costs cents)...")
    os.makedirs(PROBE_STATE, exist_ok=True)
    gw = GatewayClient(
        [sys.executable, os.path.join(ROOT, "scripts", "run_gateway.py"),
         "--state-dir", PROBE_STATE],
        extra_env={"MINERVA_TOOLS": "generate_image,generate_video"})
    img_path = ""
    try:
        gw.initialize()
        t0 = time.time()
        text, err = gw.call("generate_image",
                            {"prompt": IMG_PROMPT, "format": "png"})
        dt = round(time.time() - t0, 1)
        report["image_result"] = text[:300]
        report["image_seconds"] = dt
        if err or not text.startswith("image saved:"):
            say(f"  Image FAILED ({dt}s): {text[:200]}")
        else:
            rel = text.split("image saved: ", 1)[1].split(" ", 1)[0]
            img_path = os.path.join(PROBE_STATE, "workspace",
                                    rel.replace("/", os.sep))
            say(f"  Image OK ({dt}s). OPEN IT:")
            say(f"    {img_path}")
            report["image_ok"] = True
            report["image_file"] = img_path

        if want_video and report.get("image_ok"):
            say("Step 3: video - getting the price quote first...")
            model = ""
            try:
                model = tg._resolve_model("video", None, key)
            except RuntimeError as e:
                say(f"  no video model: {e}")
            if model:
                qs, qraw, _h = tg._request(
                    "POST", tg.api_base() + "/video/quote", key,
                    {"model": model, "duration": "5s"}, timeout=30.0,
                    byte_cap=100_000)
                quote = None
                if qs == 200:
                    try:
                        quote = float(json.loads(qraw.decode())["quote"])
                    except (json.JSONDecodeError, KeyError, ValueError):
                        quote = None
                if quote is None:
                    say("  Could not get a quote - skipping the video test.")
                else:
                    say(f"  A 5s video on '{model}' costs ${quote:.2f}.")
                    ans = input("  Type yes to generate it (anything else skips): ")
                    if ans.strip().lower() in ("y", "yes"):
                        say("  Generating (takes a few minutes - be patient)...")
                        t0 = time.time()
                        try:
                            text, err = gw.call(
                                "generate_video",
                                {"prompt": VID_PROMPT, "model": model,
                                 "duration": "5s"})
                        except (TimeoutError, RuntimeError, OSError) as e:
                            text, err = (f"client-side failure: {e} - if the "
                                         "job was queued its id is saved in "
                                         "state\\genprobe\\workspace\\generated"
                                         "\\video-jobs.jsonl (resume with "
                                         "generate_video queue_id+model)", True)
                        dt = round(time.time() - t0, 1)
                        report["video_result"] = text[:300]
                        report["video_seconds"] = dt
                        if err or not text.startswith("video saved:"):
                            say(f"  Video FAILED ({dt}s): {text[:200]}")
                        else:
                            rel = text.split("video saved: ", 1)[1].split(" ", 1)[0]
                            vp = os.path.join(PROBE_STATE, "workspace",
                                              rel.replace("/", os.sep))
                            say(f"  Video OK ({dt}s). OPEN IT:")
                            say(f"    {vp}")
                            report["video_ok"] = True
                            report["video_file"] = vp
                    else:
                        say("  Skipped the video (your call).")
    finally:
        gw.close()

    ok_chain, n, _ = Ledger(os.path.join(PROBE_STATE, "ledger.jsonl")).verify_chain()
    report["probe_ledger_entries"] = n
    report["probe_chain_intact"] = ok_chain
    report["ok"] = bool(report.get("image_ok") and ok_chain)
    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=2)

    say("")
    if report["ok"]:
        video_failed = ("video_result" in report
                        and not report.get("video_ok"))
        if video_failed:
            say("IMAGE LANE WORKS - but the VIDEO test FAILED (see the error")
            say("  above; full detail in state\\genprobe-report.json):")
        else:
            say("GENERATION LANE WORKS:")
        say("  - governed generate_image ran through the gateway")
        say(f"  - probe ledger: {n} entries, hash chain INTACT")
        say("  - to pin models: VENICE_IMAGE_MODEL= / VENICE_VIDEO_MODEL=")
        say("    in config\\venice.env (catalog: state\\venice-gen-models.txt)")
        return 1 if video_failed else 0
    say(f"Summary: image_ok={report.get('image_ok', False)} "
        f"chain={ok_chain} - detail: state\\genprobe-report.json")
    return 1


if __name__ == "__main__":
    sys.exit(main())
