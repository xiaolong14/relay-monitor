@echo off
REM 一键注册 Windows 计划任务：每天 09:00 自动运行中转站质量监控
REM 请以管理员身份运行本脚本（右键 -> 以管理员身份运行）
REM 如需修改执行时间，改下面的 /st 09:00 即可（24 小时制）

schtasks /create /tn "RelayQualityMonitor" /tr "\"%~dp0run_daily.bat\"" /sc daily /st 09:00 /f

if %errorlevel%==0 (
    echo.
    echo 计划任务创建成功！每天 09:00 将自动运行监控。
    echo 可在「任务计划程序」中搜索 RelayQualityMonitor 查看或修改。
) else (
    echo.
    echo 创建失败，请确认以管理员身份运行本脚本。
)
pause
