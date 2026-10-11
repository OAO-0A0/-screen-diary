@echo off
rem ============================================================
rem  Screen Journal launcher
rem  ASCII-only on purpose: cmd.exe parses a .bat with the console
rem  code page (936 here), so Chinese text in the file would break
rem  into garbage and be run as commands. All Chinese output comes
rem  from Python, which manages its own encoding.
rem ============================================================
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title Screen Journal

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [ERROR] python not found in PATH.
    echo   Install Python and make sure "python" works in a terminal.
    echo.
    pause
    exit /b 1
)

rem Use pythonw so no extra console window sticks around
where pythonw >nul 2>nul
if errorlevel 1 (
    python journal.py
) else (
    start "" pythonw journal.py
)
exit /b 0