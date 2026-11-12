# AR Auto-Audit: Notes vs Disposition (Status / RCA / Resolution Action)

Daily automated quality audit for Anka's offshore AR team. It checks each worked claim's
**Claim Status, Denial/Rejection Category, RCA and Resolution Action** against the
client rule workbook and against what the agent actually wrote in the **notes**.

Owner: Mick Harper (Anka Health). Built 26-Oct-2026 on the 23-Oct-2026 work dump.

---

## 1. Inputs

One Excel workbook (`Note vs Disposition-Status-RCA-Resolution.xlsx`, kept in OneDrive and **never committed**):

| Sheet | Role |
|---|---|
| 1st sheet (`AR_Work_Dump`) | One day of worked claims (≈1,900 rows, 40 cols). **Replace daily.** |
| `1. Overview` | Which form the agent sees; which fields are mandatory per form |
| `2. Claim Status - RCA` | Allowed RCA list per Claim Status / Denial Category / Rejection Category |
| `3. Resolution Actions` | The 18 AR actions, 3 Coding actions, which queue completes each, parked vs closed |
| `Final Status Action Matrix` | Y/N grid: which Resolution Action is allowed for which Claim Status |
| `action definitions` | Definition, use-when / do-not-use-when, and "notes must capture" per action |

Key dump columns: `Claim Status`, `Category` (denial category), `RCA`, `Rejection Category`,
`Last Resolution Action`, `From/To/Current Queue`, `Status Source`, `Representative Name`,
`Escalation Reason`, `Notes (PHI Safe` (sic: the header is truncated in the source), `Agent`, `Emp ID`, `Balance Amount`.

The rule tabs are **parsed at runtime**, so editing the grid or RCA lists changes the audit without code changes.

---

## 2. Pipeline

```
Daily dump (OneDrive, contains PHI)
   │
   ├─► ar_auto_audit.py ── rules engine, runs fully local ──► AR Audit Result.xlsx (contains PHI; stays local)
   │
   └─► AR Audit UI (run_daily.bat / AR-Audit.exe) ── mask ──► MASKED.xlsx   (the only file allowed to reach any AI model)
                                                         ├─► QUARANTINE.xlsx (rows still risky; never sent, never offered for download)
                                                         └─► REID KEY.xlsx   (private folder; restores the originals)
                                                                │
                                                  local Ollama (Qwen): review / classify the MASKED file
```

### Run
```bash
python ar_auto_audit.py "<workbook.xlsx>" ["<output.xlsx>"]    # the audit (this section's rules engine)
run_daily.bat                                                   # the UI: add a workbook, tag headers, mask, review, download, restore
```
Requires Python 3.12 and `requirements.txt`. `build_exe.bat` builds the same UI as `dist\AR-Audit\AR-Audit.exe`.

---

## 3. Claim status: derived, not read

The `Claim Status` column in the dump is **stale** and is **ignored** for scoring (it's kept in the
output for reference only). `derive_status()` works out each claim's status in this order:

1. Rejection Category / Rejection Reason filled → **Rejected** (basis: "Rejection fields")
2. Keyword match in the note, first hit wins: Not on File → Rejected → Claim Edit/Scrubber → Pending - Info Requested →
   Denied → Scheduled to Pay → Paid → In Process → Acknowledged/Received (basis: "Note text").
   If CARC or Denial Category is filled, a Paid/In-Process-type note still resolves to **Denied** ("Denial fields + note").
3. CARC or Denial Category filled with no note signal → **Denied** ("Denial fields")
4. Otherwise **Undetermined**. The grid check (G01) is skipped for these claims.

F01 (Claim Status blank) and N02 (note vs Claim Status) were removed. F05 is fatal only when the
Rejected status comes from the rejection fields; a note-only Rejected status scores it non-fatal (verify first).
Output tab **Derived Status** shows how many claims came from each basis.

## 4. Audit scoring convention

