from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HardwareEntry:
    name: str
    vendor: str
    segment: str = "Desktop"

    @property
    def display_name(self) -> str:
        suffix = " · Laptop" if self.segment == "Laptop" and "Laptop" not in self.name else ""
        prefix = "" if self.name.casefold().startswith(self.vendor.casefold()) else f"{self.vendor} · "
        return f"{prefix}{self.name}{suffix}"


class HardwareCatalog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with self.path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        self.version = int(data.get("version", 0))
        self.cpus = tuple(HardwareEntry(**item) for item in data.get("cpus", []))
        self.gpus = tuple(HardwareEntry(**item) for item in data.get("gpus", []))
        if not self.cpus or not self.gpus:
            raise ValueError("硬件目录缺少 CPU 或 GPU 数据")

    def entries(self, kind: str) -> tuple[HardwareEntry, ...]:
        if kind == "cpu":
            return self.cpus
        if kind == "gpu":
            return self.gpus
        raise ValueError("硬件类型必须是 cpu 或 gpu")

    def find(self, kind: str, name: str) -> HardwareEntry | None:
        normalized = name.casefold().strip()
        return next((entry for entry in self.entries(kind) if entry.name.casefold() == normalized), None)
