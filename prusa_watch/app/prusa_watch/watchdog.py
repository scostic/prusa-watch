"""Decision logic: turns a stream of verdicts into alerts and (optionally) pause/stop actions."""
from dataclasses import dataclass
from typing import Optional

from .vision import Verdict


@dataclass
class Decision:
    alert: bool = False
    action: Optional[str] = None   # "pause" | "stop" | None


class Watchdog:
    def __init__(self, failure_threshold: int, min_confidence: float, cooldown_s: float,
                 auto_action: str = "none", auto_action_threshold: int = 0):
        self.failure_threshold = failure_threshold
        self.min_confidence = min_confidence
        self.cooldown_s = cooldown_s
        self.auto_action = auto_action
        self.auto_action_threshold = auto_action_threshold
        self.job_id = None
        self.reset()

    def reset(self) -> None:
        self.streak = 0
        self.last_alert: Optional[float] = None
        self.action_taken = False

    def new_job(self, job_id) -> None:
        self.job_id = job_id
        self.reset()

    def observe(self, verdict: Verdict, now: float) -> Decision:
        if verdict.is_failure and verdict.confidence >= self.min_confidence:
            self.streak += 1
        elif verdict.status == "ok":
            self.streak = 0
        # "warning", low-confidence failures and camera problems neither build nor clear the streak.

        decision = Decision()
        if self.streak >= self.failure_threshold and (
            self.last_alert is None or now - self.last_alert >= self.cooldown_s
        ):
            decision.alert = True

        if (self.auto_action != "none" and not self.action_taken
                and self.streak >= self.auto_action_threshold):
            decision.action = self.auto_action
            decision.alert = True          # always tell the user what we did
            self.action_taken = True

        if decision.alert:
            self.last_alert = now
        return decision
