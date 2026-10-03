"""Labelled sample store: frames + context + model verdict + (later) human ground truth.

Layout: <root>/<sample_id>/{current.jpg, reference.jpg?, meta.json}
meta["label"] is None until a human labels the sample:
    {"truth": "ok" | "failure", "issue": "<issue or none>", "source": "web|button", "at": iso-time}
"""
import json
import logging
import os
import shutil
import threading
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

TRUTHS = ("ok", "failure")
FLAGGED = ("warning", "failure")


class Dataset:
    def __init__(self, root: str, ok_every: int = 10, max_samples: int = 2000):
        self.root = root
        self.ok_every = max(1, ok_every)
        self.max_samples = max_samples
        self._ok_counter = 0
        self._lock = threading.Lock()

    # ---------- writing ----------
    def save(self, t: float, job_id, current: bytes, reference: Optional[bytes], context: str,
             verdict: dict, model: str, force: bool = False) -> Optional[str]:
        """Store a sample. Flagged verdicts are always kept, 'ok' ones 1 in ok_every (or when forced)."""
        flagged = verdict.get("status") in FLAGGED
        if not (flagged or force):
            self._ok_counter += 1
            if self._ok_counter % self.ok_every:
                return None
        sid = f"{datetime.fromtimestamp(t):%Y%m%d-%H%M%S}-j{job_id if job_id is not None else 'x'}"
        path = os.path.join(self.root, sid)
        try:
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "current.jpg"), "wb") as f:
                f.write(current)
            if reference:
                with open(os.path.join(path, "reference.jpg"), "wb") as f:
                    f.write(reference)
            meta = {"id": sid, "time": datetime.fromtimestamp(t).isoformat(timespec="seconds"),
                    "job_id": job_id, "context": context, "verdict": verdict, "model": model,
                    "has_reference": bool(reference), "label": None}
            self._write_meta(sid, meta)
        except OSError as e:
            log.warning("Could not save sample %s: %s", sid, e)
            return None
        self._prune()
        return sid

    def label(self, sid: str, truth: str, issue: str = "none", source: str = "web") -> dict:
        if truth not in TRUTHS:
            raise ValueError(f"truth must be one of {TRUTHS}")
        with self._lock:
            meta = self.get(sid)
            if meta is None:
                raise KeyError(sid)
            meta["label"] = {"truth": truth, "issue": issue if truth == "failure" else "none",
                             "source": source, "at": datetime.now().isoformat(timespec="seconds")}
            self._write_meta(sid, meta)
        log.info("Labelled %s as %s/%s (%s)", sid, truth, meta["label"]["issue"], source)
        return meta

    # ---------- reading ----------
    def ids(self) -> list[str]:
        try:
            return sorted((d for d in os.listdir(self.root)
                           if os.path.isfile(os.path.join(self.root, d, "meta.json"))), reverse=True)
        except FileNotFoundError:
            return []

    def get(self, sid: str) -> Optional[dict]:
        if not self._safe(sid):
            return None
        try:
            with open(os.path.join(self.root, sid, "meta.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def image_path(self, sid: str, which: str) -> Optional[str]:
        if not self._safe(sid) or which not in ("current", "reference"):
            return None
        p = os.path.join(self.root, sid, f"{which}.jpg")
        return p if os.path.isfile(p) else None

    def samples(self, filt: str = "unlabelled", limit: int = 200) -> list[dict]:
        out = []
        for sid in self.ids():
            m = self.get(sid)
            if m is None:
                continue
            flagged = m["verdict"].get("status") in FLAGGED
            if (filt == "unlabelled" and m["label"] is not None) or \
               (filt == "flagged" and not flagged) or \
               (filt == "labelled" and m["label"] is None):
                continue
            out.append(m)
            if len(out) >= limit:
                break
        return out

    def latest(self, flagged_only: bool) -> Optional[dict]:
        for sid in self.ids():
            m = self.get(sid)
            if m and (not flagged_only or m["verdict"].get("status") in FLAGGED):
                return m
        return None

    def stats(self) -> dict:
        s = {"samples": 0, "flagged": 0, "labelled": 0, "truth_failure": 0}
        for sid in self.ids():
            m = self.get(sid)
            if not m:
                continue
            s["samples"] += 1
            s["flagged"] += m["verdict"].get("status") in FLAGGED
            if m["label"]:
                s["labelled"] += 1
                s["truth_failure"] += m["label"]["truth"] == "failure"
        return s

    # ---------- internals ----------
    def _safe(self, sid: str) -> bool:
        return bool(sid) and "/" not in sid and "\\" not in sid and ".." not in sid

    def _write_meta(self, sid: str, meta: dict) -> None:
        path = os.path.join(self.root, sid, "meta.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)
        os.replace(tmp, path)

    def _prune(self) -> None:
        """Keep at most max_samples: drop the oldest unlabelled 'ok' samples first, then oldest unlabelled."""
        ids = self.ids()
        excess = len(ids) - self.max_samples
        if excess <= 0:
            return
        oldest_first = list(reversed(ids))
        metas = [(sid, self.get(sid)) for sid in oldest_first]
        order = [sid for sid, m in metas if m and m["label"] is None and m["verdict"].get("status") not in FLAGGED] + \
                [sid for sid, m in metas if m and m["label"] is None and m["verdict"].get("status") in FLAGGED]
        for sid in order[:excess]:
            shutil.rmtree(os.path.join(self.root, sid), ignore_errors=True)
