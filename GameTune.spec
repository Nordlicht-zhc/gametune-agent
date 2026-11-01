# Build with: python -m PyInstaller --noconfirm GameTune.spec
from pathlib import Path
import os
import sys

root = Path(SPECPATH)
# Avoid unrelated developer tools on PATH supplying incompatible DLLs.
os.environ["PATH"] = os.pathsep.join([
    str(Path(os.environ["SystemRoot"]) / "System32"),
    os.environ["SystemRoot"],
    str(Path(sys.executable).parent),
])
analysis = Analysis(
    [str(root / "app.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(path), "knowledge") for path in sorted((root / "knowledge").glob("*.json"))],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
# Use one current MSVC runtime throughout the bundle. Both Python and the
# PySide6 wheel may carry older copies than the runtime used on this machine.
runtime_directory = Path(os.environ["SystemRoot"]) / "System32"
runtime_files = {
    path.name.lower(): path
    for pattern in ("vcruntime140*.dll", "msvcp140*.dll")
    for path in runtime_directory.glob(pattern)
    if not path.stem.lower().endswith("d") and "clr" not in path.stem.lower()
}
analysis.binaries = [
    (name, str(runtime_files.get(Path(name).name.lower(), Path(source))), kind)
    for name, source, kind in analysis.binaries
]
for name, path in runtime_files.items():
    if not any(destination.lower() == name for destination, _, _ in analysis.binaries):
        analysis.binaries.append((path.name, str(path), "BINARY"))
archive = PYZ(analysis.pure)
exe = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="GameTune",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