- Every claim is audited (100% population, not a sample).
- **Fatal** = the error changes the financial or claim outcome (wrong action or status, a recovery route lost, duplicate-denial risk, a mandatory RCA missing on a rejection).
- **Non-fatal** = gaps in fields or note hygiene.
- Fatal is **binary per claim**.
  - **Critical accuracy** = (claims − fatal claims) / claims
  - **Overall accuracy** = (claims − claims with any defect) / claims
- Agent zones by critical accuracy: Green ≥98, Watch 95–98, Amber 90–95, Red <90.
- N-codes come from keyword inference over the note text. **A QA person confirms them before agent feedback.**

### Defect codes

| Code | Severity | Check |
|---|---|---|
| F02 | Non-fatal | Status Source blank |
| F03 | Non-fatal | Denied without Denial Category |
| F04 | Non-fatal | Rejected without Rejection Category |
| F05 | **Fatal** (non-fatal if status is note-derived) | Rejected without RCA (mandatory on Rejection) |
| F06 | Non-fatal | Status from Payer Call/IVR but Representative Name blank |
| F07 | Non-fatal | Escalated without Escalation Reason |
| F08 | **Fatal** | Coding-queue claim closed with a non-coding action |
| F09 | **Fatal** | Coding-only action used outside Coding queue |
| G01 | **Fatal** | Action not allowed for status per Final Status Action Matrix |
| G02 | **Fatal** | Appeal Required/Submitted on a claim that is not Denied |
| R01 | Non-fatal | Denial Category not in approved list |
| R02 | Non-fatal | Denial Category filled but status not Denied |
| R03 | Non-fatal | RCA not in the list for its status/category |
| R04 | Non-fatal | Denied with category but no RCA (needed for trending) |
| N01 | **Fatal** | Note's concluding action contradicts the disposition |
| N03 | **Fatal** | "Zero balance" or "OJT out of scope" closure while the note shows open balance or work done |
| N04 | **Fatal** | Claim Resubmitted (freq 1) on a denied claim with an ICN; should be Corrected Claim |
| N05 | **Fatal** | Adjustment while note cites an open recovery route and no adjustment |
| Q01 | Non-fatal | Note < 40 characters |
| Q02 | Non-fatal | Payer call without call reference # |
| Q03 | Non-fatal | Note documents a call but Status Source isn't Payer Call |
| Q04 | Non-fatal | Parked action without TAT / next follow-up in note |

How N01 works: notes carry claim history, so only the **concluding clause** is judged. That's the text after the last "hence / therefore / thus / so …ed", or else the last 40% of the note. Action families that are near-equivalent (resubmit, corrected claim and rebill; appeal and MR) are not flagged against each other.

Alias used: `Escalate to Client` (data) = `Escalated to Onshore Team` (rules).

### Output workbook tabs
Summary + code legend · Agent Scorecard (accuracy, $ at risk, top defect, zone) · Defect Pareto ·
Grid Violations (derived status × action) · Derived Status · Client View · Findings (Line Level).

---

## 5. Baseline result: 23-Oct-2026 dump

### Re-run with derived status (current logic)
- 1,892 claims, 61 agents. **Critical accuracy 92.5%**, overall accuracy 60.6%. 142 fatal claims, $150.1K balance on fatal claims.
- Agent zones: Green 27, Watch 9, Amber 8, Red 17.
- Derived status: Denied 1,010 · Undetermined 283 · Paid 222 · Rejected 133 · Claim Edit 130 · In Process 47 · Not on File 35 · Pending 26 · other 6.
- Top defects: F03 denied without Denial Category (193), Q04 (190), R04 (160), F02 (147), Q03 (117).

### First run (used the stale Claim Status column; superseded)

- 1,892 claims, 61 agents, 12 clients.
- **Critical accuracy 91.5%**, overall accuracy 62.6%. 161 fatal claims, $164.7K balance on fatal claims.
- Fatal mix: N01 73, F05 50, G01 21, N03 16, N04 11, N05 1.
- Agent zones: Green 25, Watch 11, Amber 9, Red 16. Lowest: 37.5%, 55.0% and 62.5% critical accuracy.
- The top 5 defects make up 65% of all defects: Q04 190, F02 147, F01 134, Q03 117, R02 98.
- A manual spot-check of 16 fatal flags confirmed them. Formal calibration is still pending.

