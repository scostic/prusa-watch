import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "prusa_watch", "app"))

from prusa_watch import config  # noqa: E402
from prusa_watch.camera import change_score  # noqa: E402
from prusa_watch.prusalink import parse_status  # noqa: E402
from prusa_watch.vision import Verdict, parse_verdict  # noqa: E402
from prusa_watch.watchdog import Watchdog  # noqa: E402

FAIL = Verdict("failure", "spaghetti", 0.9, "tangle")
WEAK = Verdict("failure", "spaghetti", 0.3, "maybe")
OK = Verdict("ok", "none", 0.95, "fine")
WARN = Verdict("warning", "stringing", 0.6, "hmm")


class WatchdogTest(unittest.TestCase):
    def test_alert_after_threshold_then_cooldown(self):
        w = Watchdog(3, 0.6, cooldown_s=600)
        self.assertFalse(w.observe(FAIL, 0).alert)
        self.assertFalse(w.observe(FAIL, 60).alert)
        self.assertTrue(w.observe(FAIL, 120).alert)
        self.assertFalse(w.observe(FAIL, 180).alert)        # cooldown
        self.assertTrue(w.observe(FAIL, 720).alert)         # cooldown elapsed

    def test_ok_resets_warning_and_weak_hold(self):
        w = Watchdog(2, 0.6, cooldown_s=0)
        w.observe(FAIL, 0)
        w.observe(WARN, 1)
        w.observe(WEAK, 2)
        self.assertEqual(w.streak, 1)
        w.observe(OK, 3)
        self.assertEqual(w.streak, 0)

    def test_auto_action_once_per_job(self):
        w = Watchdog(2, 0.6, cooldown_s=9999, auto_action="pause", auto_action_threshold=3)
        actions = [w.observe(FAIL, t).action for t in range(5)]
        self.assertEqual(actions, [None, None, "pause", None, None])
        w.new_job(42)
        actions = [w.observe(FAIL, t).action for t in range(3)]
        self.assertEqual(actions, [None, None, "pause"])

    def test_action_always_alerts_even_in_cooldown(self):
        w = Watchdog(1, 0.6, cooldown_s=9999, auto_action="stop", auto_action_threshold=2)
        self.assertTrue(w.observe(FAIL, 0).alert)
        d = w.observe(FAIL, 1)
        self.assertEqual((d.alert, d.action), (True, "stop"))

    def test_no_action_when_disabled(self):
        w = Watchdog(1, 0.6, cooldown_s=0)
        self.assertIsNone(w.observe(FAIL, 0).action)


class ParseTest(unittest.TestCase):
    def test_status(self):
        st = parse_status({
            "job": {"id": 7, "progress": 42.0, "time_remaining": 600},
            "printer": {"state": "PRINTING", "temp_nozzle": 215.1, "target_nozzle": 215.0,
                        "axis_z": 3.2, "fan_hotend": 5000},
        })
        self.assertEqual((st.state, st.job_id, st.axis_z), ("PRINTING", 7, 3.2))
        self.assertIn("z=3.20mm", st.summary())
        self.assertEqual(parse_status({}).state, "UNKNOWN")

    def test_verdict_from_tool(self):
        block = SimpleNamespace(type="tool_use", name="report_assessment",
                                input={"status": "failure", "issue": "clog_or_under_extrusion",
                                       "confidence": 1.4, "description": "no growth"})
        v = parse_verdict([block])
        self.assertEqual((v.status, v.issue, v.confidence), ("failure", "clog_or_under_extrusion", 1.0))

    def test_verdict_from_text_and_garbage(self):
        txt = SimpleNamespace(type="text", text='Sure: {"status":"ok","issue":"none","confidence":0.8,"description":"x"}')
        self.assertEqual(parse_verdict([txt]).status, "ok")
        bad = SimpleNamespace(type="text", text="no idea")
        self.assertEqual(parse_verdict([bad]).status, "warning")
        odd = SimpleNamespace(type="tool_use", name="report_assessment",
                              input={"status": "explode", "issue": "aliens", "confidence": "x"})
        v = parse_verdict([odd])
        self.assertEqual((v.status, v.issue, v.confidence), ("warning", "other", 0.0))

    def test_guards_downgrade_unseen_detachment(self):
        def tool(**kw):
            base = {"status": "failure", "issue": "detached_part", "confidence": 0.95,
                    "description": "gone", "evidence": "x"}
            base.update(kw)
            return [SimpleNamespace(type="tool_use", name="report_assessment", input=base)]
        v = parse_verdict(tool(part_visible="partly"))
        self.assertEqual((v.status, v.confidence), ("warning", 0.5))
        self.assertIn("partly", v.downgraded)
        self.assertEqual(parse_verdict(tool(part_visible="fully")).status, "failure")
        self.assertEqual(parse_verdict(tool(part_visible="hidden", issue="layer_shift")).status, "warning")
        self.assertEqual(parse_verdict(tool(part_visible="hidden", issue="spaghetti")).status, "failure")

    def test_change_score(self):
        self.assertEqual(change_score(b"\x00" * 10, b"\x00" * 10), 0.0)
        self.assertAlmostEqual(change_score(b"\x00" * 10, b"\xff" * 10), 1.0)


