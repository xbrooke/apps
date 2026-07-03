@echo off
REM ============================================================
REM run_sync.bat
REM
REM DBStore 自动同步脚本（Windows 任务计划版）
REM 每 6 小时跑一次 sync_openlist_to_apps_json.py，把 OpenList 上的 APK 列表
REM 同步到 apps.json / apps_remote.json
REM
REM 用法（命令行直接跑，看效果）:
REM   cd C:\Users\Santali\Desktop\apps
REM   run_sync.bat
REM
REM 加入 Windows 任务计划（每天 03:00 跑一次）:
REM   schtasks /Create /SC DAILY /TN "DBStore-Sync" /TR "C:\Users\Santali\Desktop\apps\run_sync.bat" /ST 03:00
REM
REM 卸载:
REM   schtasks /Delete /TN "DBStore-Sync" /F
REM ============================================================
setlocal

REM 切到本脚本所在目录
cd /d "%~dp0"

REM 选 python
where py >nul 2>nul && (set PY=py) || (set PY=python)

echo [%date% %time%] === DBStore sync start ===  >> sync.log
%PY% sync_openlist_to_apps_json.py --url-mode openlist >> sync.log 2>&1
echo [%date% %time%] === DBStore sync end (rc=%ERRORLEVEL%) === >> sync.log
echo.

REM 保留最近 30 天日志
forfiles /p "%~dp0" /m sync.log.* /d -30 /c "cmd /c del @path" >nul 2>&1

endlocal
