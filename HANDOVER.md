# AnkaShield (PHI Redactor): handover

Last updated 2026-11-17. Pull request: Mick1e2260/Notes-disposition#1, branch `masking-ui-and-exe`.

## 1. What it is

AnkaShield masks patient data in Excel workbooks on one Windows PC. It runs a small web page on this PC, served on the office network. Nothing is sent to any cloud service. It works on any sheet, not only audit results. It only masks: the local LLM review that existed earlier has been removed.

## 2. Folder layout

```
C:\AI_Agents\AnkaShield\
  run_daily.bat        launcher (forwards to AnkaSHIELD\run_daily.bat)
  AnkaSHIELD\          the project (git repository)
  masked_out\          masked results (default output folder)
  anka-demo-video\     Remotion demo video project (own git repo, not committed)
```

Inside `AnkaSHIELD\`:

| File | Job |
| --- | --- |
| `run_daily.bat` | Starts the page on the network link. Closes any earlier AnkaShield window first. |
| `ar_audit.py` | Entry point: `ui`, `daily`, `mask`, `doctor`. |
| `webui.py` | The page and its API (standard-library `http.server`). Upload, tagging, run, progress, download, restore. |
| `daily_mask.py` | The masking engine: `process()` writes the masked workbook, quarantine file, REID key and run log. |
| `phi_scrub.py`, `phiscan.py`, `presidio_layer.py` | Masking rules for free text, the "No PHI column still holds PHI" scan, optional Presidio + NER. |
| `headermem.py`, `mask_columns_logic.py` | Saved tags per header and per file type, and the first guess for each header. |
| `reidentify.py` | Restore: turns tokens and stars back into originals. |
| `doctor.py` | Checks libraries, folders, port and network, and prints what is wrong. |
| `ar_auto_audit.py` | Separate rules tool for audit results. Nothing imports it. Delete it if it is not needed. |
| `CONTEXT.md` | Longer design notes (older, partly out of date, see section 9). |

## 3. How to run

Double-click `C:\AI_Agents\AnkaShield\run_daily.bat`. It prints the link and opens it. The link is always `http://<this PC's IP>:8765/` (today `http://10.10.140.163:8765/`). Opening `127.0.0.1` redirects to it. Keep the window open while people use it.

| Command | Result |
| --- | --- |
| `run_daily.bat` | Network UI. |
| `run_daily.bat local` | UI on this PC only. |
| `run_daily.bat daily` | No UI: masks the newest `AR Audit Result*.xlsx` in the source folder. |
| `run_daily.bat doctor` | Health check. |
| `run_daily.bat "<file>"` | No UI: masks one workbook. |

Needs Python 3.12 and `pip install -r requirements.txt`, then `python -m spacy download en_core_web_sm`.

## 4. The tags

| Tag | Use it for | Result |
| --- | --- | --- |
| No PHI | Amounts, payer, status | Kept as is. |
| PHI | Names, date of birth | First and last character kept, the rest starred. |
| Identifier | Claim number, MRN | Stable token like `[CLM-7F3A9C]`, restorable exactly. |
| Text | Notes | Names and sensitive numbers inside the sentence are masked. |
| Drop | Columns not needed | Removed. |

The Date tag was removed. Saved configs that still say Date are read as PHI. Tags are remembered per header and per file type.

## 5. What a run produces

In the output folder (`masked_out` by default):

- `<name> - MASKED.xlsx`: every sheet masked. For each sheet with Identifier columns there is also `<sheet> (Originals)`, which repeats the rows with the real value in an `(Original)` column next to each token. A `PHI Warnings` sheet is added when a No PHI column looks sensitive.
- `<name> - QUARANTINE.xlsx`: rows that still look risky. Never offered for download.

In `%LOCALAPPDATA%\AnkaARAudit\private`: the salt, `ui_key.txt`, `run_log.txt` and one `REID KEY` workbook per run. Saved tags are in `%LOCALAPPDATA%\AnkaARAudit\config`.

The uploaded workbook is held in memory only. The page shows a progress bar and an estimate of the time left while it runs.

## 6. Settings

Environment variables override the defaults: `AR_AUDIT_SOURCE_DIR`, `AR_AUDIT_MASKED_DIR`, `AR_AUDIT_PRIVATE_DIR`, `AR_AUDIT_CONFIG_DIR`.

The default source folder is still `C:\Users\mick.harper\OneDrive - Anka` (hard-coded in `daily_mask.py`). The default output folder is now `masked_out` next to the project folder.

## 7. Security notes: read before sharing

- The link has **no key**. Anyone on the private network who has it can mask files. The server refuses non-private addresses.
- Restore only works from the PC that runs AnkaShield, because the keys stay there.
- The masked workbook **contains real identifiers** on its `(Originals)` sheet. Share it only with people allowed to see them.
- The REID key files, run log and quarantine files hold real data. Keep them on this PC.
- Tested on synthetic data only.

## 8. Open items

1. **Merge the pull request** (state when this was written: open, no conflicts).
2. **Old output folder.** Until today the output default pointed to `C:\AI_Agents\Notes-Disposition-Automation\masked_out`. Runs between the rename and this fix wrote files there (AR Audit Result, OJT logic, TargetMain, WorkedClaimsReport). Move them into `C:\AI_Agents\AnkaShield\masked_out` and delete the old folder. Two of the AR Audit Result files have the same names as older ones, so check before overwriting. The `WorkedClaimsReport ... - MASKED.xlsx` there is 0 bytes (a failed run).
3. **Daily scheduled run** is not set up.
4. **Other report types** (Worked Claims, Aged AR / WFM) are tagged by uploading the first file of each type and saving tags once.
5. **Per-user default folders.** The source folder is tied to one user.
6. **Azure.** Would need sign-in (Entra ID), a private network, persistent and secured storage for the salt and key files, changes to the Windows-only code (`os.startfile`, `SO_EXCLUSIVEADDRUSE`, `%LOCALAPPDATA%`), a decision on Restore, and a Microsoft BAA.
7. **Demo video and guide** still mention the removed LLM ("Review stays on the PC" scene, "Mask and review" screenshot and tagline, guide section 5). `anka-demo-video\out\anka-demo.mp4` is the old render. Nothing in that folder is committed.
8. **`CONTEXT.md`** still describes the older audit tooling and rules in places.
9. The exe build was removed. A new one could be built with PyInstaller, but nothing in the repo does that now.

## 9. Git

Repository: `kumareshgoswami-lgtm/Notes-disposition` (GitHub redirects to `Mick1e2260/Notes-disposition`). Work branch: `masking-ui-and-exe`. The history includes the header-tag redesign, the network link, the AnkaShield rename, the Originals sheet, the LLM removal and the progress screen.
