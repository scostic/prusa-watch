"""Print-failure judgement with Claude (Claude API or Amazon Bedrock)."""
import base64
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional

log = logging.getLogger(__name__)

STATUSES = ("ok", "warning", "failure", "camera_problem")
ISSUES = (
    "none", "spaghetti", "clog_or_under_extrusion", "detached_part", "blob_on_nozzle",
    "layer_shift", "warping", "stringing", "camera_problem", "other",
)

PART_VISIBILITY = ("fully", "partly", "hidden")

SYSTEM_PROMPT = """You watch a Prusa MK3.5S FDM 3D printer through a fixed Buddy3D camera inside an \
enclosure and decide whether the print is failing. False alarms are costly: they wake the owner and may \
pause a good print. Only report a failure when you can point at the defect in the CURRENT frame.

How this printer moves - read carefully, most false alarms come from this:
- It is a bed-slinger. The bed (black textured sheet printed with "ORIGINAL PRUSA") slides front/back \
all the time. The printed part is stuck to the bed and moves WITH it, so from one frame to the next the \
part appears in a different place in the image, larger or smaller, and seen from a different angle.
- The print head (black extruder block with a "PRUSA" label and fan shroud) and the X gantry move \
left/right and climb with Z. They very often cover part or most of the object, more so as it gets taller.
- Lattice, mesh and organic shapes look very different from different angles and distances.
So: the part being somewhere else in the image, looking different, or being partly or mostly hidden \
behind the head or gantry is NORMAL. It is never, on its own, evidence that the part detached, fell over \
or disappeared. Do not compare the part's position in the image between frames.

Report "failure" only with positive, visible evidence in the CURRENT frame:
- spaghetti: loose tangled strands of filament in the air, around the part or balled up on the nozzle.
- detached_part: you can clearly see the part lying tilted or on its side, sitting away from where it \
was printed relative to the bed sheet itself, or hanging from the nozzle - while the part is fully in view.
- blob_on_nozzle: a large lump of plastic engulfing the hotend (the black fan shroud is normal).
- clog_or_under_extrusion: new top layers visibly missing, very thin or full of gaps, or the part \
clearly has not grown although telemetry shows Z rose by several millimetres.
- layer_shift (layers offset sideways), warping (corners lifted off the bed), heavy stringing.
- camera_problem: ONLY when the image itself is unusable - black, blurred, covered, or not showing the printer.

You may also get an EXPECTED image: the slicer's preview of the FINISHED object from the G-code. Use it \
to know what shape you are looking for and where the part should be - a lattice, a thin tower or several \
small parts look very different from a solid block. The real part is only partly printed (see progress), \
seen from the camera's angle, in the filament colour (often greyscale at night). Something on the bed \
that cannot belong to that shape (loose strands, a lump, a toppled piece) is a defect; an unfinished or \
partly hidden version of the expected shape is not.

The REFERENCE frame (a few minutes older) is for judging growth and newly appearing defects only. \
Telemetry matters: stable temperatures at target, fans spinning and Z rising mean the printer is working.

Set part_visible honestly: "fully", "partly" (some of it hidden or out of frame) or "hidden". If the \
part is not fully visible, or you are unsure, answer "ok" with lower confidence (or "warning" if you see \
something odd) - never "failure" for detached_part. Use "failure" only when you would bet the print is \
ruined or the printer is at risk. Always answer by calling the report_assessment tool."""

TOOL = {
    "name": "report_assessment",
    "description": "Report the assessment of the current print state.",
    "input_schema": {
        "type": "object",
        "properties": {
            "part_visible": {"type": "string", "enum": list(PART_VISIBILITY),
                             "description": "How much of the printed part is visible in the CURRENT frame."},
            "evidence": {"type": "string",
                         "description": "The concrete visual evidence in the CURRENT frame behind the status "
                                        "(for ok: what looks healthy)."},
            "status": {"type": "string", "enum": list(STATUSES)},
            "issue": {"type": "string", "enum": list(ISSUES)},
            "confidence": {"type": "number", "description": "0.0-1.0 confidence in the status"},
            "description": {"type": "string", "description": "One or two sentences on what you see."},
        },
        "required": ["part_visible", "evidence", "status", "issue", "confidence", "description"],
        "additionalProperties": False,
    },
}


@dataclass
class Verdict:
    status: str
    issue: str
    confidence: float
    description: str
    part_visible: str = "fully"
    evidence: str = ""
    downgraded: Optional[str] = None     # why a guard rule softened the model's verdict

    @property
    def is_failure(self) -> bool:
        return self.status == "failure"

    def as_dict(self) -> dict:
        return asdict(self)


# Issues the model can see without the part itself being in view.
VISIBLE_WITHOUT_PART = {"spaghetti", "blob_on_nozzle"}


