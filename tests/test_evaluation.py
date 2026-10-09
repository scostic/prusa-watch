import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "prusa_watch", "app"))

from prusa_watch.dataset import Dataset  # noqa: E402
from prusa_watch.evaluation import confusion  # noqa: E402


def row(truth, status, truth_issue="none"):
    return {"truth": truth, "truth_issue": truth_issue, "status": status, "issue": "x"}


class ConfusionTest(unittest.TestCase):
    def test_metrics(self):
        rows = [row("failure", "failure", "spaghetti"), row("failure", "ok", "detached_part"),
                row("failure", "warning", "spaghetti"), row("ok", "failure"), row("ok", "ok"), row("ok", "ok")]
        m = confusion(rows, {"failure"})
        self.assertEqual((m["tp"], m["fn"], m["fp"], m["tn"]), (1, 2, 1, 2))
        self.assertEqual((m["recall"], m["precision"], m["false_alarm_rate"]), (0.333, 0.5, 0.333))
        self.assertEqual(m["recall_by_issue"], {"detached_part": "0/1", "spaghetti": "1/2"})
        m2 = confusion(rows, {"failure", "warning"})              # warnings count as alarms
        self.assertEqual(m2["recall"], 0.667)

    def test_empty_classes(self):
        m = confusion([row("ok", "ok")], {"failure"})
        self.assertIsNone(m["recall"])
        self.assertIsNone(m["precision"])
        self.assertEqual(m["false_alarm_rate"], 0.0)


class ReplayRecordedTest(unittest.TestCase):
    def test_recorded_variant_needs_no_api(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        ds = Dataset(root, ok_every=1)
        fail = {"status": "failure", "issue": "spaghetti", "confidence": 0.9, "description": "tangle"}
        ok = {"status": "ok", "issue": "none", "confidence": 0.9, "description": "fine"}
        ds.label(ds.save(1000, 1, b"J", None, "ctx", fail, "m"), "failure", "spaghetti")
        ds.label(ds.save(1060, 1, b"J", None, "ctx", fail, "m"), "ok")              # false alarm
        ds.label(ds.save(1120, 1, b"J", None, "ctx", ok, "m"), "failure", "detached_part")  # missed
        ds.save(1180, 1, b"J", None, "ctx", ok, "m")                                 # unlabelled: skipped

        spec = importlib.util.spec_from_file_location("replay", os.path.join(HERE, "..", "eval", "replay.py"))
        replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(replay)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            replay.run_dataset(SimpleNamespace(dataset=root, variants="recorded", config="unused",
                                               positive="failure", provider=None, model=None))
        text = out.getvalue()
        self.assertIn("3 labelled samples (2 real failures, 1 ok)", text)
        self.assertIn("recall (failures caught)  50.0%", text)
        report = [f for f in os.listdir(root) if f.startswith("eval-")]
        with open(os.path.join(root, report[0]), encoding="utf-8") as f:
            metrics = json.load(f)["metrics"]["recorded"]
        self.assertEqual((metrics["tp"], metrics["fn"], metrics["fp"], metrics["tn"]), (1, 1, 1, 0))


if __name__ == "__main__":
    unittest.main()
