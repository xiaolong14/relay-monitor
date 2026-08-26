@echo off
REM 中转站质量监控 - 每日执行入口
REM 由 Windows 任务计划程序调用，也可手动双击运行
cd /d "%~dp0"
if not exist logs mkdir logs
echo [%date% %time%] 开始监控 >> logs\monitor.log

REM 优先使用 python，找不到时回退到 py 启动器
where python >nul 2>nul
if %errorlevel%==0 (
    python monitor.py >> logs\monitor.log 2>&1
) else (
    py monitor.py >> logs\monitor.log 2>&1
)

echo [%date% %time%] 监控结束 >> logs\monitor.log
