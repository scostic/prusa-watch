"""Replay saved frames through old (v1) and new (v2) prompts and compare verdicts.

All frames in eval/job83 come from a print that finished fine, so every non-ok verdict is a false alarm.
Reference frame = the previous saved frame if it is 1-6 minutes older (mimics the live agent).

    set PYTHONPATH=prusa_watch/app
    python eval/replay.py eval/job83 --runs 1
"""
import argparse
import collections
import glob
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "prusa_watch", "app"))

from prusa_watch import config  # noqa: E402
from prusa_watch import vision  # noqa: E402

V1_PROMPT = """You watch a Prusa MK3.5S FDM 3D printer through a Buddy3D camera and decide whether the \
print is failing. The MK3.5S is a bed-slinger: the bed moves front/back (Y) and the print head moves \
left/right (X) and up (Z), so the part and nozzle legitimately sit in different places from frame to frame. \
Lighting can change slightly. Judge the printed object itself, not its position.

You get the CURRENT frame and usually a REFERENCE frame from a few minutes earlier, plus live printer \
telemetry. Look for:
- spaghetti: loose tangled filament strands, extrusion into the air.
- clog_or_under_extrusion: the part has not grown between reference and current frame while Z/progress \
advanced, gaps or very thin/stringy walls, missing layers, nozzle moving over the part with nothing coming out.
- detached_part: part knocked over, moved on the bed, or stuck to the nozzle.
- blob_on_nozzle: a growing blob of plastic around the hotend.
- layer_shift, warping (corners lifting), heavy stringing.
- camera_problem: ONLY when the frame itself is unusable - black, blurred, covered, or pointing away \
from the printer.

It is normal and NOT a camera problem when the print head or gantry hides the part, or the bed has \
moved so the part is partly out of view, or there are strong sun/shadow areas. In that case judge \
whatever is visible (including the reference frame) and answer "ok" with lower confidence unless you \
actually see a defect.

Use "failure" only when you are fairly sure the print is ruined or about to damage the printer; "warning" \
for something suspicious worth another look; "ok" otherwise. Early in a print (first layers, small part) \
there is little to see - that is normal. Always answer by calling the report_assessment tool."""

V1_TOOL = {
    "name": "report_assessment",
    "description": "Report the assessment of the current print state.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(vision.STATUSES)},
            "issue": {"type": "string", "enum": list(vision.ISSUES)},
            "confidence": {"type": "number"},
            "description": {"type": "string"},
        },
        "required": ["status", "issue", "confidence", "description"],
        "additionalProperties": False,
    },
}

CONTEXT = ("state=PRINTING progress=~70% nozzle=250/250C bed=90/90C hotend_fan=4700rpm print_fan=1800rpm "
           "flow=100% speed=100%\n{ref}")


def frame_time(path: str) -> datetime:
    return datetime.strptime(os.path.basename(path)[8:23], "%Y%m%d-%H%M%S")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--config", default="config.local.json")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--variants", default="v1,v2")
    args = ap.parse_args()

    cfg = config.load(args.config)
    judges = {
        "v1": vision.VisionJudge(cfg.aws_region, cfg.bedrock_model_id, cfg.aws_access_key_id,
                                 cfg.aws_secret_access_key, system_prompt=V1_PROMPT, tool=V1_TOOL),
        "v2": vision.VisionJudge(cfg.aws_region, cfg.bedrock_model_id, cfg.aws_access_key_id,
                                 cfg.aws_secret_access_key),
    }
    variants = args.variants.split(",")
    frames = sorted(glob.glob(os.path.join(args.folder, "flagged-*.jpg")))
    results, cost = [], 0.0
    prev = None
    for path in frames:
        cur = open(path, "rb").read()
        ref, ref_note = None, "No reference frame."
        if prev and 60 <= (frame_time(path) - frame_time(prev)).total_seconds() <= 360:
            ref = open(prev, "rb").read()
            mins = (frame_time(path) - frame_time(prev)).total_seconds() / 60
            ref_note = f"Reference frame is {mins:.1f} min older than the current one."
        prev = path
        for variant in variants:
            for run in range(args.runs):
                v = judges[variant].assess(cur, ref, CONTEXT.format(ref=ref_note))
                u = judges[variant].last_usage
                cost += u["input_tokens"] * 1.1e-6 + u["output_tokens"] * 5.5e-6
                results.append({"frame": os.path.basename(path), "variant": variant, "run": run,
                                "has_ref": ref is not None, **v.as_dict()})
                print(f"{os.path.basename(path)} {variant} ref={ref is not None!s:5} "
                      f"{v.status:8} {v.issue:24} {v.confidence:.2f} vis={v.part_visible:6} "
                      f"{'DOWNGRADED ' if v.downgraded else ''}{v.description[:90]}", flush=True)

    out = os.path.join(args.folder, f"replay-{datetime.now():%Y%m%d-%H%M%S}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)

    print("\n=== summary (all frames are from a GOOD print: anything but ok is a false alarm) ===")
    for variant in variants:
        rs = [r for r in results if r["variant"] == variant]
        c = collections.Counter(r["status"] for r in rs)
        down = sum(1 for r in rs if r.get("downgraded"))
        print(f"{variant}: n={len(rs)}  ok={c['ok']}  warning={c['warning']}  failure={c['failure']}  "
              f"camera_problem={c['camera_problem']}  (guard downgrades: {down})")
    print(f"approx Bedrock cost: ${cost:.3f}  -> {out}")


if __name__ == "__main__":
    main()
