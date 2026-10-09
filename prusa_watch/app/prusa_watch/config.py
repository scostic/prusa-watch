import json
import logging
from dataclasses import dataclass, fields

log = logging.getLogger(__name__)

AUTO_ACTIONS = ("none", "pause", "stop")
PROVIDERS = ("bedrock", "anthropic")


@dataclass
class Config:
    printer_host: str
    prusalink_api_key: str = ""
    prusalink_username: str = "maker"
    prusalink_password: str = ""
    camera_url: str = ""
    camera_rotate: int = 0
    aws_region: str = "eu-central-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    bedrock_model_id: str = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    ai_provider: str = "bedrock"                 # bedrock | anthropic (Claude API)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    email_from: str = ""
    email_to: str = ""
    check_interval_s: int = 60
    compare_minutes: int = 5
    failure_threshold: int = 3
    min_confidence: float = 0.6
    confirm_failures: bool = True
    use_gcode_thumbnail: bool = True
    skip_preheat: bool = True          # no AI checks while heating up at 0% progress   # show the model the slicer preview of the object being printed
    alert_cooldown_min: int = 30
    notify_finished: bool = True
    auto_action: str = "none"
    auto_action_threshold: int = 5
    ha_sensor: bool = True
    power_entity: str = ""            # HA sensor with the printer plug's power in W
    energy_price: float = 0.0         # price per kWh, for the cost per print
    currency: str = "€"
    dataset_enabled: bool = True      # keep labelled samples for evaluation (/share/prusa_watch/dataset)
    dataset_ok_every: int = 10        # keep 1 in N 'ok' checks (all warnings/failures are kept)
    dataset_max: int = 2000
    label_button_correct: str = "input_button.prusa_watch_correct"
    label_button_false_alarm: str = "input_button.prusa_watch_false_alarm"
    label_button_missed: str = "input_button.prusa_watch_missed_failure"
    heartbeat_min: int = 5
    cloudwatch_heartbeat: bool = True
    blind_alert_min: int = 10
    hec_url: str = ""
    hec_token: str = ""
    hec_index: str = "prusa_watch"
    hec_verify_tls: bool = True
    snapshot_dir: str = "/share/prusa_watch"
    log_level: str = "info"

    @property
    def model_id(self) -> str:
        return self.anthropic_model if self.ai_provider == "anthropic" else self.bedrock_model_id

    @property
    def recipients(self) -> list[str]:
        return [a.strip() for a in self.email_to.split(",") if a.strip()]

    def validate(self) -> None:
        # Pasted secrets often carry stray spaces/newlines.
        for name in ("printer_host", "camera_url", "prusalink_api_key", "prusalink_password",
                     "aws_region", "aws_access_key_id", "aws_secret_access_key",
                     "bedrock_model_id", "email_from", "email_to", "hec_url", "hec_token", "hec_index", "power_entity",
                     "ai_provider", "anthropic_api_key", "anthropic_model"):
            setattr(self, name, str(getattr(self, name) or "").strip())
        problems = []
        if self.ai_provider not in PROVIDERS:
            problems.append(f"ai_provider must be one of {PROVIDERS}")
        if self.ai_provider == "anthropic" and not self.anthropic_api_key:
            problems.append("anthropic_api_key is required when ai_provider is 'anthropic'")
        if not self.printer_host or "X" in self.printer_host:
            problems.append("printer_host is not set")
        if not self.camera_url or "Y" in self.camera_url:
            problems.append("camera_url is not set")
        if not self.email_from or not self.recipients:
            problems.append("email_from / email_to are not set")
        try:
            self.camera_rotate = int(self.camera_rotate)   # HA list() options arrive as strings
        except (TypeError, ValueError):
            self.camera_rotate = -1
        if self.camera_rotate not in (0, 90, 180, 270):
            problems.append("camera_rotate must be 0, 90, 180 or 270")
        if self.auto_action not in AUTO_ACTIONS:
            problems.append(f"auto_action must be one of {AUTO_ACTIONS}")
        if problems:
            raise ValueError("; ".join(problems))
        # Never act before the user has had at least one warning email.
        if self.auto_action != "none" and self.auto_action_threshold <= self.failure_threshold:
            log.warning(
                "auto_action_threshold (%d) <= failure_threshold (%d); raising it to %d",
                self.auto_action_threshold, self.failure_threshold, self.failure_threshold + 1,
            )
            self.auto_action_threshold = self.failure_threshold + 1


def load(path: str) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    known = {f.name for f in fields(Config)}
    for key in set(raw) - known:
        log.warning("Ignoring unknown option %r", key)
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    cfg.validate()
    return cfg
