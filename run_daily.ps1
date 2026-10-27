# Daily pipeline: 1) mask PHI (regex + Presidio + NER; columns per column_profiles.json / built-in sheet types),
# 2) local-Ollama (Qwen) review of the MASKED file(s).
# First time on a new report type, mark its PHI columns once:  python mask_columns.py
# Schedule (weekdays 08:00), run once in PowerShell:
#   schtasks /Create /TN "AR Audit PHI Mask" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:00 /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\AI_Agents\Notes-Disposition-Automation\Notes-disposition\run_daily.ps1"
$env:Path += ";$env:LOCALAPPDATA\Programs\Ollama"
Set-Location $PSScriptRoot
python daily_mask.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }   # never review if masking failed
try { Invoke-WebRequest http://127.0.0.1:11434/api/tags -UseBasicParsing -TimeoutSec 3 | Out-Null }
catch { Start-Process ollama -ArgumentList serve -WindowStyle Hidden; Start-Sleep 8 }
python llm_audit.py       # audit-result workbook: note vs disposition review
# Open AR inventory workbook (pass the file): python daily_mask.py "<Open AR.xlsx>"; python llm_classify.py
