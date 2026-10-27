@echo off
rem Daily AR audit: mask PHI (regex + Presidio + NER) then local Qwen review. Double-click to run, or schedule it.
rem Log (counts only, no PHI) goes to %LOCALAPPDATA%\AnkaARAudit\private\run_log.txt
rem
rem   run_daily.bat            full pipeline on the newest "AR Audit Result*.xlsx" in OneDrive
rem   run_daily.bat mark       open the column-marking window (choose which columns hold PHI)
rem   run_daily.bat "<file>"   mask one specific workbook (e.g. Open AR), then classify it
cd /d "%~dp0"

if /i "%~1"=="mark" (
    python mask_columns.py
    exit /b %errorlevel%
)

if not "%~1"=="" (
    python daily_mask.py "%~1" || goto :fail
    call :ollama
    python llm_classify.py
    goto :done
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_daily.ps1" || goto :fail
goto :done

:ollama
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Ollama"
powershell -NoProfile -Command "try { Invoke-WebRequest http://127.0.0.1:11434/api/tags -UseBasicParsing -TimeoutSec 3 | Out-Null } catch { Start-Process ollama -ArgumentList serve -WindowStyle Hidden; Start-Sleep 8 }"
exit /b 0

:fail
echo.
echo Masking failed. Nothing was sent to the LLM.
pause
exit /b 1

:done
echo.
echo Done. Masked files are in C:\AI_Agents\Notes-Disposition-Automation\masked_out
pause
