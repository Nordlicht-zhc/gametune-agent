@echo off
setlocal
cd /d "%~dp0"
python -m PyInstaller --noconfirm --distpath . --workpath .qa\build GameTune.spec
if errorlevel 1 (
    echo Build failed. Install build dependencies: python -m pip install -r requirements-build.txt
    pause
    exit /b 1
)
echo Built: %~dp0GameTune.exe
pause