def apply_guards(v: Verdict) -> Verdict:
    """Deterministic sanity rules on top of the model (learned from job 83's false alarms)."""
    if v.status != "failure":
        return v
    reason = None
    if v.issue == "detached_part" and v.part_visible != "fully":
        reason = f"detached_part claimed but part is {v.part_visible} visible"
    elif v.part_visible == "hidden" and v.issue not in VISIBLE_WITHOUT_PART:
        reason = f"{v.issue} claimed but part is hidden"
    if reason:
        v.status, v.confidence, v.downgraded = "warning", min(v.confidence, 0.5), reason
    return v


def _normalise(d: dict) -> Verdict:
    status = str(d.get("status", "warning")).lower()
    if status not in STATUSES:
        status = "warning"
    issue = str(d.get("issue", "other")).lower()
    if issue not in ISSUES:
        issue = "other"
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    visible = str(d.get("part_visible", "fully")).lower()
    if visible not in PART_VISIBILITY:
        visible = "partly"
    return apply_guards(Verdict(status, issue, conf, str(d.get("description", "")).strip(),
                                visible, str(d.get("evidence", "")).strip()))


def parse_verdict(content) -> Verdict:
    """Extract the verdict from response content blocks (tool call preferred, JSON text as fallback)."""
    text_parts = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "tool_use" and getattr(block, "name", "") == TOOL["name"]:
            data = block.input if isinstance(block.input, dict) else json.loads(block.input)
            return _normalise(data)
        if btype == "text":
            text_parts.append(block.text)
    text = "".join(text_parts)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return _normalise(json.loads(text[start:end + 1]))
        except json.JSONDecodeError:
            pass
    return Verdict("warning", "other", 0.0, f"Unparseable model reply: {text[:200]}")


def _image_block(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg",
                   "data": base64.standard_b64encode(jpeg).decode("ascii")},
    }


# USD per million tokens (input, output) for cost tracking; unknown models report no cost.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),                               # Claude API, prompts < 100k tokens
    "claude-haiku-4-5": (1.00, 5.00),
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0": (1.10, 5.50),    # Bedrock EU cross-region profile
    "anthropic.claude-haiku-5-5": (0.10, 0.50),
}
PROVIDERS = ("bedrock", "anthropic")


def cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    price = PRICES.get(model_id)
    if not price:
        return None
    return round(input_tokens * price[0] / 1e6 + output_tokens * price[1] / 1e6, 6)


class VisionJudge:
    def __init__(self, region: str, model_id: str, access_key: str = "", secret_key: str = "",
                 system_prompt: str = SYSTEM_PROMPT, tool: Optional[dict] = None,
                 provider: str = "bedrock", api_key: str = ""):
        # SDK imported lazily so tests don't need it
        self.model_id = model_id
        self.provider = provider
        self.system_prompt = system_prompt
        self.tool = tool or TOOL
        self.last_usage: dict = {}
        if provider == "anthropic":
            from anthropic import Anthropic

            self.client = Anthropic(api_key=api_key or None, max_retries=3, timeout=60.0)
        else:
            from anthropic import AnthropicBedrock

            self.client = AnthropicBedrock(
                aws_region=region,
                aws_access_key=access_key or None,
                aws_secret_key=secret_key or None,
                max_retries=3,
                timeout=60.0,
            )

    def assess(self, current: bytes, reference: Optional[bytes], context: str,
               expected: Optional[bytes] = None) -> Verdict:
        content: list[dict] = []
        if expected is not None:
            content += [{"type": "text", "text": "EXPECTED (slicer preview of the finished object):"},
                        _image_block(expected)]
        if reference is not None:
            content += [{"type": "text", "text": "REFERENCE frame (earlier):"}, _image_block(reference)]
        content += [{"type": "text", "text": "CURRENT frame:"}, _image_block(current)]
        content.append({"type": "text", "text": f"Telemetry and context:\n{context}"})

        started = time.monotonic()
        response = self.client.messages.create(
            model=self.model_id,
            max_tokens=512,
            system=self.system_prompt,
            tools=[self.tool],
            tool_choice={"type": "tool", "name": self.tool["name"]},
            messages=[{"role": "user", "content": content}],
        )
        self.last_usage = {
            "latency_ms": round((time.monotonic() - started) * 1000),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        cost = cost_usd(self.model_id, response.usage.input_tokens, response.usage.output_tokens)
        if cost is not None:
            self.last_usage["cost_usd"] = cost
        log.debug("%s usage: %s", self.provider, self.last_usage)
        if response.stop_reason == "refusal":
            return Verdict("warning", "other", 0.0, "Model declined to assess this frame.")
        return parse_verdict(response.content)
