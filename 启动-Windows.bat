@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 尚未完成安装，请先双击「安装依赖-Windows.bat」。
    pause
    exit /b 1
)

if not exist "node_modules\promptfoo" (
    echo 未找到 promptfoo，请先双击「安装依赖-Windows.bat」。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" launcher.py
if errorlevel 1 (
    echo.
    echo 启动失败。若提示端口 8787 被占用，请关闭已运行的 Relay Monitor 后重试。
    pause
)
