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

PROMPT_VERSION = "v3"

# v3 (0.9.0): after job 101 (flat PETG parts came loose and were dragged around, all checks said "ok")
# the layout of the parts ON THE BED SHEET is checked against a fixed first-layer LAYOUT frame, and
# detached_part no longer needs the part fully in view. v2 is kept in eval/replay.py for comparison.
SYSTEM_PROMPT = """You watch a Prusa MK3.5S FDM 3D printer through a fixed Buddy3D camera inside an \
enclosure and decide whether the print is failing. False alarms are costly: they wake the owner and may \
pause a good print. Missed failures are costly too: a part that came loose gets dragged around by the \
nozzle, knocks other parts off and can damage the printer. Report what you can point at in the CURRENT frame.

How this printer moves:
- It is a bed-slinger. The bed (black textured sheet printed with "ORIGINAL PRUSA") slides front/back \
all the time. Printed parts are stuck to the sheet and move WITH it, so from one frame to the next they \
appear in a different place in the IMAGE, larger or smaller, and seen from a different angle.
- The print head (black extruder block with a "PRUSA" label and fan shroud) and the X gantry move \
left/right and climb with Z. They often cover part or most of the objects, more so as they get taller.
- Lattice, mesh and organic shapes look very different from different angles and distances.
So a part's position in the IMAGE, its apparent size, or it being partly hidden behind the head or \
gantry is NORMAL and never, on its own, evidence of a problem.

What must NOT change is the layout ON THE BED SHEET. Judge positions relative to the sheet itself \
(its edges, the "ORIGINAL PRUSA" print, the sheet texture) and relative to the other parts, never \
relative to the image frame. Parts never move on a good print: each part keeps the same spot on the \
sheet, the same orientation and the same distance to its neighbours, from the first layer to the end. \
Check this on every frame:
- Count the parts you can see and where they sit on the sheet. Compare with the LAYOUT frame and the \
EXPECTED image (several separate parts on the slicer preview = several separate parts on the bed).
- A part that has shifted or rotated on the sheet, two parts that were apart now touching or piled up, \
a part pushed against the edge or off the sheet, a part tilted, standing on its edge or lying upside \
down, a part stuck to or dragged by the nozzle, or an empty spot on the sheet where the LAYOUT frame \
shows a part (and the head is not covering that spot) - each of these is detached_part.
- detached_part does NOT need every part in view: one clearly displaced part is enough, even if others \
are hidden. Flat parts with only a few layers come loose easily (PETG, warping corners) - watch them closely.

Report "failure" with positive, visible evidence in the CURRENT frame:
- detached_part: as above - a part has moved, turned, tipped over, piled onto another, is missing from \
its spot on the sheet or is hanging from the nozzle.
- spaghetti: loose tangled strands of filament in the air, around the parts or balled up on the nozzle.
- blob_on_nozzle: a large lump of plastic engulfing the hotend (the black fan shroud is normal).
- clog_or_under_extrusion: new top layers visibly missing, very thin or full of gaps, or the parts \
clearly have not grown although telemetry shows Z rose by several millimetres.
- layer_shift (layers offset sideways), warping (corners lifted off the bed), heavy stringing.
- camera_problem: ONLY when the image itself is unusable - black, blurred, covered, or not showing the printer.
Small wisps of filament, debris or blobs near the nozzle or between parts are a "warning" at least: say \
exactly where they are. If you see something odd but cannot pin it down, answer "warning", not "ok".
Not being able to see is NOT odd: when the head or gantry covers the parts so you cannot check the \
layout, and nothing you can see is wrong, answer "ok" with lower confidence (never "warning" or \
"camera_problem" just for that). Judge the layout on the parts you can see.
Hidden or missing? Before you say a part from LAYOUT is "hidden", check that the head, gantry or frame \
really covers the spot on the sheet where that part sits (find the spot via the sheet edges and the \
"ORIGINAL PRUSA" print). If that patch of black sheet is visible and empty, the part is MISSING: that \
is detached_part, even if the other parts are fine. A missing part plus a loose lump or tangle nearby \
is a "failure", not a warning.

The images you may get:
- EXPECTED: the slicer's preview of the FINISHED objects from the G-code: their shapes, how many there \
are and how they are arranged on the bed. The real parts are only partly printed (see progress), seen \
from the camera's angle, in the filament colour (often greyscale at night). Something that cannot belong \
to those shapes (loose strands, a lump, a toppled piece) is a defect; an unfinished or partly hidden \
version of them is not.
- LAYOUT: a frame from the first layers of THIS print, taken when everything was fine. It is the ground \
truth for where each part sits on the sheet. The bed may be at a different Y position in it, so compare \
positions relative to the sheet, not the image.
- REFERENCE: a frame a few minutes older, for judging growth and newly appearing defects.
Telemetry matters: stable temperatures at target, fans spinning and Z rising mean the printer is working \
- but they do not tell you whether the parts are still in place.

Set part_visible honestly: "fully", "partly" (some of it hidden or out of frame) or "hidden". Put the \
layout comparison in evidence (e.g. "4 parts in the same places as in LAYOUT" or "front-left part has \
moved onto its neighbour"). Use "failure" when you would bet the print is ruined or the printer is at \
risk. Always answer by calling the report_assessment tool."""

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
    """Deterministic sanity rules on top of the model (learned from job 83's false alarms).
    Since 0.9.0 detached_part counts with the part "partly" visible (job 101: a displaced part is enough);
    it is still downgraded when the model cannot see the part at all."""
    if v.status != "failure":
        return v
    reason = None
    if v.part_visible == "hidden" and v.issue not in VISIBLE_WITHOUT_PART:
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


