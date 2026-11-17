@echo off
title AnkaShield
rem Opens the AnkaShield UI in your browser on the NETWORK link (this PC's LAN address + a secret key), not localhost.
rem Keep this window open while you use it; close it (or press Quit in the page) to stop.
rem
rem   run_daily.bat            open the UI on the network link (always port 8765: an old window is closed first)
rem   run_daily.bat local      open the UI on this PC only (127.0.0.1)
rem   run_daily.bat daily      no UI: mask the newest audit result in OneDrive
rem   run_daily.bat doctor     check everything AnkaShield needs and print what is wrong
rem   run_daily.bat "<file>"   no UI: mask one workbook
cd /d "%~dp0"

set "RUN="
where python >nul 2>nul && set "RUN=python ar_audit.py"
if not defined RUN (
    echo Python was not found. Install Python 3.12.
    pause
    exit /b 1
)

if "%~1"=="" goto :ui
if /i "%~1"=="local" ( %RUN% ui & goto :end )
if /i "%~1"=="doctor" ( %RUN% doctor & pause & goto :end )
if /i "%~1"=="daily" ( %RUN% daily & goto :end )

%RUN% mask "%~1" || goto :fail
goto :end

:ui
rem An earlier AnkaShield window on port 8765 would push this one to another port (and another link): close it first.
rem (found by program name: Windows does not report the owner of a port that was opened for exclusive use)
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'AR-Audit.exe' -or ($_.Name -eq 'python.exe' -and $_.CommandLine -match 'ar_audit\.py\s+ui') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; 'Closed an earlier AnkaShield window.' }"
%RUN% ui --network
goto :end

:fail
echo.
echo Masking failed.

:end
if errorlevel 1 pause
