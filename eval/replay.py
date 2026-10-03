"""Replay frames through prompt versions and measure them.

Labelled dataset (recommended) - samples saved by the add-on and labelled in the HA "Prusa Watch" panel:
    copy \\<ha>\share\prusa_watch\dataset to eval/dataset, then
    python eval/replay.py --dataset eval/dataset --variants recorded,v2
  `recorded` re-scores what the live model said at the time (free, no API calls);
  `v2` (current prompt, with the G-code thumbnail when the sample has one), `v2-noexp` (same without the
  thumbnail - measures its value) and `v1` (original prompt) re-run Bedrock on the saved frames + context.
  Reports a confusion matrix, recall (failures caught), precision and false-alarm rate.

Legacy mode - a folder of flagged-*.jpg frames from a print that went fine (every non-ok = false alarm):
    python eval/replay.py eval/job83 --variants v1,v2

PYTHONPATH=prusa_watch/app and a config.local.json with AWS credentials are needed for v1/v2.
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
from prusa_watch import evaluation  # noqa: E402
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

LEGACY_CONTEXT = ("state=PRINTING progress=~70% nozzle=250/250C bed=90/90C hotend_fan=4700rpm "
                  "print_fan=1800rpm flow=100% speed=100%\n{ref}")
PRICE_IN, PRICE_OUT = 1.1e-6, 5.5e-6        # Haiku 4.5 via EU profile, USD per token (approx.)


def make_judges(cfg_path: str, variants: list[str]) -> dict:
    needed = [v for v in variants if v in ("v1", "v2", "v2-noexp")]
    if not needed:
        return {}
    cfg = config.load(cfg_path)
    args = (cfg.aws_region, cfg.bedrock_model_id, cfg.aws_access_key_id, cfg.aws_secret_access_key)
    judges = {"v2": lambda: vision.VisionJudge(*args), "v2-noexp": lambda: vision.VisionJudge(*args),
              "v1": lambda: vision.VisionJudge(*args, system_prompt=V1_PROMPT, tool=V1_TOOL)}
    return {v: judges[v]() for v in needed}


def read(path):
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------- labelled dataset
def run_dataset(args) -> None:
    variants = args.variants.split(",")
    unknown = set(variants) - {"recorded", "v1", "v2", "v2-noexp"}
    if unknown:
        sys.exit(f"unknown variants: {unknown}")
    judges = make_judges(args.config, variants)
    positives = set(args.positive.split(","))

    samples = []
    for meta_path in sorted(glob.glob(os.path.join(args.dataset, "*", "meta.json"))):
        with open(meta_path, encoding="utf-8") as f:
            m = json.load(f)
        if m.get("label"):
            samples.append((os.path.dirname(meta_path), m))
    if not samples:
        sys.exit("no labelled samples - label some in the Home Assistant 'Prusa Watch' panel first")
    n_fail = sum(m["label"]["truth"] == "failure" for _, m in samples)
    print(f"{len(samples)} labelled samples ({n_fail} real failures, {len(samples) - n_fail} ok)\n")

    rows = collections.defaultdict(list)
    cost = 0.0
    for folder, m in samples:
        truth = {"truth": m["label"]["truth"], "truth_issue": m["label"].get("issue", "none")}
        for variant in variants:
            if variant == "recorded":
                v = m["verdict"]
            else:
                ref_path = os.path.join(folder, "reference.jpg")
                exp_path = os.path.join(folder, "expected.jpg")
                use_exp = variant == "v2" and os.path.isfile(exp_path)
                verdict = judges[variant].assess(read(os.path.join(folder, "current.jpg")),
                                                 read(ref_path) if os.path.isfile(ref_path) else None,
                                                 m.get("context", ""),
                                                 read(exp_path) if use_exp else None)
                u = judges[variant].last_usage
                cost += u["input_tokens"] * PRICE_IN + u["output_tokens"] * PRICE_OUT
                v = verdict.as_dict()
            rows[variant].append({**truth, "id": m["id"], "status": v["status"], "issue": v["issue"],
                                  "confidence": v.get("confidence"), "description": v.get("description", "")})
            mark = "MISS " if truth["truth"] == "failure" and v["status"] not in positives else \
                   "FALSE" if truth["truth"] == "ok" and v["status"] in positives else "     "
            print(f"{mark} {m['id']:22} truth={truth['truth']:7} {variant:8} {v['status']:14} "
                  f"{v['issue']:24} {str(v.get('description', ''))[:70]}", flush=True)

    print(f"\n=== results (positive = model status in {sorted(positives)}) ===")
    report = {}
    for variant in variants:
        report[variant] = evaluation.confusion(rows[variant], positives)
        print(evaluation.format_report(variant, report[variant]))
    out = os.path.join(args.dataset, f"eval-{datetime.now():%Y%m%d-%H%M%S}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"positives": sorted(positives), "metrics": report, "rows": rows}, f, indent=1)
    print(f"\napprox Bedrock cost: ${cost:.3f}  -> {out}")


# ---------------------------------------------------------------- legacy: flagged frames of a good print
def frame_time(path: str) -> datetime:
    return datetime.strptime(os.path.basename(path)[8:23], "%Y%m%d-%H%M%S")


def run_legacy(args) -> None:
    variants = args.variants.split(",")
    judges = make_judges(args.config, variants)
    frames = sorted(glob.glob(os.path.join(args.folder, "flagged-*.jpg")))
    results, cost, prev = [], 0.0, None
    for path in frames:
        cur = read(path)
        ref, ref_note = None, "No reference frame."
        if prev and 60 <= (frame_time(path) - frame_time(prev)).total_seconds() <= 360:
            ref = read(prev)
            ref_note = f"Reference frame is {(frame_time(path) - frame_time(prev)).total_seconds() / 60:.1f} " \
                       "min older than the current one."
        prev = path
        for variant in variants:
            for run in range(args.runs):
                v = judges[variant].assess(cur, ref, LEGACY_CONTEXT.format(ref=ref_note))
                u = judges[variant].last_usage
                cost += u["input_tokens"] * PRICE_IN + u["output_tokens"] * PRICE_OUT
                results.append({"frame": os.path.basename(path), "variant": variant, "run": run,
                                "has_ref": ref is not None, **v.as_dict()})
                print(f"{os.path.basename(path)} {variant} {v.status:14} {v.issue:24} {v.confidence:.2f} "
                      f"{'DOWNGRADED ' if v.downgraded else ''}{v.description[:80]}", flush=True)
    out = os.path.join(args.folder, f"replay-{datetime.now():%Y%m%d-%H%M%S}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    print("\n=== summary (all frames are from a GOOD print: anything but ok is a false alarm) ===")
    for variant in variants:
        c = collections.Counter(r["status"] for r in results if r["variant"] == variant)
        print(f"{variant}: ok={c['ok']} warning={c['warning']} failure={c['failure']} "
              f"camera_problem={c['camera_problem']}")
    print(f"approx Bedrock cost: ${cost:.3f}  -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", help="legacy: folder with flagged-*.jpg from a good print")
    ap.add_argument("--dataset", help="folder of labelled samples (<id>/meta.json, current.jpg, reference.jpg)")
    ap.add_argument("--config", default="config.local.json")
    ap.add_argument("--variants", default=None, help="dataset: recorded,v2,v2-noexp,v1   legacy: v1,v2")
    ap.add_argument("--positive", default="failure", help="model statuses counted as an alarm, e.g. failure,warning")
    ap.add_argument("--runs", type=int, default=1, help="legacy mode only")
    args = ap.parse_args()
    if args.dataset:
        args.variants = args.variants or "recorded,v2"
        run_dataset(args)
    elif args.folder:
        args.variants = args.variants or "v1,v2"
        run_legacy(args)
    else:
        ap.error("give --dataset DIR or a legacy frames folder")


if __name__ == "__main__":
    main()