class HECTest(unittest.TestCase):
    def test_disabled_without_token(self):
        from prusa_watch.notify import HECSender
        self.assertFalse(HECSender("http://s:8088", "").enabled)

    def test_batches_backlog_until_success(self):
        from unittest import mock
        import requests
        from prusa_watch.notify import HECSender
        h = HECSender("http://s:8088", "tok", index="prusa_watch")
        self.assertEqual(h.url, "http://s:8088/services/collector/event")
        h.session = mock.Mock()
        h.session.post.side_effect = requests.ConnectionError("down")
        h.send({"a": 1}, 1.0)
        h.send({"a": 2}, 2.0)
        self.assertEqual(len(h.backlog), 2)
        h.session.post.side_effect = None
        h.session.post.return_value = mock.Mock(raise_for_status=lambda: None)
        h.send({"a": 3}, 3.0)
        body = h.session.post.call_args.kwargs["data"]
        self.assertEqual(len(body.splitlines()), 3)
        self.assertIn('"index":"prusa_watch"', body)
        self.assertEqual(h.backlog, [])


class ThumbnailRefTest(unittest.TestCase):
    def test_refs(self):
        from prusa_watch.prusalink import thumbnail_ref
        job = {"file": {"name": "A~1.BGC", "display_name": "a.bgcode",
                        "refs": {"icon": "/thumb/s/usb/A~1.BGC", "thumbnail": "/thumb/l/usb/A~1.BGC"}}}
        self.assertEqual(thumbnail_ref(job), ("/thumb/l/usb/A~1.BGC", "a.bgcode"))
        job["file"]["refs"].pop("thumbnail")
        self.assertEqual(thumbnail_ref(job)[0], "/thumb/s/usb/A~1.BGC")
        self.assertEqual(thumbnail_ref(None), (None, ""))
        self.assertEqual(thumbnail_ref({"serial_print": True}), (None, ""))


class EnergyMeterTest(unittest.TestCase):
    def test_trapezoid_and_gaps(self):
        from prusa_watch.notify import EnergyMeter
        m = EnergyMeter()
        for i in range(61):                 # 1 h in 60 s steps, ramp 100 -> 300 W: avg 200 W -> 200 Wh
            m.add(i * 60, 100.0 + i * 200 / 60)
        self.assertAlmostEqual(m.wh, 200.0)
        m.add(3600 + 3600, 300.0)           # 1 h gap > MAX_GAP_S: ignored
        self.assertAlmostEqual(m.wh, 200.0)
        m.add(7260, None)                   # unavailable breaks the chain
        m.add(7320, 300.0)
        self.assertAlmostEqual(m.wh, 200.0)
        m.reset()
        self.assertEqual((m.wh, m.samples), (0.0, 0))


class ConfigTest(unittest.TestCase):
    def _write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_placeholders_rejected(self):
        with self.assertRaises(ValueError):
            config.load(self._write({"printer_host": "192.168.1.X", "camera_url": "rtsp://192.168.1.Y/live"}))

    def test_auto_threshold_raised(self):
        cfg = config.load(self._write({
            "printer_host": "10.0.0.5", "camera_url": "rtsp://10.0.0.6/live",
            "email_from": "a@b.c", "email_to": "x@y.z, q@w.e", "auto_action": "pause",
            "failure_threshold": 3, "auto_action_threshold": 2, "extra": 1,
        }))
        self.assertEqual(cfg.auto_action_threshold, 4)
        self.assertEqual(cfg.recipients, ["x@y.z", "q@w.e"])


if __name__ == "__main__":
    unittest.main()
