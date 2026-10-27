@echo off
title AR Audit
rem Opens the AR Audit UI in your browser on the NETWORK link (this PC's LAN address + a secret key), not localhost.
rem Keep this window open while you use it; close it (or press Quit in the page) to stop.
rem
rem   run_daily.bat            open the UI on the network link
rem   run_daily.bat local      open the UI on this PC only (127.0.0.1)
rem   run_daily.bat daily      no UI: mask the newest audit result in OneDrive, then run the local-Qwen review
rem   run_daily.bat mark       column-marking window
rem   run_daily.bat doctor     check everything AR Audit needs and print what is wrong
rem   run_daily.bat "<file>"   no UI: mask one workbook, then classify it
cd /d "%~dp0"

rem Prefer the Python source (always current); fall back to the built exe.
set "RUN="
where python >nul 2>nul && set "RUN=python ar_audit.py"
if not defined RUN if exist "%~dp0..\dist\AR-Audit\AR-Audit.exe" set "RUN="%~dp0..\dist\AR-Audit\AR-Audit.exe""
if not defined RUN (
    echo Python was not found and AR-Audit.exe is not built. Install Python 3.12 or build the exe.
    pause
    exit /b 1
)

if "%~1"=="" goto :ui
if /i "%~1"=="local" ( %RUN% ui & goto :end )
if /i "%~1"=="mark" ( %RUN% mark & goto :end )
if /i "%~1"=="doctor" ( %RUN% doctor & pause & goto :end )
if /i "%~1"=="daily" ( powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_daily.ps1" & goto :end )

%RUN% mask "%~1" || goto :fail
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Ollama"
%RUN% classify
goto :end

:ui
%RUN% ui --network
goto :end

:fail
echo.
echo Masking failed. Nothing was sent to the LLM.

:end
if errorlevel 1 pause
