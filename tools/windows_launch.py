"""Explicit, user-triggered Windows elevation; never changes account permissions."""
import ctypes
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


def is_admin() -> bool:
    return os.name == 'nt' and bool(ctypes.windll.shell32.IsUserAnAdmin())


def elevation_arguments():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.find_spec('PySide6')
    paths = [str(root)]
    if spec and spec.origin:
        paths.append(str(Path(spec.origin).parent.parent))
    # ShellExecute may not preserve PYTHONPATH, so include the already-used Qt runtime.
    code = ('import sys; sys.path[:0]=' + repr(paths) + '; '
            "sys.argv=['victoria3_app.py','--page','2']; "
            'from victoria3_app import main; raise SystemExit(main())')
    return subprocess.list2cmdline(['-c', code])


def restart_as_admin():
    if os.name != 'nt':
        raise ValueError('管理员启动仅支持 Windows')
    root = Path(__file__).resolve().parent.parent
    result = ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable,
                                               elevation_arguments(), str(root), 1)
    if result <= 32:
        raise ValueError('管理员启动未完成或已取消。当前窗口与数据保留不变。')
