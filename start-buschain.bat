@echo off
REM Double-click launcher. Pins the working directory to this file's folder:
REM DEFAULT_DB is a relative path, so starting the server from anywhere else
REM silently creates a second, empty buschain.db instead of failing. pushd
REM rather than "cd /d" so this still works from a UNC path.
pushd "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo No .venv here yet - run the setup steps in README.md first.
    echo.
    pause
    popd
    exit /b 1
)

title buschain - close this window to stop the server
.venv\Scripts\python.exe -m buschain.web

REM Keep the window up on a crash. Double-clicked, it would otherwise vanish
REM with the traceback still in it.
if errorlevel 1 pause
popd
