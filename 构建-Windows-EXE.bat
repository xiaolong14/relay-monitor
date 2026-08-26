@echo off
chcp 65001 >nul
cd /d "%~dp0"
python build_desktop.py
if errorlevel 1 (
    echo 构建失败，请查看上方信息。
) else (
    echo Windows EXE 已生成到 desktop-dist\RelayMonitor。
)
pause
