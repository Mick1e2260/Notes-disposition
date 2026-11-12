@echo off
rem Builds the exe with the browser UI: ..\dist\AR-Audit\AR-Audit.exe. Needs: pip install pyinstaller
rem   build_exe.bat              build into ..\dist (close AR Audit first: its files cannot be replaced while it runs)
rem   build_exe.bat <folder>     build into <folder>\AR-Audit instead (use this to stage a build next to a running one)
rem Your saved tags and header memory are not in that folder (they live in %LOCALAPPDATA%\AnkaARAudit\config).
cd /d "%~dp0"

set "OUT=%~1"
if "%OUT%"=="" (
    set "OUT=..\dist"
    tasklist /fi "imagename eq AR-Audit.exe" | find /i "AR-Audit.exe" >nul && (
        echo AR-Audit.exe is running. Close it ^(or press Quit in the page^), then run this again.
        pause
        exit /b 1
    )
)

rem Packages that are installed on this PC but not used by AR Audit are left out: this keeps the exe small.
python -m PyInstaller ar_audit.py --noconfirm --name AR-Audit --onedir --console ^
  --distpath "%OUT%" --workpath "%TEMP%\ar_build\work" --specpath "%TEMP%\ar_build" ^
  --collect-all presidio_analyzer --collect-all en_core_web_sm --collect-data spacy --collect-submodules spacy ^
  --exclude-module tkinter --exclude-module matplotlib --exclude-module torch --exclude-module tensorflow ^
  --exclude-module playwright --exclude-module jupyterlab --exclude-module notebook --exclude-module IPython ^
  --exclude-module jedi --exclude-module parso --exclude-module scipy --exclude-module pyarrow --exclude-module babel ^
  --exclude-module PIL --exclude-module pytest --exclude-module sphinx --exclude-module docutils
if errorlevel 1 ( if "%~1"=="" pause
  exit /b 1 )

copy /y llm_context.md "%OUT%\AR-Audit\" >nul
echo.
echo Built %OUT%\AR-Audit\AR-Audit.exe
if "%~1"=="" pause
