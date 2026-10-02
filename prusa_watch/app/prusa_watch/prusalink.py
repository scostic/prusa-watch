"""Minimal PrusaLink v1 client (Buddy firmware 6.x: MK4/MK3.5/MINI/XL/CORE One)."""
from dataclasses import dataclass
from typing import Optional

import requests
from requests.auth import HTTPDigestAuth


@dataclass
class PrinterStatus:
    state: str                      # IDLE, BUSY, PRINTING, PAUSED, FINISHED, STOPPED, ERROR, ATTENTION, READY
    job_id: Optional[int] = None
    progress: Optional[float] = None
    time_remaining: Optional[int] = None
    time_printing: Optional[int] = None
    temp_nozzle: Optional[float] = None
    target_nozzle: Optional[float] = None
    temp_bed: Optional[float] = None
    target_bed: Optional[float] = None
    axis_z: Optional[float] = None
    fan_hotend: Optional[int] = None
    fan_print: Optional[int] = None
    flow: Optional[int] = None
    speed: Optional[int] = None

    def summary(self) -> str:
        def f(v, unit="", nd=1):
            return "n/a" if v is None else f"{v:.{nd}f}{unit}" if isinstance(v, float) else f"{v}{unit}"
        return (
            f"state={self.state} progress={f(self.progress, '%', 0)} z={f(self.axis_z, 'mm', 2)} "
            f"nozzle={f(self.temp_nozzle)}/{f(self.target_nozzle)}C bed={f(self.temp_bed)}/{f(self.target_bed)}C "
            f"hotend_fan={f(self.fan_hotend, 'rpm')} print_fan={f(self.fan_print, 'rpm')} "
            f"flow={f(self.flow, '%')} speed={f(self.speed, '%')}"
        )


def parse_status(data: dict) -> PrinterStatus:
    p = data.get("printer") or {}
    j = data.get("job") or {}
    return PrinterStatus(
        state=str(p.get("state", "UNKNOWN")).upper(),
        job_id=j.get("id"),
        progress=j.get("progress"),
        time_remaining=j.get("time_remaining"),
        time_printing=j.get("time_printing"),
        temp_nozzle=p.get("temp_nozzle"),
        target_nozzle=p.get("target_nozzle"),
        temp_bed=p.get("temp_bed"),
        target_bed=p.get("target_bed"),
        axis_z=p.get("axis_z"),
        fan_hotend=p.get("fan_hotend"),
        fan_print=p.get("fan_print"),
        flow=p.get("flow"),
        speed=p.get("speed"),
    )


class PrusaLink:
    def __init__(self, host: str, username: str = "maker", password: str = "",
                 api_key: str = "", timeout: float = 10):
        self.base = host.rstrip("/") if host.startswith("http") else f"http://{host}"
        self.timeout = timeout
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-Api-Key"] = api_key
        elif password:
            self.session.auth = HTTPDigestAuth(username, password)

    def _req(self, method: str, path: str) -> requests.Response:
        r = self.session.request(method, f"{self.base}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r

    def status(self) -> PrinterStatus:
        return parse_status(self._req("GET", "/api/v1/status").json())

    def pause(self, job_id: int) -> None:
        self._req("PUT", f"/api/v1/job/{job_id}/pause")

    def stop(self, job_id: int) -> None:
        self._req("DELETE", f"/api/v1/job/{job_id}")
