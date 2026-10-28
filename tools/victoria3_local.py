"""Local discovery and reversible Victoria 3 settings changes; no save writes."""
from __future__ import annotations

from copy import deepcopy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from uuid import uuid4

from tools.resolution import lower_resolution_same_aspect


def documents_directory() -> Path:
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
            return Path(buffer.value)
    return Path.home() / "Documents"


def game_running() -> bool:
    if os.name != "nt":
        raise ValueError("自动设置目前仅支持 Windows")
    result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq victoria3.exe", "/FO", "CSV", "/NH"],
                            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("无法检查游戏是否运行，请稍后重试")
    return b'"victoria3.exe"' in result.stdout.lower()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def encode(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


class LocalVictoria3:
    def __init__(self, user_directory: Path | None = None, backup_directory: Path | None = None,
                 running_check=game_running):
        self.directory = user_directory or documents_directory() / "Paradox Interactive/Victoria 3"
        self.settings_path = self.directory / "pdx_settings.json"
        self.backups = backup_directory or self.directory / "gametune-backups"
        self.running_check = running_check

    def discover(self) -> dict:
        settings = self.read_settings()
        log_path = self.directory / "logs/system.log"
        log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        def field(pattern: str) -> str:
            match = re.search(pattern, log)
            return match.group(1).strip() if match else "未识别"
        debug_path = self.directory / "logs/debug.log"
        debug = debug_path.read_text(encoding="utf-8", errors="replace") if debug_path.exists() else ""
        root = re.search(r"Mounted Data: (.+?)/clausewitz", debug)
        install = Path(root.group(1)) if root else None
        saves = sorted((self.directory / "save games").glob("*.v3"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
        return {"settings": settings, "directory": str(self.directory),
                "install": str(install) if install and install.is_dir() else "未识别",
                "version": field(r"Exe Git Version: (.+)"),
                "cpu": field(r"CPU Model: (.+)"), "gpu": field(r"GPU Name: (.+)"),
                "saves": saves,
                "hardware_source": "最近一次游戏启动日志，可能不是当前硬件状态"}

    def read_settings(self) -> dict:
        data = json.loads(self.settings_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("Graphics"), dict):
            raise ValueError("游戏设置缺少有效 Graphics 对象，请先正常运行一次游戏")
        return data

    def preview(self, mode: str) -> dict:
        original = self.settings_path.read_bytes()
        data = json.loads(original.decode("utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("Graphics"), dict):
            raise ValueError("无法识别游戏图形设置")
        graphics = data["Graphics"]
        resolution = graphics.get("resolution")
        if not isinstance(resolution, str):
            raise ValueError("未读取到当前分辨率，请先在游戏中保存一次显示设置")
        target = lower_resolution_same_aspect(resolution).replace(" × ", "x")
        changes = {"resolution": target}
        if mode == "performance":
            # Values verified against Victoria 3's clausewitz/compound_settings.txt.
            changes.update(quality="custom", multi_sampling="DISABLED", anisotropic_filtering="DISABLED",
                           refraction_quality="disabled", shadowmap_resolution="disabled")
        elif mode != "resolution":
            raise ValueError("未知优化方案")
        changes = {key: value for key, value in changes.items() if graphics.get(key) != value}
        return {"source_hash": hashlib.sha256(original).hexdigest(), "changes": changes,
                "before": {key: graphics.get(key) for key in changes},
                "mode": mode, "status": "proposed"}

    def _ensure_stopped(self):
        if self.running_check():
            raise ValueError("请先退出 Victoria 3，再应用或恢复设置；无需关闭启动器")

    def apply(self, plan: dict) -> Path:
        self._ensure_stopped()
        raw = self.settings_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != plan["source_hash"]:
            raise ValueError("设置在预览后发生变化，请重新扫描并预览")
        # Rebuild the plan from the current file; never trust caller-supplied setting keys/values.
        expected = self.preview(plan["mode"])
        if plan != expected:
            raise ValueError("方案已失效，请重新生成预览")
        original = self.read_settings()
        updated = deepcopy(original)
        updated["Graphics"].update(plan["changes"])
        self.backups.mkdir(parents=True, exist_ok=True)
        backup = self.backups / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex + ".json")
        record = {"settings_path": str(self.settings_path.resolve()), "original": original,
                  "original_sha256": hashlib.sha256(encode(original)).hexdigest(),
                  "plan": plan, "status": "backup_created"}
        atomic_write(backup, encode(record))
        self._ensure_stopped()
        if self.settings_path.read_bytes() != raw:
            raise ValueError("游戏设置发生变化，本次未应用，请重新扫描")
        atomic_write(self.settings_path, encode(updated))
        # The plan records actual written values; performance remains unmeasured.
        record["status"] = "applied"
        atomic_write(backup, encode(record))
        return backup

    def restore(self, backup: Path) -> None:
        self._ensure_stopped()
        if backup.resolve().parent != self.backups.resolve():
            raise ValueError("只能恢复本工具创建的备份")
        record = json.loads(backup.read_text(encoding="utf-8"))
        if record["settings_path"] != str(self.settings_path.resolve()):
            raise ValueError("备份不属于当前游戏目录")
        original = record["original"]
        if hashlib.sha256(encode(original)).hexdigest() != record["original_sha256"]:
            raise ValueError("备份校验失败")
        raw = self.settings_path.read_bytes()
        current = json.loads(raw.decode("utf-8-sig"))
        for key, applied in record["plan"]["changes"].items():
            old = original["Graphics"].get(key)
            if current["Graphics"].get(key) not in (applied, old):
                raise ValueError(f"{key} 已在游戏中另行修改；为避免覆盖，请重新扫描")
            if key in original["Graphics"]:
                current["Graphics"][key] = old
            else:
                current["Graphics"].pop(key, None)
        self._ensure_stopped()
        if self.settings_path.read_bytes() != raw:
            raise ValueError("恢复前设置发生变化，请重试")
        atomic_write(self.settings_path, encode(current))

    def backup_list(self) -> list[Path]:
        return sorted(self.backups.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
