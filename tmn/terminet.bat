@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "STAMP=%SCRIPT_DIR%.terminet_deps_ok"

:: Detect Python launcher
set "PY=py"
%PY% --version >nul 2>&1 || set "PY=python"

:: Check Python is available
%PY% --version >nul 2>&1 || (
    echo.
    echo   [ERR] Python not found.
    echo   Install Python 3.8+ from https://python.org
    echo.
    pause
    exit /b 1
)

:: Only install deps if stamp file is missing or import fails
if not exist "%STAMP%" goto :install

%PY% -c "import requests" >nul 2>&1
if errorlevel 1 goto :install

goto :run

:install
echo.
echo   [INFO] Installing / verifying Terminet dependencies ...
echo.
%PY% -m pip install --upgrade pip
%PY% -m pip install requests
if errorlevel 1 (
    echo.
    echo   [ERR] pip install failed.
    pause
    exit /b 1
)

%PY% -m pip install bcrypt 2>nul

echo ok > "%STAMP%"
echo   [OK] Dependencies ready.
echo.

:run
%PY% "%SCRIPT_DIR%terminet.py" %*