import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "prusa_watch", "app"))

from prusa_watch import agent as agent_mod  # noqa: E402
from prusa_watch.config import Config  # noqa: E402
from prusa_watch.prusalink import PrinterStatus  # noqa: E402
from prusa_watch.vision import Verdict  # noqa: E402


agent_mod.CONFIRM_DELAY_S = 0


def make_agent(**over):
    cfg = Config(printer_host="10.0.0.5", camera_url="rtsp://10.0.0.6/live", email_from="a@b.c",
                 email_to="me@x.y", failure_threshold=2, snapshot_dir="", **over)
    cfg.validate()
    with mock.patch.object(agent_mod, "VisionJudge"), mock.patch.object(agent_mod, "Mailer"), \
            mock.patch.object(agent_mod, "CloudWatchHeartbeat"):
        a = agent_mod.Agent(cfg)
    a.printer = mock.Mock(base="http://10.0.0.5")
    a.printer.job.return_value = None                     # no G-code thumbnail unless a test sets one
    a.judge.last_usage = {"latency_ms": 900, "input_tokens": 1500, "output_tokens": 80}
    a.hec = mock.Mock()
    return a


@mock.patch.object(agent_mod.camera, "thumbnail", return_value=b"\x10" * 64)
@mock.patch.object(agent_mod.camera, "grab_jpeg", return_value=b"JPEG")
class AgentTickTest(unittest.TestCase):
    def test_failure_streak_emails_and_pauses(self, _grab, _thumb):
        a = make_agent(auto_action="pause", auto_action_threshold=3)
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=9, axis_z=1.0)
        a.judge.assess.return_value = Verdict("failure", "spaghetti", 0.9, "tangle")
        for _ in range(3):
            a.tick()
        self.assertEqual(a.mailer.send.call_count, 2)            # alert at 2, action at 3
        a.printer.pause.assert_called_once_with(9)
        subject = a.mailer.send.call_args_list[-1].args[0]
        self.assertIn("PAUSED", subject)
        # each tick = main check + confirmation; the 2nd tick's main check has the 1st frame as reference
        self.assertEqual(a.judge.assess.call_count, 6)
        _, ref, ctx, _expected = a.judge.assess.call_args_list[2].args
        self.assertEqual(ref, b"JPEG")
        self.assertIn("Z moved", ctx)

    def test_unconfirmed_failure_is_downgraded(self, grab, _thumb):
        a = make_agent()
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=9)
        a.judge.assess.side_effect = [
            Verdict("failure", "spaghetti", 0.9, "tangle"), Verdict("ok", "none", 0.9, "fine"),
        ] * 3
        for _ in range(3):
            a.tick()
        self.assertEqual(a.watchdog.streak, 0)
        a.mailer.send.assert_not_called()
        self.assertEqual(a.last_verdict.status, "warning")
        self.assertIn("not confirmed", a.last_verdict.downgraded)
        check = a.hec.send.call_args.args[0]
        self.assertEqual((check["confirm_status"], check["input_tokens"]), ("ok", 3000))

    def test_confirm_can_be_disabled(self, grab, _thumb):
        a = make_agent(confirm_failures=False)
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=9)
        a.judge.assess.return_value = Verdict("failure", "spaghetti", 0.9, "tangle")
        a.tick()
        self.assertEqual(a.judge.assess.call_count, 1)
        self.assertEqual(a.watchdog.streak, 1)

    def test_ha_sensor_carries_telemetry(self, _grab, _thumb):
        a = make_agent()
        a.ha = mock.Mock()
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine", part_visible="partly")
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=4, progress=40.0, temp_nozzle=249.8,
                                                      target_nozzle=250.0, time_remaining=3600)
        a.tick()
        state, attrs = a.ha.publish.call_args.args[0], a.ha.publish.call_args.kwargs
        self.assertEqual(state, "ok")
        self.assertEqual((attrs["printer_state"], attrs["temp_nozzle"], attrs["time_remaining"],
                          attrs["part_visible"]), ("PRINTING", 249.8, 3600, "partly"))
        a.printer.status.return_value = PrinterStatus("FINISHED", job_id=4, progress=100.0)
        a.tick()
        self.assertEqual(a.ha.publish.call_args.args[0], "finished")
        self.assertEqual(a.ha.publish.call_args.kwargs["progress"], 100.0)

    def test_blind_alert_once_then_rearms(self, _grab, _thumb):
        a = make_agent(blind_alert_min=10)
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=2)
        a.judge.assess.side_effect = RuntimeError("bedrock down")
        with mock.patch.object(agent_mod.time, "time") as clock:
            for minute in range(0, 16):
                clock.return_value = 1000.0 + minute * 60
                a.tick()
            subjects = [c.args[0] for c in a.mailer.send.call_args_list]
            self.assertEqual(sum("BLIND" in s for s in subjects), 1)       # once, at ~10 min
            a.judge.assess.side_effect = None
            a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
            clock.return_value += 60
            a.tick()
            self.assertFalse(a.blind_alerted)                                # recovered, re-armed

    def test_no_blind_alert_when_idle(self, _grab, _thumb):
        a = make_agent(blind_alert_min=3)
        a.printer.status.return_value = PrinterStatus("IDLE")
        with mock.patch.object(agent_mod.time, "time") as clock:
            for minute in range(10):
                clock.return_value = 1000.0 + minute * 60
                a.tick()
        a.mailer.send.assert_not_called()

    def test_printer_offline_reported_once(self, _grab, _thumb):
        a = make_agent()
        a.printer.status.side_effect = ConnectionError("Host is unreachable")
        for _ in range(5):
            a.tick()
        errors = [c.args[0] for c in a.hec.send.call_args_list if c.args[0]["type"] == "error"]
        self.assertEqual(len(errors), 1)
        a.printer.status.side_effect = None
        a.printer.status.return_value = PrinterStatus("IDLE")
        a.tick()
        states = [c.args[0] for c in a.hec.send.call_args_list if c.args[0]["type"] == "state"]
        self.assertEqual(states[0]["previous_state"], "OFFLINE")          # recovery reported once
        self.assertFalse(a.printer_offline)

    def test_energy_per_print(self, _grab, _thumb):
        a = make_agent(power_entity="sensor.printer_plug_current_consumption", energy_price=0.17114)
        a.ha = mock.Mock()
        a.ha.read_number.return_value = 200.0                     # constant 200 W
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=7)
        with mock.patch.object(agent_mod.time, "time") as clock:
            for minute in range(61):                                # one hour of printing
                clock.return_value = 1000.0 + minute * 60
                a.tick()
            self.assertAlmostEqual(a.energy.kwh, 0.2, places=3)
            self.assertEqual(a.ha.publish.call_args.kwargs["power_w"], 200.0)
            ctx = a.judge.assess.call_args.args[2]
            self.assertIn("Printer plug power: 200 W", ctx)
            a.printer.status.return_value = PrinterStatus("FINISHED", job_id=7)
            clock.return_value += 60
            a.tick()
        body = a.mailer.send.call_args.args[1]
        self.assertIn("0.20 kWh", body)
        self.assertIn("€0.03", body)
        energy = [c.args[0] for c in a.hec.send.call_args_list if c.args[0]["type"] == "energy"]
        self.assertEqual(energy[0]["print_energy_cost"], 0.034)

    def test_no_power_entity_no_energy(self, _grab, _thumb):
        a = make_agent()
        a.ha = mock.Mock()
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=7)
        a.tick()
        a.ha.read_number.assert_not_called()
        self.assertNotIn("print_energy_kwh", a.ha.publish.call_args.kwargs)

    def test_gcode_thumbnail_sent_once_per_job(self, _grab, _thumb):
        a = make_agent()
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=11)
        a.printer.job.return_value = {"id": 11, "file": {"display_name": "lattice_sphere.bgcode",
                                                         "refs": {"thumbnail": "/thumb/l/usb/LATTIC~1.BGC"}}}
        a.printer.fetch.return_value = b"QOIF..."
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        with mock.patch.object(agent_mod.camera, "to_jpeg", return_value=b"EXPJPEG") as conv:
            a.tick()
            a.tick()
        a.printer.fetch.assert_called_once_with("/thumb/l/usb/LATTIC~1.BGC")
        conv.assert_called_once_with(b"QOIF...")
        cur, ref, ctx, expected = a.judge.assess.call_args.args
        self.assertEqual(expected, b"EXPJPEG")
        self.assertIn("Print file: lattice_sphere.bgcode", ctx)

    def test_gcode_thumbnail_failure_does_not_block(self, _grab, _thumb):
        a = make_agent()
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=12)
        a.printer.job.side_effect = ConnectionError("boom")
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        a.tick()
        a.tick()
        self.assertEqual(a.printer.job.call_count, 1)                  # failure cached for the job
        self.assertIsNone(a.judge.assess.call_args.args[3])
        self.assertEqual(a.last_verdict.status, "ok")

    def test_gcode_thumbnail_disabled(self, _grab, _thumb):
        a = make_agent(use_gcode_thumbnail=False)
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=13)
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        a.tick()
        a.printer.job.assert_not_called()

    def test_heartbeat_throttled(self, _grab, _thumb):
        a = make_agent(heartbeat_min=5)
        a.prev_state = "PRINTING"
        for t in (0, 60, 299, 300, 360, 601):
            a.heartbeat(10_000.0 + t)
        self.assertEqual(a.cloudwatch.beat.call_count, 3)                  # t=0, 300, 601
        a.cloudwatch.beat.assert_called_with(True)
        beats = [c.args[0] for c in a.hec.send.call_args_list if c.args[0]["type"] == "heartbeat"]
        self.assertEqual((len(beats), beats[0]["printer_state"]), (3, "PRINTING"))
        self.assertEqual((beats[0]["provider"], beats[0]["model"]),
                         ("bedrock", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"))

    def test_no_ai_checks_while_preheating(self, grab, _thumb):
        a = make_agent(blind_alert_min=3)
        a.ha = mock.Mock()
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        heating = PrinterStatus("PRINTING", job_id=87, progress=0.0, temp_nozzle=170.0, target_nozzle=250.0,
                                temp_bed=44.0, target_bed=85.0)
        with mock.patch.object(agent_mod.time, "time") as clock:
            for minute in range(8):                                 # long preheat: no checks, no BLIND alert
                clock.return_value = 1000.0 + minute * 60
                a.printer.status.return_value = heating
                a.tick()
            a.judge.assess.assert_not_called()
            grab.assert_not_called()
            a.mailer.send.assert_not_called()
            self.assertEqual((a.ha.publish.call_args.args[0], a.ha.publish.call_args.kwargs["phase"]),
                             ("printing", "preheat"))
            clock.return_value += 60
            a.printer.status.return_value = PrinterStatus("PRINTING", job_id=87, progress=0.0,
                                                          temp_nozzle=249.0, target_nozzle=250.0,
                                                          temp_bed=84.0, target_bed=85.0)
            a.tick()                                                # at temperature: checks start
        a.judge.assess.assert_called_once()
        self.assertFalse(a.preheating)

    def test_preheat_rule(self, _grab, _thumb):
        P = PrinterStatus
        self.assertTrue(agent_mod.is_preheating(P("PRINTING", progress=0.0, temp_bed=40.0, target_bed=85.0)))
        self.assertFalse(agent_mod.is_preheating(P("PRINTING", progress=0.0, temp_bed=82.0, target_bed=85.0)))
        self.assertFalse(agent_mod.is_preheating(P("PRINTING", progress=5.0, temp_bed=40.0, target_bed=85.0)))
        self.assertFalse(agent_mod.is_preheating(P("PRINTING", progress=0.0)))          # no telemetry
        self.assertFalse(agent_mod.is_preheating(P("PRINTING", progress=0.0, temp_nozzle=25.0, target_nozzle=0.0)))

    def test_preheat_skip_can_be_disabled(self, _grab, _thumb):
        a = make_agent(skip_preheat=False)
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=1, progress=0.0, temp_bed=40.0, target_bed=85.0)
        a.tick()
        a.judge.assess.assert_called_once()

    def test_ai_selfcheck_ok(self, _grab, _thumb):
        a = make_agent()
        a.judge.check.return_value = (True, "claude-haiku-5-5 available", False)
        self.assertTrue(a.ai_selfcheck())
        ev = a.hec.send.call_args.args[0]
        self.assertEqual((ev["type"], ev["ok"]), ("ai_check", True))
        a.mailer.send.assert_not_called()

    def test_ai_selfcheck_bad_key_emails(self, _grab, _thumb):
        a = make_agent()
        a.judge.check.return_value = (False, "invalid API key / credentials (401): ...", True)
        self.assertFalse(a.ai_selfcheck())
        subject, body = a.mailer.send.call_args.args[:2]
        self.assertIn("AI provider not working", subject)
        self.assertIn("401", body)

    def test_ai_selfcheck_network_error_no_email(self, _grab, _thumb):
        a = make_agent()
        a.judge.check.return_value = (False, "cannot reach the AI service (network)", False)
        self.assertFalse(a.ai_selfcheck())
        a.mailer.send.assert_not_called()
        a.judge.check.side_effect = RuntimeError("boom")          # a crashing check never propagates
        self.assertFalse(a.ai_selfcheck())

    def test_ai_error_event_names_provider(self, _grab, _thumb):
        a = make_agent()
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=3)
        a.judge.assess.side_effect = RuntimeError("401 invalid x-api-key")
        a.tick()
        err = [c.args[0] for c in a.hec.send.call_args_list if c.args[0]["type"] == "error"][0]
        self.assertEqual((err["component"], err["provider"]), ("ai", "bedrock"))
        self.assertIn("401", err["error"])

    def test_pause_clears_reference(self, _grab, _thumb):
        a = make_agent()
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=1)
        a.tick()
        a.printer.status.return_value = PrinterStatus("PAUSED", job_id=1)
        a.tick()
        self.assertEqual(len(a.history), 0)

    def test_hec_events(self, _grab, _thumb):
        a = make_agent()
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=5, axis_z=2.0, progress=10.0)
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        a.tick()
        a.tick()
        events = [c.args[0] for c in a.hec.send.call_args_list]
        self.assertEqual([e["type"] for e in events], ["state", "check", "check"])
        check = events[-1]
        self.assertEqual((check["status"], check["job_id"], check["input_tokens"]), ("ok", 5, 1500))
        self.assertIn("change_pct", check)
        self.assertNotIn("action", check)          # None values are dropped

    def test_idle_printer_skips_vision(self, grab, _thumb):
        a = make_agent()
        a.printer.status.return_value = PrinterStatus("IDLE")
        a.tick()
        a.judge.assess.assert_not_called()
        grab.assert_not_called()

    def test_attention_state_emails_once(self, _grab, _thumb):
        a = make_agent()
        a.printer.status.return_value = PrinterStatus("ATTENTION", job_id=3)
        a.tick()
        a.tick()
        a.mailer.send.assert_called_once()
        self.assertIn("ATTENTION", a.mailer.send.call_args.args[0])

    def test_finished_email(self, _grab, _thumb):
        a = make_agent()
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=1)
        a.tick()
        a.printer.status.return_value = PrinterStatus("FINISHED", job_id=1)
        a.tick()
        self.assertIn("finished", a.mailer.send.call_args.args[0])

    def test_camera_down_alerts_after_5(self, grab, _thumb):
        grab.side_effect = agent_mod.camera.CameraError("no route")
        a = make_agent()
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=1)
        for _ in range(7):
            a.tick()
        a.mailer.send.assert_called_once()
        self.assertIn("Camera", a.mailer.send.call_args.args[0])

    def test_dry_run_never_acts(self, _grab, _thumb):
        a = make_agent(auto_action="stop", auto_action_threshold=3)
        a.dry_run = True
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=9)
        a.judge.assess.return_value = Verdict("failure", "detached_part", 0.95, "gone")
        for _ in range(4):
            a.tick()
        a.printer.stop.assert_not_called()
        a.mailer.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