### Problems with the setup itself (not agent errors)
- Action-name mismatch: "Escalate to Client" vs "Escalated to Onshore Team".
- Categories used but not in the approved list: `Contractual Adjustment`, `Other/Uncategorized`. `Submission` has no RCAs.
- The system allows submission with Status Source / Claim Status blank, despite the "mandatory" rule (~280 defects/day).
- 13 Unique IDs were worked twice in one day.
- The "PHI Safe" notes column still contains names, claim/ICN/member/auth numbers, phones and addresses.

---

## 6. Masking PHI and the UI

**Rules of the house.** There is **no BAA** with any AI or cloud vendor, so raw workbooks, audit results, quarantine files and the
re-identification key never go to a cloud service or to git (`.gitignore` blocks `*.xlsx`, `*.csv`, saved configs). Everything below
runs on this PC. Pattern and NER masking is not proof of de-identification: **compliance must approve** the method and a reviewed
sample before masked files go anywhere outside this PC.

### Workflow (`run_daily.bat`, or `AR-Audit.exe`)
1. **Workbook:** drop or pick an .xlsx (held in memory only, never saved) or use the newest `AR Audit Result*.xlsx` from OneDrive.
   Every sheet is listed; title rows above the headers are skipped automatically.
2. **Column privacy:** headers appear at once with suggested tags. Confirm or change them; they are remembered per file type.
3. **Run options:** masked Excel only, or masked Excel + local-model review / classification.
4. **Results:** download the masked workbook (and review output). Warnings, if any, are shown here.
5. **Restore:** turn a masked workbook back into the originals (separate tool).

`run_daily.bat` serves the page on this PC's network address with a secret key in the link (private-network clients only; Restore only
from this PC). `run_daily.bat local` keeps it on 127.0.0.1. `run_daily.bat doctor` checks the PC and prints what is wrong.

### The six tags
| Tag | What happens to the cell |
|---|---|
| No PHI | unchanged |
| PHI | whole value starred, first and last character of each word kept: `SMITH, JOHN` -> `S***H, J**N`; a number is one unit: `(214) 555-0187` -> `(2**) ***-***7` |
| Identifier | stable token you can group on and restore exactly: `[CLM-AB12CD]` |
| Date | days from the work date (`DOS (days vs worked)`) |
| Text | sentence kept; only names, numbers, phones, emails, addresses inside it are starred: `spoke with M***a L***z ref # 4**5, claim 1*******9` |
| Drop | column removed |

### How Text is masked (`phi_scrub.py`, `presidio_layer.py`, `daily_mask.py`)
1. Regex rules (`phi_scrub`): names after "spoke with / rep / per", labelled patient names, `LAST, FIRST`, emails, URLs, SSNs, phones,
   addresses and ZIPs, reference numbers after a label (claim, ICN, auth, ref, EFT...), any other run of 4+ digits, dates of birth.
   Kept: CPT/HCPCS, ICD-10, CARC/RARC, $ amounts, years, small counts, work dates as written.
2. Presidio + spaCy NER (`presidio_layer`; payer and client names are allow-listed; custom MRN and reference recognizers).
3. Sweep: a value masked once is masked everywhere it appears (any capitalisation); patient names from a name column also catch
   "First Last" and single names inside sentences.
4. Guards: a candidate name made only of RCM words, or of words from that sheet's own status/action/category columns ("Appeal
   Submitted", "INSURANCE PLAN, EOB"), is not a person. Placeholders the source already holds (`[CLAIM NUMBER-12]`) are left alone.
5. Residual scan: rows that still look risky go to QUARANTINE.

