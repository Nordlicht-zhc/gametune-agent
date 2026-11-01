"""Fixed-purpose CS2 benchmark launch and foreground-only console input."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
from tools.cs2_log import steam_locations

MAP_COMMAND = "map_workshop 3240880604 de_dust2"


@dataclass(frozen=True)
class Installation:
    steam: Path
    console_log: Path


def find_installation() -> Installation:
    roots, libraries = steam_locations()
    steam = next((root / "steam.exe" for root in roots if (root / "steam.exe").is_file()), None)
    game = next((library / "steamapps/common/Counter-Strike Global Offensive" for library in libraries
                 if (library / "steamapps/common/Counter-Strike Global Offensive/game/bin/win64/cs2.exe").is_file()), None)
    if steam is None or game is None:
        raise ValueError("未找到 Steam 或 CS2 安装目录。请先通过 Steam 安装并启动过 CS2。")
    return Installation(steam, game / "game/csgo/console.log")


def launch_benchmark(installation: Installation):
    # Arguments are passed as a list; no shell, launch-option edits or game kills.
    return subprocess.Popen([str(installation.steam), "-applaunch", "730", "-condebug", "-console",
                             "+map_workshop", "3240880604", "de_dust2"],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def game_window() -> int | None:
    if os.name != "nt":
        raise ValueError("控制台宏仅支持 Windows。")
    import ctypes as c
    from ctypes import wintypes as w
    user, kernel = c.WinDLL("user32", use_last_error=True), c.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    user.IsWindowVisible.argtypes = [w.HWND]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    found = []
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    @callback_type
    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd):
            return True
        pid = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, c.byref(pid))
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if handle:
            try:
                name, size = c.create_unicode_buffer(32768), w.DWORD(32768)
                if kernel.QueryFullProcessImageNameW(handle, 0, name, c.byref(size)) and Path(name.value).name.lower() == "cs2.exe":
                    found.append(int(hwnd))
            finally:
                kernel.CloseHandle(handle)
        return True
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumWindows(visit, 0)
    if len(found) > 1:
        raise ValueError("检测到多个 CS2 窗口，请只保留一个后再试。")
    return found[0] if found else None


def _send(hwnd: int, keys: list[tuple[int, int, int]]):
    import ctypes as c
    from ctypes import wintypes as w
    user = c.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = w.HWND
    if user.GetForegroundWindow() != hwnd:
        raise ValueError("CS2 未处于前台，宏已停止；请切回游戏后重试。")
    class Keyboard(c.Structure):
        _fields_ = [("vk", w.WORD), ("scan", w.WORD), ("flags", w.DWORD), ("time", w.DWORD), ("extra", c.c_size_t)]
    class Mouse(c.Structure):
        _fields_ = [("x", w.LONG), ("y", w.LONG), ("data", w.DWORD), ("flags", w.DWORD), ("time", w.DWORD), ("extra", c.c_size_t)]
    class Union(c.Union):
        _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]
    class Input(c.Structure):
        _fields_ = [("kind", w.DWORD), ("value", Union)]
    events = (Input * len(keys))()
    for event, (vk, scan, flags) in zip(events, keys):
        event.kind = 1
        event.value.keyboard = Keyboard(vk, scan, flags, 0, 0)
    user.SendInput.argtypes = [w.UINT, c.POINTER(Input), c.c_int]
    if user.SendInput(len(events), events, c.sizeof(Input)) != len(events):
        raise ValueError("Windows 未接受按键输入；请让游戏与 GameTune 使用相同权限运行。")


def open_console(hwnd: int):
    import ctypes as c
    from ctypes import wintypes as w
    user = c.WinDLL("user32", use_last_error=True)
    user.ShowWindow.argtypes = [w.HWND, c.c_int]
    user.SetForegroundWindow.argtypes = [w.HWND]
    user.IsIconic.argtypes = [w.HWND]
    if user.IsIconic(hwnd):
        user.ShowWindow(hwnd, 9)
    user.SetForegroundWindow(hwnd)
    _send(hwnd, [(0xC0, 0, 0), (0xC0, 0, 2)])


def console_command(hwnd: int, command: str):
    import re
    if command != MAP_COMMAND and not re.fullmatch(r"echo GAMETUNE_[0-9a-f]{32}", command):
        raise ValueError("不支持的宏指令")
    # Clear the console input, type Unicode without touching the clipboard, Enter.
    keys = [(0x11, 0, 0), (0x41, 0, 0), (0x41, 0, 2), (0x11, 0, 2)]
    for char in command:
        keys.extend([(0, ord(char), 4), (0, ord(char), 6)])
    keys.extend([(0x0D, 0, 0), (0x0D, 0, 2)])
    _send(hwnd, keys)
