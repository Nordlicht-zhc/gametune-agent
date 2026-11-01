"""Read only newly appended CS2 VProf summary lines; never execute game commands."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from pathlib import Path
import re
import os
from uuid import uuid4

FPS = re.compile(rb"\[VProf\]\s*FPS:\s*Avg\s*=\s*([0-9]+(?:\.[0-9]+)?),\s*P1\s*=\s*([0-9]+(?:\.[0-9]+)?)(?:\s|$)", re.I)
SUMMARY = re.compile(rb"\[VProf\]\s*Summary of\s+[0-9]+\s+frames[^\r\n]*", re.I)


@dataclass(frozen=True)
class LogResult:
    measurement_id: str
    avg_fps: float
    p1_fps: float
    excerpt: str


class CS2LogTail:
    def __init__(self, path: str | Path, *, follow_creation: bool = False):
        self.path = Path(path)
        self.echo_tokens: set[str] = set()
        self.expected_map: bytes | None = None
        self.map_loaded = False
        self.expected_echo: str | None = None
        self.identity = None
        self.offset = 0
        self.anchor = b""
        if self.path.exists() or not follow_creation:
            with self.path.open("rb") as stream:
                stat = stream.fileno()
                info = os.fstat(stat)
                self.identity = (info.st_dev, info.st_ino)
                stream.seek(0, 2)
                self.offset = stream.tell()
                stream.seek(max(0, self.offset - 64))
                self.anchor = stream.read(self.offset - stream.tell())
        self.epoch = uuid4().hex
        self.buffer = b""
        self.summary = b""

    def poll(self) -> list[LogResult]:
        # Bounded reads keep the GUI responsive even if a large log is appended.
        with self.path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            stream.seek(max(0, self.offset - 64))
            rewritten = info.st_size >= self.offset and stream.read(self.offset - stream.tell()) != self.anchor
            if identity != self.identity or info.st_size < self.offset or rewritten:
                self.identity = identity
                self.offset = 0
                self.buffer = b""
                self.summary = b""
                self.map_loaded = False
                self.epoch = uuid4().hex
            stream.seek(self.offset)
            chunk = stream.read(256 * 1024)
            start = self.offset - len(self.buffer)
            self.offset += len(chunk)
            stream.seek(max(0, self.offset - 64))
            self.anchor = stream.read(self.offset - stream.tell())
        content = self.buffer + chunk
        lines = content.split(b"\n")
        self.buffer = lines.pop()
        results = []
        position = start
        for line in lines:
            position += len(line) + 1
            if self.expected_map and re.search(rb'\bMap:\s*"' + re.escape(self.expected_map) + rb'"', line, re.I):
                self.map_loaded = True
            if self.expected_echo and re.fullmatch(
                    rb"(?:\[[^]\r\n]+\]\s*)*" + re.escape(self.expected_echo.encode("ascii")) + rb"\s*", line):
                self.echo_tokens.add(self.expected_echo)
            if SUMMARY.search(line):
                self.summary = SUMMARY.search(line).group(0)[:512]
            match = FPS.search(line)
            if not match:
                continue
            avg, p1 = map(float, match.groups())
            if not (isfinite(avg) and isfinite(p1) and avg > 0 and 0 < p1 <= avg * 1.05):
                continue
            # Retain only performance summary text, not unrelated console data.
            excerpt = b"\n".join(filter(None, [self.summary, match.group(0).strip()])).decode("ascii")
            token = sha256(f"{self.epoch}:{position}".encode()).hexdigest()
            results.append(LogResult(token, avg, p1, excerpt))
            self.summary = b""
        if len(self.buffer) > 8192:
            self.buffer = self.buffer[-8192:]
        return results


def steam_locations() -> tuple[list[Path], list[Path]]:
    """Discover Steam libraries without reading user account or API settings."""
    import os
    roots = []
    if os.name == "nt":
        import winreg
        for key, value in ((r"Software\Valve\Steam", "SteamPath"),):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                    roots.append(Path(winreg.QueryValueEx(handle, value)[0]))
            except OSError:
                pass
    default = os.environ.get("ProgramFiles(x86)")
    if default:
        roots.append(Path(default) / "Steam")
    libraries = list(roots)
    for root in roots:
        try:
            text = (root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8")
            libraries.extend(Path(value.replace("\\\\", "\\")) for value in
                             re.findall(r'"path"\s*"([^"\r\n]+)"', text))
        except (OSError, UnicodeError):
            pass
    return list(dict.fromkeys(roots)), list(dict.fromkeys(libraries))


def locate_console_logs() -> list[Path]:
    _, libraries = steam_locations()
    return list(dict.fromkeys(path for library in libraries
        if (path := library / "steamapps/common/Counter-Strike Global Offensive/game/csgo/console.log").is_file()))