# SDK exception class name -> (problem, is it a configuration error the user must fix?)
_ERROR_KINDS = {
    "AuthenticationError": ("invalid API key / credentials (401)", True),
    "PermissionDeniedError": ("no access to this model (403)", True),
    "NotFoundError": ("model not found (404) - check the model id", True),
    "BadRequestError": ("request rejected (400)", True),
    "RateLimitError": ("rate limited (429)", False),
    "APIConnectionError": ("cannot reach the AI service (network)", False),
    "APITimeoutError": ("AI service timed out", False),
}


def classify_error(e: Exception) -> tuple[str, bool]:
    """(human description, is_config_error) for an exception from the Anthropic SDK or boto."""
    for cls in type(e).__mro__:
        if cls.__name__ in _ERROR_KINDS:
            return _ERROR_KINDS[cls.__name__]
    return f"{type(e).__name__}", False


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

    def check(self) -> tuple[bool, str, bool]:
        """Startup self-test: (ok, detail, is_config_error).
        Claude API: free Models API lookup. Bedrock: a 1-token request (fractions of a cent)."""
        try:
            if self.provider == "anthropic":
                m = self.client.with_options(max_retries=1).models.retrieve(self.model_id)
                return True, f"{m.id} available", False
            self.client.with_options(max_retries=1).messages.create(
                model=self.model_id, max_tokens=1, messages=[{"role": "user", "content": "ping"}])
            return True, f"{self.model_id} answered", False
        except Exception as e:                         # noqa: BLE001 - classify everything
            what, config_error = classify_error(e)
            return False, f"{what}: {e}", config_error

    def assess(self, current: bytes, reference: Optional[bytes], context: str,
               expected: Optional[bytes] = None, layout: Optional[bytes] = None) -> Verdict:
        content: list[dict] = []
        if expected is not None:
            content += [{"type": "text", "text": "EXPECTED (slicer preview of the finished object):"},
                        _image_block(expected)]
        if layout is not None:
            content += [{"type": "text", "text": "LAYOUT frame (first layers of this print, parts in place):"},
                        _image_block(layout)]
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
