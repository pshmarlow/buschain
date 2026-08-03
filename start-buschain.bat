@echo off
setlocal
REM Double-click launcher AND one-shot setup: the first run on a PC creates the
REM virtualenv and installs the app, every run after that just starts it. The
REM people who use this are dispatchers, not developers - the shortcut has to be
REM the whole procedure.
REM
REM Pins the working directory to this file's folder: DEFAULT_DB is a relative
REM path, so starting the server from anywhere else silently creates a second,
REM empty buschain.db instead of failing. pushd rather than "cd /d" so this
REM still works from a UNC path.
pushd "%~dp0"

REM Every check below is "if errorlevel 1", never "%ERRORLEVEL%": inside a
REM parenthesised block the latter is expanded when the block is parsed, so it
REM reads the PREVIOUS command's exit code and silently takes the wrong branch.

REM --------------------------------------------------------------- sync guard
REM The .db/-wal/-shm trio does not survive being synced as three independent
REM files, and Known Folder Move puts Desktop and Documents under sync by
REM default - exactly where a copied folder tends to land. Warn rather than
REM refuse: not being able to start at all is worse than a database we merely
REM have doubts about.
set "here=%CD%"
if not "%here:OneDrive=%"=="%here%" goto :synced
if not "%here:Dropbox=%"=="%here%" goto :synced
goto :checkvenv

:synced
echo.
echo   WARNING - this folder is inside a cloud-synced location:
echo   %here%
echo.
echo   Syncing can damage the database. Move the whole buschain folder
echo   somewhere local - C:\dev\buschain is a good choice - and start it
echo   from there instead.
echo.
timeout /t 8

:checkvenv
if exist ".venv\Scripts\python.exe" goto :deps

REM ------------------------------------------------------- first-run bootstrap
REM Only reached when there is no virtualenv yet. Both checks exist to turn a
REM confusing failure much later into a sentence someone can act on now: a
REM missing py launcher would otherwise surface as "'py' is not recognized",
REM and Python 3.10 as a pip resolution error ninety seconds into the install.
where py >nul 2>nul
if errorlevel 1 (
    echo Python is not installed on this PC, and this launcher cannot install
    echo it for you. To fix it:
    echo.
    echo   1. Go to https://www.python.org/downloads/
    echo   2. Download Python 3.11 or newer
    echo   3. In the installer, tick "Add python.exe to PATH"
    echo   4. Double-click this file again
    echo.
    pause
    popd
    exit /b 1
)

REM pyproject requires-python is >=3.11. py -3 picks the newest installed 3.x,
REM but PY_PYTHON in the environment can override that, so ask the interpreter
REM itself rather than trusting the launcher's choice.
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo The Python on this PC is too old - buschain needs 3.11 or newer.
    echo This is what is installed:
    py -3 -V
    echo.
    echo Install a newer one from https://www.python.org/downloads/ and
    echo double-click this file again.
    echo.
    pause
    popd
    exit /b 1
)

echo Setting up buschain for the first time on this PC. This takes a few
echo minutes and needs an internet connection - you only see it once.
echo.
REM Re-running this over a half-created .venv repairs it, so a bootstrap that
REM was interrupted needs no cleanup branch.
py -3 -m venv .venv
if errorlevel 1 (
    echo Could not create the .venv folder - see the messages above.
    echo.
    pause
    popd
    exit /b 1
)

:deps
REM Runs on every launch, not only after a fresh venv: a first run that died
REM part-way through pip - no network, a company proxy, antivirus - leaves a
REM .venv that exists but cannot serve, and a bare "does .venv exist" check
REM would never retry it, so every later double-click would throw the same
REM traceback. Probing the three imports the server needs in order to boot
REM costs about half a second and makes that failure fix itself. pulp is
REM deliberately not probed: the solver is optional, and a machine without it
REM must not reinstall on every launch.
.venv\Scripts\python.exe -c "import fastapi, uvicorn, openpyxl" >nul 2>nul
if errorlevel 1 (
    echo Installing buschain and its dependencies. This takes a few minutes
    echo and needs an internet connection.
    echo.
    REM Same command as the README, so there is one setup path for everyone.
    REM Double quotes because cmd.exe does not strip single ones.
    .venv\Scripts\python.exe -m pip install -e ".[dev]"
    if errorlevel 1 (
        echo.
        echo The install did not finish - see the messages above. Usually that
        echo is no internet connection, or a company proxy or antivirus in the
        echo way. Fix that and double-click this file again.
        echo.
        pause
        popd
        exit /b 1
    )
    echo.
)

title buschain - close this window to stop the server
.venv\Scripts\python.exe -m buschain.web

REM Keep the window up on a crash. Double-clicked, it would otherwise vanish
REM with the traceback still in it.
if errorlevel 1 pause
popd
