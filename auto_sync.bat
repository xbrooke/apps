@echo off
REM ============================================================
REM auto_sync.bat
REM
REM DBStore 全自动同步脚本
REM 1) 调用 sync_openlist_to_apps_json.py 拉取 OpenList 上的 APK
REM 2) 自动 git add / commit / push
REM 3) Netlify 监听 main 分支自动部署，用户看到新 apps.json
REM
REM 手动跑（看效果）:
REM   cd C:\Users\Santali\Desktop\apps
REM   auto_sync.bat
REM
REM 加 Windows 任务计划（每 6 小时跑一次）:
REM   schtasks /Create /SC HOURLY /MO 6 /TN "DBStore-AutoSync" ^
REM     /TR "C:\Users\Santali\Desktop\apps\auto_sync.bat" /ST 00:00
REM
REM 卸载:
REM   schtasks /Delete /TN "DBStore-AutoSync" /F
REM ============================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"

REM 选 python
where py >nul 2>nul && (set PY=py) || (set PY=python)

set LOGFILE=%~dp0sync.log
set TS=%date% %time%

echo [!TS!] === DBStore auto_sync start ===  >> "%LOGFILE%"

REM --- step 1: 同步 OpenList 到 apps.json ---
echo [!TS!] [1/3] sync_openlist_to_apps_json.py ... >> "%LOGFILE%"
%PY% sync_openlist_to_apps_json.py --url-mode openlist >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo [!TS!] [ERR] sync failed, abort.  >> "%LOGFILE%"
    type "%LOGFILE%"
    exit /b 1
)
echo [!TS!] [1/3] sync done.  >> "%LOGFILE%"

REM --- step 2: 镜像到 apps_remote.json ---
copy /Y apps.json apps_remote.json >nul 2>&1
echo [!TS!] [2/3] mirrored to apps_remote.json  >> "%LOGFILE%"

REM --- step 3: git add / commit / push ---
echo [!TS!] [3/3] git commit + push ... >> "%LOGFILE%"

REM 跳过凭据文件，确保不入仓
git update-index --assume-unchanged openlist.config.json 2>nul

git add apps.json apps_remote.json netlify.toml run_sync.bat auto_sync.bat 2>>"%LOGFILE%"

REM 看是否有变化
git diff --cached --quiet
if errorlevel 1 (
    REM 有变化 → commit + push
    git commit -m "chore(sync): auto-update apps.json (%TS%)" >> "%LOGFILE%" 2>&1
    if errorlevel 1 (
        echo [!TS!] [ERR] git commit failed  >> "%LOGFILE%"
        type "%LOGFILE%"
        exit /b 2
    )
    git push origin main >> "%LOGFILE%" 2>&1
    if errorlevel 1 (
        echo [!TS!] [ERR] git push failed  >> "%LOGFILE%"
        type "%LOGFILE%"
        exit /b 3
    )
    echo [!TS!] [3/3] pushed to origin/main, Netlify will redeploy  >> "%LOGFILE%"
) else (
    echo [!TS!] [3/3] no changes, skip commit  >> "%LOGFILE%"
)

echo [!TS!] === DBStore auto_sync end (ok) === >> "%LOGFILE%"

REM 保留最近 30 天日志
forfiles /p "%~dp0" /m sync.log /d -30 /c "cmd /c del @path" >nul 2>&1

type "%LOGFILE%"
endlocal
