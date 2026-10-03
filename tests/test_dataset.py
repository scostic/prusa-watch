import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "prusa_watch", "app"))

from prusa_watch import agent as agent_mod  # noqa: E402
from prusa_watch import webui  # noqa: E402
from prusa_watch.config import Config  # noqa: E402
from prusa_watch.dataset import Dataset  # noqa: E402
from prusa_watch.prusalink import PrinterStatus  # noqa: E402
from prusa_watch.vision import Verdict  # noqa: E402

OK = {"status": "ok", "issue": "none", "confidence": 0.9, "description": "fine"}
FAIL = {"status": "failure", "issue": "spaghetti", "confidence": 0.9, "description": "tangle"}


class DatasetTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_keeps_flagged_and_samples_ok(self):
        ds = Dataset(self.root, ok_every=3)
        kept = [ds.save(1000 + i, 1, b"J", None, "ctx", OK, "m") for i in range(6)]
        self.assertEqual(sum(k is not None for k in kept), 2)          # 1 in 3
        sid = ds.save(2000, 1, b"J", b"R", "ctx", FAIL, "m")
        self.assertIsNotNone(sid)
        m = ds.get(sid)
        self.assertEqual((m["has_reference"], m["label"], m["verdict"]["issue"]), (True, None, "spaghetti"))
        self.assertTrue(os.path.isfile(ds.image_path(sid, "reference")))

    def test_label_filter_and_stats(self):
        ds = Dataset(self.root, ok_every=1)
        a = ds.save(1000, 1, b"J", None, "c", FAIL, "m")
        b = ds.save(1060, 1, b"J", None, "c", OK, "m")
        ds.label(a, "ok")
        self.assertEqual([m["id"] for m in ds.samples("unlabelled")], [b])
        self.assertEqual([m["id"] for m in ds.samples("flagged")], [a])
        self.assertEqual(ds.stats(), {"samples": 2, "flagged": 1, "labelled": 1, "truth_failure": 0})
        with self.assertRaises(ValueError):
            ds.label(b, "maybe")
        with self.assertRaises(KeyError):
            ds.label("nope", "ok")

    def test_prune_drops_unlabelled_ok_first(self):
        ds = Dataset(self.root, ok_every=1, max_samples=3)
        f = ds.save(1000, 1, b"J", None, "c", FAIL, "m")
        o1 = ds.save(1060, 1, b"J", None, "c", OK, "m")
        ds.label(o1, "ok")
        ds.save(1120, 1, b"J", None, "c", OK, "m")
        ds.save(1180, 1, b"J", None, "c", OK, "m")                    # 4th -> prune one unlabelled ok
        ids = ds.ids()
        self.assertEqual(len(ids), 3)
        self.assertIn(f, ids)
        self.assertIn(o1, ids)

    def test_path_traversal_rejected(self):
        ds = Dataset(self.root)
        self.assertIsNone(ds.get("../etc"))
        self.assertIsNone(ds.image_path("..\\x", "current"))


@mock.patch.object(agent_mod.camera, "thumbnail", return_value=b"\x10" * 64)
@mock.patch.object(agent_mod.camera, "grab_jpeg", return_value=b"JPEG")
class AgentDatasetTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        agent_mod.CONFIRM_DELAY_S = 0

    def make(self, **over):
        opts = {"snapshot_dir": self.root, "dataset_ok_every": 1, **over}
        cfg = Config(printer_host="10.0.0.5", camera_url="rtsp://10.0.0.6/live", email_from="a@b.c",
                     email_to="me@x.y", **opts)
        cfg.validate()
        with mock.patch.object(agent_mod, "VisionJudge"), mock.patch.object(agent_mod, "Mailer"), \
                mock.patch.object(agent_mod, "CloudWatchHeartbeat"):
            a = agent_mod.Agent(cfg)
        a.printer = mock.Mock(base="http://10.0.0.5")
        a.judge.last_usage = {}
        a.hec = mock.Mock()
        a.ha = mock.Mock()
        self.buttons = {}
        a.ha.read_state.side_effect = lambda e: self.buttons.get(e)
        a.printer.status.return_value = PrinterStatus("PRINTING", job_id=3)
        return a

    def test_checks_saved_and_buttons_label(self, _grab, _thumb):
        a = self.make()
        a.judge.assess.return_value = Verdict("warning", "stringing", 0.7, "strings")
        self.buttons = {"input_button.prusa_watch_correct": "2026-10-03T10:00:00",
                        "input_button.prusa_watch_false_alarm": "2026-10-03T10:00:00",
                        "input_button.prusa_watch_missed_failure": "unknown"}
        a.tick()                                              # first sight: remember, no action
        sid = a.dataset.latest(flagged_only=True)["id"]
        self.assertIsNone(a.dataset.get(sid)["label"])
        self.buttons["input_button.prusa_watch_false_alarm"] = "2026-10-03T10:05:00"
        with mock.patch.object(agent_mod.time, "time", return_value=5_000_000_000.0):
            a.tick()                                          # press -> label latest flagged as ok
        labelled = a.dataset.samples("labelled")
        self.assertEqual(len(labelled), 1)
        self.assertEqual(labelled[0]["label"]["truth"], "ok")
        self.assertEqual(labelled[0]["label"]["source"], "button")

    def test_missed_failure_forces_sample(self, _grab, _thumb):
        a = self.make(dataset_ok_every=1000)                  # ok checks normally not kept
        a.judge.assess.return_value = Verdict("ok", "none", 0.9, "fine")
        self.buttons = {"input_button.prusa_watch_missed_failure": "t0"}
        a.tick()
        self.assertEqual(a.dataset.ids(), [])
        self.buttons["input_button.prusa_watch_missed_failure"] = "t1"
        a.tick()
        lab = a.dataset.samples("labelled")
        self.assertEqual((len(lab), lab[0]["label"]["truth"], lab[0]["verdict"]["status"]), (1, "failure", "ok"))

    def test_disabled(self, _grab, _thumb):
        self.assertIsNone(self.make(dataset_enabled=False).dataset)
        self.assertIsNone(self.make(snapshot_dir="").dataset)


class WebUITest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.ds = Dataset(self.root, ok_every=1)
        self.sid = self.ds.save(1000, 7, b"\xff\xd8JPEG", None, "ctx", FAIL, "m")
        self.server = webui.start(self.ds, 0)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.read()

    def test_page_list_image_and_label(self):
        status, page = self.get("")
        self.assertEqual(status, 200)
        self.assertIn(b"Prusa Watch - labels", page)
        self.assertEqual(json.loads(self.get("api/samples?filter=flagged")[1])[0]["id"], self.sid)
        self.assertEqual(self.get(f"img/{self.sid}/current.jpg")[1], b"\xff\xd8JPEG")
        req = urllib.request.Request(self.base + "api/label", method="POST",
                                     data=json.dumps({"id": self.sid, "truth": "failure",
                                                      "issue": "spaghetti"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.loads(r.read())["label"]["truth"], "failure")
        self.assertEqual(json.loads(self.get("api/stats")[1])["truth_failure"], 1)

    def test_bad_requests(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("img/../meta.json")
        self.assertEqual(e.exception.code, 404)
        req = urllib.request.Request(self.base + "api/label", method="POST",
                                     data=b'{"id": "x", "truth": "maybe"}')
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
