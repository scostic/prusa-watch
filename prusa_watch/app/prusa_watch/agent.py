"""Main monitoring loop."""
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional

from . import camera
from .config import Config
from . import __version__
from .notify import CloudWatchHeartbeat, HASensor, HECSender, Mailer
from .prusalink import PrinterStatus, PrusaLink
from .vision import Verdict, VisionJudge
from .watchdog import Watchdog

log = logging.getLogger(__name__)

ATTENTION_STATES = {"ERROR", "ATTENTION"}
CAMERA_FAIL_ALERT_AFTER = 5
CONFIRM_DELAY_S = 4


@dataclass
class Frame:
    t: float
    jpeg: bytes
    thumb: bytes
    z: Optional[float]


class Agent:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.printer = PrusaLink(cfg.printer_host, cfg.prusalink_username, cfg.prusalink_password,
                                 api_key=cfg.prusalink_api_key)
        self.judge = VisionJudge(cfg.aws_region, cfg.bedrock_model_id,
                                 cfg.aws_access_key_id, cfg.aws_secret_access_key)
        self.mailer = Mailer(cfg.aws_region, cfg.email_from, cfg.recipients,
                             cfg.aws_access_key_id, cfg.aws_secret_access_key)
        self.ha = HASensor(cfg.ha_sensor)
        self.hec = HECSender(cfg.hec_url, cfg.hec_token, cfg.hec_index, sourcetype="prusa:watch",
                             host=cfg.printer_host, verify_tls=cfg.hec_verify_tls)
        self.cloudwatch = CloudWatchHeartbeat(cfg.cloudwatch_heartbeat, cfg.aws_region,
                                              cfg.aws_access_key_id, cfg.aws_secret_access_key)
        self.last_beat = 0.0
        self.printing_since: Optional[float] = None
        self.last_good_check: Optional[float] = None
        self.blind_alerted = False
        self.printer_offline = False
        self.watchdog = Watchdog(cfg.failure_threshold, cfg.min_confidence,
                                 cfg.alert_cooldown_min * 60, cfg.auto_action, cfg.auto_action_threshold)
        self.history: deque[Frame] = deque()
        self.prev_state: Optional[str] = None
        self.camera_failures = 0
        self.camera_alerted = False
        self.last_verdict: Optional[Verdict] = None

    # ---------- helpers ----------
    def _event(self, kind: str, st: Optional[PrinterStatus] = None,
               verdict: Optional[Verdict] = None, **fields) -> None:
        """One Splunk HEC event (no-op when HEC is not configured)."""
        ev: dict = {"type": kind}
        if st is not None:
            ev.update(vars(st))
        if verdict is not None:
            ev.update(verdict.as_dict(), model=self.cfg.bedrock_model_id)
        ev.update(fields)
        self.hec.send({k: v for k, v in ev.items() if v is not None}, time.time())

    def _email(self, subject: str, body: str, jpeg: Optional[bytes] = None) -> None:
        if self.dry_run:
            log.warning("[dry-run] would email: %s\n%s", subject, body)
            self._event("email", subject=subject, sent=False, dry_run=True)
            return
        try:
            self.mailer.send(subject, body, jpeg,
                             filename=f"prusa-{datetime.now():%Y%m%d-%H%M%S}.jpg")
            self._event("email", subject=subject, sent=True)
        except Exception as e:
            log.exception("Failed to send email")
            self._event("email", subject=subject, sent=False, error=str(e))

    def _grab(self) -> Optional[bytes]:
        try:
            jpeg = camera.grab_jpeg(self.cfg.camera_url, rotate=self.cfg.camera_rotate)
        except camera.CameraError as e:
            self.camera_failures += 1
            log.warning("Camera grab failed (%d in a row): %s", self.camera_failures, e)
            self._event("error", component="camera", error=str(e), consecutive=self.camera_failures)
            if self.camera_failures >= CAMERA_FAIL_ALERT_AFTER and not self.camera_alerted:
                self.camera_alerted = True
                self._email("[Prusa Watch] Camera unreachable during print",
                            f"The camera at {self.cfg.camera_url} has failed {self.camera_failures} "
                            f"times in a row, so the print is NOT being watched.\n\nLast error: {e}")
            return None
        self.camera_failures = 0
        self.camera_alerted = False
        return jpeg

    def _save(self, jpeg: bytes, flagged: bool) -> None:
        d = self.cfg.snapshot_dir
        if not d:
            return
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "latest.jpg"), "wb") as f:
                f.write(jpeg)
            if flagged:
                with open(os.path.join(d, f"flagged-{datetime.now():%Y%m%d-%H%M%S}.jpg"), "wb") as f:
                    f.write(jpeg)
        except OSError as e:
            log.debug("Snapshot save skipped: %s", e)

    def _reference(self, now: float) -> Optional[Frame]:
        """Newest frame that is at least compare_minutes old, else the oldest we have."""
        target = now - self.cfg.compare_minutes * 60
        older = [f for f in self.history if f.t <= target]
        if older:
            return older[-1]
        return self.history[0] if self.history else None

    def _prune(self, now: float) -> None:
        keep_after = now - self.cfg.compare_minutes * 60 * 2
        while len(self.history) > 1 and self.history[0].t < keep_after:
            self.history.popleft()

    def _check_blind(self, st: PrinterStatus, now: float) -> None:
        """Alert when a print runs but no AI check has succeeded for blind_alert_min minutes."""
        if self.printing_since is None or self.blind_alerted:
            return
        since = max(self.printing_since, self.last_good_check or 0.0)
        blind_for = now - since
        if blind_for < self.cfg.blind_alert_min * 60:
            return
        self.blind_alerted = True
        mins = round(blind_for / 60)
        log.error("Watchdog blind: no successful check for %d min while printing", mins)
        self._event("error", st, component="watchdog", error=f"blind for {mins} min", blind_min=mins)
        self._email(
            "[Prusa Watch] Watchdog is BLIND - print not being checked",
            self._status_body(st, extra=(
                f"The printer has been printing for a while, but there has been no successful AI check "
                f"for {mins} minutes. The print is NOT being watched.\n"
                f"Likely causes: camera stream down, Bedrock/AWS error, or the Pi overloaded. "
                f"Check the Prusa Watch log in Home Assistant.")),
            self._grab())

    def heartbeat(self, now: float) -> None:
        """Proof of life for the outside world (CloudWatch alarm + Splunk), every heartbeat_min."""
        if now - self.last_beat < self.cfg.heartbeat_min * 60:
            return
        self.last_beat = now
        printing = self.prev_state == "PRINTING"
        self.cloudwatch.beat(printing)
        self._event("heartbeat", printer_state=self.prev_state, version=__version__,
                    streak=self.watchdog.streak, blind=self.blind_alerted,
                    last_check_age_s=round(now - self.last_good_check) if self.last_good_check else None)

    def _confirm(self, first: Verdict, ref: Optional[Frame], context: list[str]) -> tuple[Verdict, dict]:
        """Second opinion on a fresh frame (bed/head in a different position) before a failure counts."""
        time.sleep(CONFIRM_DELAY_S)
        jpeg = self._grab()
        if jpeg is None:
            return first, {"confirm_status": "no_frame"}
        second = self.judge.assess(
            jpeg, ref.jpeg if ref else None,
            "\n".join(context + ["This is a confirmation frame taken a few seconds after a suspected "
                                 f"{first.issue}. Judge it independently."]))
        info = {"confirm_status": second.status, "confirm_issue": second.issue,
                "confirm_confidence": round(second.confidence, 2)}
        if second.is_failure:
            return first, info
        return replace(first, status="warning", confidence=min(first.confidence, 0.5),
                       downgraded=f"not confirmed by a second frame "
                                  f"({second.status}: {second.description[:120]})"), info

    @staticmethod
    def _ha_attrs(st: PrinterStatus) -> dict:
        """Printer telemetry for the HA sensor (used by dashboards and the SenseCAP Indicator page)."""
        return {"printer_state": st.state, "job_id": st.job_id, "progress": st.progress,
                "time_remaining": st.time_remaining, "temp_nozzle": st.temp_nozzle,
                "target_nozzle": st.target_nozzle, "temp_bed": st.temp_bed, "target_bed": st.target_bed,
                "axis_z": st.axis_z}

    def _status_body(self, st: PrinterStatus, verdict: Optional[Verdict] = None, extra: str = "") -> str:
        lines = []
        if verdict:
            lines += [f"Assessment : {verdict.status.upper()} ({verdict.issue}, confidence {verdict.confidence:.0%})",
                      f"What it sees: {verdict.description}", ""]
        if extra:
            lines += [extra, ""]
        lines += [f"Printer    : {st.summary()}",
                  f"Remaining  : {round(st.time_remaining / 60)} min" if st.time_remaining else "Remaining  : n/a",
                  f"PrusaLink  : {self.printer.base}",
                  f"Time       : {datetime.now():%Y-%m-%d %H:%M:%S}"]
        return "\n".join(lines)

    # ---------- main tick ----------
    def tick(self) -> None:
        now = time.time()
        try:
            st = self.printer.status()
        except Exception as e:
            # Report the transition once; a switched-off printer is normal (the heartbeat proves we're alive).
            if not self.printer_offline:
                self.printer_offline = True
                log.warning("PrusaLink unreachable (printer off?): %s", e)
                self._event("error", component="prusalink", error=str(e))
            else:
                log.debug("PrusaLink still unreachable: %s", e)
            self.ha.publish("offline", error=str(e))
            return
        if self.printer_offline:
            self.printer_offline = False
            log.info("PrusaLink reachable again")
            self._event("state", st, previous_state="OFFLINE")

        if st.job_id is not None and st.job_id != self.watchdog.job_id:
            log.info("New job %s detected", st.job_id)
            self.watchdog.new_job(st.job_id)
            self.history.clear()
            self.last_verdict = None

        changed = st.state != self.prev_state
        if changed:
            log.info("Printer state: %s -> %s", self.prev_state, st.state)
            self._event("state", st, previous_state=self.prev_state)

        if changed and st.state in ATTENTION_STATES:
            jpeg = self._grab()
            self._email(f"[Prusa Watch] Printer needs attention: {st.state}",
                        self._status_body(st, extra="The printer itself reported a problem "
                                                    "(e.g. filament runout, thermal or fan error)."), jpeg)
        elif changed and st.state == "FINISHED" and self.prev_state == "PRINTING" and self.cfg.notify_finished:
            self._email("[Prusa Watch] Print finished", self._status_body(st), self._grab())

        self.prev_state = st.state
        if st.state != "PRINTING":
            # After a pause/attention the scene may have changed (filament swap, user at the printer):
            # start fresh instead of comparing against a stale reference frame.
            self.history.clear()
            self.printing_since, self.blind_alerted = None, False
            self.ha.publish(st.state.lower(), **self._ha_attrs(st))
            return

        if self.printing_since is None:
            self.printing_since = now
        self._check_blind(st, now)

        jpeg = self._grab()
        if jpeg is None:
            self.ha.publish("camera_error", **self._ha_attrs(st))
            return
        try:
            thumb = camera.thumbnail(jpeg)
        except camera.CameraError:
            thumb = b""

        ref = self._reference(now)
        context = [st.summary()]
        metrics: dict = {}
        if ref:
            mins = (now - ref.t) / 60
            metrics["ref_age_min"] = round(mins, 1)
            context.append(f"Reference frame is {mins:.1f} min older than the current one.")
            if thumb and ref.thumb:
                change = camera.change_score(thumb, ref.thumb)
                metrics["change_pct"] = round(change * 100, 2)
                context.append(f"Whole-image pixel change vs reference: {change:.1%}")
            if st.axis_z is not None and ref.z is not None:
                metrics["z_delta"] = round(st.axis_z - ref.z, 2)
                context.append(f"Z moved {st.axis_z - ref.z:+.2f} mm since the reference frame.")
        else:
            context.append("No reference frame yet (first check of this print).")

        try:
            verdict = self.judge.assess(jpeg, ref.jpeg if ref else None, "\n".join(context))
        except Exception as e:
            log.warning("Bedrock call failed: %s", e)
            self._event("error", st, component="bedrock", error=str(e))
            self.ha.publish("analysis_error", error=str(e), **self._ha_attrs(st))
            return

        self.last_good_check = now
        if self.blind_alerted:
            log.info("Watchdog can see again")
            self.blind_alerted = False
        usage = dict(self.judge.last_usage)
        if verdict.is_failure and self.cfg.confirm_failures:
            verdict, confirm = self._confirm(verdict, ref, context)
            metrics.update(confirm)
            for k in ("input_tokens", "output_tokens", "latency_ms"):
                usage[k] = usage.get(k, 0) + self.judge.last_usage.get(k, 0)

        self.last_verdict = verdict
        log.info("Verdict: %s/%s %.2f vis=%s - %s%s", verdict.status, verdict.issue, verdict.confidence,
                 verdict.part_visible, verdict.description,
                 f" [downgraded: {verdict.downgraded}]" if verdict.downgraded else "")
        self._save(jpeg, flagged=verdict.status in ("warning", "failure"))

        decision = self.watchdog.observe(verdict, now)
        action_note = ""
        if decision.action and st.job_id is not None:
            if self.dry_run:
                action_note = f"[dry-run] Would have sent {decision.action.upper()} to the printer."
            else:
                try:
                    (self.printer.pause if decision.action == "pause" else self.printer.stop)(st.job_id)
                    action_note = f"Prusa Watch sent {decision.action.upper()} to the printer."
                    log.warning(action_note)
                except Exception as e:
                    action_note = f"Tried to {decision.action} the print but it FAILED: {e}"
                    log.error(action_note)

        if decision.alert:
            verb = {"pause": "PAUSED", "stop": "STOPPED"}.get(decision.action, "Possible failure")
            subject = f"[Prusa Watch] {verb}: {verdict.issue.replace('_', ' ')}"
            extra = (f"{self.watchdog.streak} consecutive failure verdicts.\n{action_note}").strip()
            self._email(subject, self._status_body(st, verdict, extra), jpeg)

        self._event("check", st, verdict, streak=self.watchdog.streak, alert=decision.alert,
                    action=decision.action, action_result=action_note or None,
                    **metrics, **usage)
        self.ha.publish(
            verdict.status, issue=verdict.issue, confidence=round(verdict.confidence, 2),
            description=verdict.description, streak=self.watchdog.streak, part_visible=verdict.part_visible,
            last_check=datetime.now().isoformat(timespec="seconds"), **self._ha_attrs(st),
        )
        self.history.append(Frame(now, jpeg, thumb, st.axis_z))
        self._prune(now)

    def run(self, once: bool = False) -> None:
        log.info("Watching %s, camera %s, model %s, every %ss (auto_action=%s)",
                 self.cfg.printer_host, self.cfg.camera_url, self.cfg.bedrock_model_id,
                 self.cfg.check_interval_s, self.cfg.auto_action)
        if self.cfg.prusalink_api_key:
            auth = f"API key ({len(self.cfg.prusalink_api_key)} chars)"
        elif self.cfg.prusalink_password:
            auth = f"digest login as {self.cfg.prusalink_username!r}"
        else:
            auth = "NONE - set prusalink_api_key in the configuration"
        log.info("PrusaLink auth: %s", auth)
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                log.exception("Unexpected error in monitoring loop")
            try:
                self.heartbeat(time.time())
            except Exception:
                log.exception("Heartbeat failed")
            if once:
                return
            time.sleep(max(1.0, self.cfg.check_interval_s - (time.monotonic() - started)))
