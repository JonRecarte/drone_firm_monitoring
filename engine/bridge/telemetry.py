"""Optional JSONL telemetry tap for orchestration / probe tools.

Enable with:  --telemetry /path/to/file.jsonl
One object per bridge control cycle (and optional Renode cpu_stats samples).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, TextIO


@dataclass
class CycleSample:
    t_wall: float
    sim: float
    alt: float
    vz: float
    thrust: float
    u: float
    motors: list[float]
    omega: list[float]
    gyro: list[float]
    accel: list[float]
    pressure: float
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        extra = d.pop("extra", {}) or {}
        d.update(extra)
        return d


class TelemetryWriter:
    def __init__(self, path: str):
        self.path = path
        self._fh: TextIO = open(path, "a", buffering=1)
        self._n = 0

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def write_cycle(self, sample: CycleSample) -> None:
        self._fh.write(json.dumps(sample.to_dict(), separators=(",", ":")) + "\n")
        self._n += 1

    def write_raw(self, kind: str, **fields: Any) -> None:
        obj = {"t_wall": time.time(), "kind": kind, **fields}
        self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self._n += 1

    @property
    def count(self) -> int:
        return self._n
