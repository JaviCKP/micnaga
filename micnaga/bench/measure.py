"""Medicion de recursos durante una ejecucion: tiempo, CPU y pico de RAM (``psutil``)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any

SAMPLE_INTERVAL_S = 0.1


@dataclass(frozen=True)
class Resources:
    wall_s: float
    cpu_s: float
    peak_rss_mb: float
    cpu_percent_machine: float
    """CPU consumida / (tiempo * nucleos logicos)."""

    def to_dict(self) -> dict[str, float]:
        return {
            "wall_s": self.wall_s,
            "cpu_s": self.cpu_s,
            "peak_rss_mb": self.peak_rss_mb,
            "cpu_percent_machine": self.cpu_percent_machine,
        }


class ResourceMonitor:
    """``with ResourceMonitor() as m: ...`` y despues ``m.result``."""

    def __init__(self, interval_s: float = SAMPLE_INTERVAL_S) -> None:
        import psutil

        self._proc = psutil.Process()
        self._cores = psutil.cpu_count(logical=True) or 1
        self._interval = interval_s
        self._stop = threading.Event()
        self._peak = 0
        self._thread: threading.Thread | None = None
        self._t0 = 0.0
        self._cpu0 = 0.0
        self.result: Resources | None = None

    def _cpu(self) -> float:
        times: Any = self._proc.cpu_times()
        return float(times.user + times.system)

    def _sample(self) -> None:
        while not self._stop.is_set():
            self._peak = max(self._peak, int(self._proc.memory_info().rss))
            self._stop.wait(self._interval)

    def __enter__(self) -> ResourceMonitor:
        self._t0 = time.perf_counter()
        self._cpu0 = self._cpu()
        self._peak = int(self._proc.memory_info().rss)
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)
        wall = time.perf_counter() - self._t0
        cpu = self._cpu() - self._cpu0
        self._peak = max(self._peak, int(self._proc.memory_info().rss))
        self.result = Resources(
            wall_s=wall,
            cpu_s=cpu,
            peak_rss_mb=self._peak / (1024 * 1024),
            cpu_percent_machine=100.0 * cpu / (wall * self._cores) if wall > 0 else 0.0,
        )


def machine_info() -> dict[str, Any]:
    import platform

    import psutil

    battery = psutil.sensors_battery()
    return {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_logical": psutil.cpu_count(logical=True),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "on_battery": None if battery is None else not battery.power_plugged,
    }
