@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================
echo Relay Monitor - Windows 一键安装
echo ========================================

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 goto :python_missing
    set "PYTHON=python"
)

where node >nul 2>nul
if errorlevel 1 goto :node_missing
where npm >nul 2>nul
if errorlevel 1 goto :node_missing

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 正在创建 Python 虚拟环境...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :failed
) else (
    echo [1/3] Python 虚拟环境已存在，跳过创建。
)

echo [2/3] 正在安装 Python 依赖...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo [3/3] 正在安装 promptfoo 评测引擎...
call npm install
if errorlevel 1 goto :failed

echo.
echo 安装完成。以后双击「启动-Windows.bat」即可使用。
pause
exit /b 0

:python_missing
echo 未找到 Python 3.10 或更高版本。
echo 请访问 https://www.python.org/downloads/ 安装，并勾选 Add Python to PATH。
pause
exit /b 1

:node_missing
echo 未找到 Node.js 18 或更高版本。
echo 请访问 https://nodejs.org/ 安装 LTS 版本。
pause
exit /b 1

:failed
echo.
echo 安装失败，请查看上方错误信息后重试。
pause
exit /b 1