### Warning when a No PHI column holds PHI (`phiscan.py`)
Each No PHI column is run through the masker's own rules without changing anything; if the masker would have masked something the
column is flagged. Step 2 shows a badge, a banner (with *Apply suggested tags*) and a "Set to X" button; *Start processing* asks
for confirmation; the run scans the full data (statuses and codes once per distinct value) and reports in the log, in Results and in a
**PHI Warnings** sheet inside the masked workbook. Output is counts and value shapes (`999-999-9999`), never the values.

### Header memory and saved configs (`headermem.py`, `daily_mask.py`)
Saving or running remembers every header's tag globally (matched ignoring case, spaces and punctuation) and saves one config per file
type (file name without its date + a hash of its headers). A new sheet pre-fills remembered headers and marks the rest "new". Both
files hold headers and tags only, no data, in `%LOCALAPPDATA%\AnkaARAudit\config` (override: `AR_AUDIT_CONFIG_DIR`), shared by the exe and
python. The salt, the re-ID keys and a counts-only run log are in `%LOCALAPPDATA%\AnkaARAudit\private` (current-user ACL).

### Restore (`reidentify.py`)
Identifier tokens come back exactly. Starred values come back only when only one original matches (John and Joan both star to `J**n`,
so `J**n` stays masked rather than restoring the wrong person). Date columns come back as MM/DD/YYYY from the work date (file-name date
or the date field). Not recoverable: dropped columns, quarantined rows, ambiguous stars. The restored file contains PHI: it is built in
memory and streamed to the browser, never saved.

### LLM review and classification (`llm_audit.py`, `llm_classify.py`, `llm_context.md`)
Local Ollama, model `qwen2.5:7b-instruct` (CPU only: roughly 11-20 s per claim). Only the `- MASKED` file is read; every note is
re-scanned for PHI first and a non-loopback Ollama host is refused. `llm_audit` reviews the claims the rules engine flagged on
N01/N03/N04/N05/G01 plus a 10% control sample and compares Qwen with the rules (Confirmed / Possible false positive / NEW finding /
Unclear). `llm_classify` predicts status, action and denial category for an Open AR sheet from labels found in the sheet itself and
flags rows that differ from what the agent entered. `llm_context.md` is the plain-language brief given to the model; edit it to tune.
Advisory only: QA confirms before agent feedback.

---

## 7. Roadmap

1. **Calibrate (week 1):** QA manually audits about 50 fatal flags plus clean samples. Tune regexes to at least 90% precision, then lock which codes are fatal. Compare the LLM review with the QA answer key before trusting it.
2. **Run it daily:** automated 100% check. A QA person confirms fatal flags, plus a random 5% of clean claims.
3. **Trends:** 7/30-day agent accuracy, feeding the BQ/PIP governance process. Defect repeat rates by client and payer.
4. **More checks:**
   - Timely-filing risk (age vs payer TFL, especially before adjustments).
   - Adjustment without approval reference.
   - Bill-to-patient on CO group code.
   - Repeat touches with no progress.
   - Next-FUP date vs the action's required TAT.

---

## 8. Files

| File | Purpose |
|---|---|
| `ar_auto_audit.py` | Audit rules engine and scorecard writer |
| `ar_audit.py` | Single entry point (the exe runs this): `ui`, `daily`, `mask`, `review`, `classify`, `doctor` |
| `webui.py` | The browser UI (page + local API) |
| `daily_mask.py` | Masks a workbook (every sheet), saved configs, PHI Warnings sheet |
| `phi_scrub.py` | The masking rules for free text |
| `presidio_layer.py` | Presidio + spaCy NER pass |
| `phiscan.py` | Flags No PHI columns that hold PHI |
| `mask_columns_logic.py` | Tags and the first-guess tag from a header name |
| `headermem.py` | Header memory |
| `reidentify.py` | Restores originals from a masked workbook |
| `llm_audit.py`, `llm_classify.py`, `llm_context.md` | Local-Qwen review and classification, and the model's brief |
| `doctor.py` | Diagnostic |
| `run_daily.bat`, `build_exe.bat` | Start the UI; build the exe |
| `requirements.txt`, `.gitignore`, `CONTEXT.md` | Dependencies, data blocked from git, this document |
