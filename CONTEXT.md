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
   ├─► ar_auto_audit.py   ── rules engine, runs fully local ──► AUDIT RESULT.xlsx (contains PHI; stays local)
   │
   └─► phi_scrub.py       ── de-identify ──► SCRUBBED.xlsx  (only file allowed to reach any AI model)
                                         ├─► QUARANTINE.xlsx (rows still risky; never sent)
                                         └─► REID KEY.xlsx   (token → real value; local, restricted)
                                                │
                              (planned) AI note reviewer: local Ollama first (no BAA in place)
```

### Run
```bash
python ar_auto_audit.py "<workbook.xlsx>" ["<output.xlsx>"]
python phi_scrub.py     "<workbook.xlsx>" ["<out_dir>"]
```
Requires Python 3.10+, `pandas`, `openpyxl`.

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

## 6. PHI handling (no BAA in place)

- Assume **no BAA** with any AI or cloud vendor. Raw dumps, audit results, quarantine files and the REID KEY **never** go to a cloud service or to git (`.gitignore` blocks `*.xlsx`, `*.csv`, `scrub_out/`).
- `phi_scrub.py` (Safe Harbor style):
  - Names, phones, emails, URLs, SSNs, addresses/ZIPs and long IDs become salted-hash tokens (stable across days).
  - Dates become day offsets from Worked Date (`[D-34]`).
  - Kept: CPT/HCPCS (including `CPT-` prefixed), ICD-10, modifiers, CARC/RARC, $ amounts, payer names.
  - A final residual scan quarantines rows that still look risky. On 23-Oct: 1,883 safe, 9 quarantined.
- Pattern scrubbing is not proof of de-identification. **Compliance must approve** the method and a reviewed ~100-row sample before scrubbed data reaches any external service.
- Set a private `SALT` in `phi_scrub.py`, and keep it out of git for production.

---

### Daily mask (`daily_mask.py`)
Reads the newest `AR Audit Result*.xlsx` from OneDrive **in memory only** (no copy of the source is written), tokenises
Unique ID / Agent / Emp ID, scrubs notes via `phi_scrub.py`, then sweeps every masked value out of all other notes.
Outputs `masked_out\… - MASKED.xlsx` (the only file an LLM may see) and `… - QUARANTINE.xlsx` (never sent). The salt,
the token→value REID KEY and a counts-only run log sit in `%LOCALAPPDATA%\AnkaARAudit\private` (current-user ACL).
`run_daily.ps1` runs it; the scheduling command is in its header. Salt comes from `AR_AUDIT_SALT` or that private dir.

### LLM review (`llm_audit.py`, `llm_context.md`)
Local Ollama, model `qwen2.5:7b-instruct` (CPU-only PC: i3-12100, 15.7 GB RAM, ~17 s/claim). Reads only the `- MASKED`
file, re-runs the residual-PHI scan per note, and refuses non-loopback Ollama hosts. Reviews every claim the rules engine
flagged on N01/N03/N04/N05/G01 plus a 10% control sample, and returns schema-constrained JSON
`{note_status, note_action, verdict, confidence, reason}`. `llm_context.md` is the plain-language brief given to the model
(edit it to tune behaviour). Output `masked_out\… - LLM REVIEW.xlsx` compares the LLM to the rules engine:
Confirmed / Possible false positive / NEW finding / Unclear. Advisory only: QA confirms before agent feedback, and the
LLM must be checked against the QA answer key (roadmap step 1) before it is trusted. Override with `--model` / `AR_AUDIT_MODEL`.

### Presidio + NER layer, column marking, inventory classifier (added 27-Oct-2026)
- **Mark PHI columns once:** `python mask_columns.py` opens a small window: pick the workbook and sheet, then set each
  column header to Not PHI / Identifier (token) / Date (day offset) / Free text (scan) / Drop. Only headers are read.
  Saved to `column_profiles.json` (headers + roles, no data); `daily_mask.py` applies it to every sheet with those headers.
  Columns marked Not PHI are left alone; `daily_mask.py` warns if one looks like SSN/phone/email. Without a saved profile
  the built-in sheet types are used (Open AR inventory, WFM aging, audit result).
- **Free-text masking order:** `phi_scrub` regex rules -> `presidio_layer.py` (Presidio recognizers + spaCy NER; custom MRN and
  AR-ID recognizers; payer/client names on an allow-list) -> sweep (a value masked once is masked everywhere, any case; patient
  names from the name column also catch "First Last" and name parts). NER: `AR_AUDIT_NER=spacy` (default, `AR_AUDIT_SPACY`
  model) or `hf` (clinical de-id transformer, `AR_AUDIT_HF_MODEL`, downloaded once, not yet tested here).
  Dates far from the work date (e.g. DOB) become `[DATE]` rather than an offset that would reveal age.
- **Classifier:** `llm_classify.py` reads the masked inventory sheet, has local Qwen predict claim status / action / denial
  category from the masked Notes + denial text (labels taken from the sheet itself), and flags rows where the prediction differs
  from what the agent entered (`... - CLASSIFIED.xlsx`). Advisory, QA confirms.
- Tested on a synthetic inventory workbook (fake patients) and on the 23-Oct audit result (strict leak check: 0 originals left).
  Presidio/NER accuracy on real notes is not benchmarked; compliance sign-off is still required.

## 7. Roadmap

1. **Calibrate (week 1):** QA manually audits about 50 fatal flags plus clean samples. Tune regexes to at least 90% precision, then lock which codes are fatal.
2. **Run it daily:** automated 100% check. A QA person confirms fatal flags, plus a random 5% of clean claims.
3. **AI note reviewer:** on the scrubbed file, a local **Ollama** model (8–14B) returns structured JSON `{status, action, rca, confidence}` constrained to workbook lists. Run it only on flagged claims plus a 10–15% sample (≈300/day). Compare against the QA answer key before trusting it. Switch to a cloud model only if a BAA exists.
4. **Trends:** 7/30-day agent accuracy, feeding the BQ/PIP governance process. Defect repeat rates by client and payer.
5. **More checks:**
   - Timely-filing risk (age vs payer TFL, especially before adjustments).
   - Adjustment without approval reference.
   - Bill-to-patient on CO group code.
   - Repeat touches with no progress.
   - Next-FUP date vs the action's required TAT.

---

## 8. Files

| File | Purpose |
|---|---|
| `ar_auto_audit.py` | Rules engine and scorecard writer |
| `phi_scrub.py` | De-identification, quarantine and re-identification key |
| `CONTEXT.md` | This document |
| `.gitignore` | Blocks all data and outputs from git |

### Add / download / restore (browser UI)
- **Add a workbook:** drop an .xlsx on Step 1 (or use "add a workbook"). It is held in server memory only (`webui.UPLOADS`, one at a
  time, 100 MB cap) and masked in-process by `daily_mask.process()`; the original is never written to disk.
- **Download:** Step 3 lists the masked workbook and review outputs with Download buttons. QUARANTINE files are never offered.
- **Restore originals (`reidentify.py`):** turns a masked workbook (from the results list, or any masked workbook added) back into real
  values using every `REID KEY` in the private dir; the restored file is built in memory and streamed to the browser, never saved.
  Date columns and `[D-n]` tokens are rebuilt from the work date (file-name date or the date field) as MM/DD/YYYY. Not recoverable:
  dropped columns, `[DATE]` (far-off dates such as DOB), quarantined rows; name case variants return in first-seen form.

### Header memory (`headermem.py`, `header_memory.json`)
Every header you mark (Save, or Run) is remembered globally: role plus token kind, matched after normalising case, spaces and
punctuation. A new sheet pre-fills remembered headers and tags only unseen ones "new" for review. If a sheet matches no saved or
built-in profile but 70%+ of its headers are remembered, `daily_mask.py` masks it with no UI (unseen headers are guessed from the name
and listed in the log). Below 70% it refuses rather than guess. The file holds headers/roles only, no data; it is gitignored.
`mask_columns_logic.py` holds the header-name guesses (no GUI dependency).
