"""
AR Auto-Audit: Notes vs Disposition (Claim Status / RCA / Resolution Action)

Usage:  python ar_auto_audit.py "<workbook.xlsx>" [output.xlsx]
The workbook's first sheet is the day's AR work dump; the rule tabs
('2. Claim Status - RCA', 'Final Status Action Matrix') are read from the same
workbook, so rule changes there flow into the audit automatically.

Scoring convention (state on every scorecard):
  - Each claim is checked against every rule below. A claim is FATAL if it has
    >= 1 fatal defect (binary per claim).
  - Critical accuracy = claims without a fatal defect / claims audited.
  - Overall accuracy  = claims with zero defects / claims audited.
  - Note-vs-disposition checks are keyword inference: treat as "flag for QA
    confirmation", not as a final verdict, until calibrated.
"""
import re
import sys
from pathlib import Path

import pandas as pd

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else SRC.with_name(SRC.stem + " - AUDIT RESULT.xlsx")

NOTES = "Notes (PHI Safe"
ACT = "Last Resolution Action"
CODING_ACTIONS = {"Routed Back to AR Team", "Coding Error Fixed & Resubmitted",
                  "Client Intervention Needed - Sent Back to AR"}
ACTION_ALIAS = {"Escalate to Client": "Escalated to Onshore Team",
                "Adjustment Processed - Post": "Adjustment Processed",
                "Adjustment Processed - Park": "Adjustment Processed"}
CALL_SOURCES = {"Payer Call", "IVR", "Payer Call Back"}


# ---------------------------------------------------------------- rule tables
def load_grid(xl):
    g = pd.read_excel(xl, "Final Status Action Matrix", header=None)
    hdr = g.index[g[0].astype(str).str.strip() == "Resolution Action"][0]
    cols = [str(c).strip() for c in g.iloc[hdr]]
    grid = {}
    for _, r in g.iloc[hdr + 1:].iterrows():
        act = str(r[0]).strip()
        if act in ("nan", "") or act.startswith("No Action Required") or act.startswith("Dispositions"):
            continue
        for i, st in enumerate(cols[1:], 1):
            if st != "nan" and pd.notna(r[i]):
                grid[(act, st)] = str(r[i]).strip().upper().startswith("Y")
    return grid


def load_rca(xl):
    t = pd.read_excel(xl, "2. Claim Status - RCA", header=None)
    m, status, cat = {}, None, None
    for _, r in t.iloc[3:].iterrows():
        a, b, c = (str(x).strip() if pd.notna(x) else "" for x in r[:3])
        if a.isupper() or a.startswith(("FOLLOW-UP", "DENIED", "REJECTED", "Coding queue")):
            continue
        if a:
            status = a
        if b:
            cat = None if b == "-" else b
        if c and not c.startswith("("):
            m.setdefault((status, cat), set()).add(c)
    return m


# ------------------------------------------------- note-text inference (NLP-lite)
def rx(*p):
    return re.compile("|".join(p), re.I)

NOTE_ACTION = [  # (inferred action, pattern) - first match that the agent's action contradicts
    ("Adjustment Processed", rx(r"\badjust(ed|ment)? (done|the|via|in|as)", r"write[- ]?off", r"\bw/o\b", r"hence adjusted")),
    ("Corrected Claim Resubmitted", rx(r"corrected claim", r"\bCC\b", r"frequency (code )?7", r"replacement claim")),
    ("Claim Resubmitted", rx(r"re-?submit", r"re-? ?bill(ed)? the claim", r"submit(ted)? again")),
    ("Rebilled to Correct Payer", rx(r"bill(ed)? (the claim )?to (the )?(correct|new|other)", r"rebill(ed)? to")),
    ("Billed to Secondary / Tertiary Payer", rx(r"(transfer|bill|crossover|forward)\w* (\w+ ){0,4}secondary", r"balance transfer to secondary")),
    ("Billed to Patient", rx(r"(bill|transfer)\w* (\w+ ){0,4}(to )?(the )?patient", r"patient (bucket|responsibility) transfer")),
    ("Appeal Submitted", rx(r"(sent|submitted|faxed|filed) (an |the )?appeal", r"appeal (sent|submitted|filed)", r"reconsideration (sent|submitted)")),
    ("Appeal Required", rx(r"(route|forward|tag)\w* (\w+ ){0,3}appeal team", r"appeal team to create")),
    ("Medical Records Submitted", rx(r"(sent|submitted|faxed|upload\w*) (the )?(medical records|MR\b|records)")),
    ("Sent to Coding", rx(r"(tag|route|sen[dt]|forward)\w* (\w+ ){0,3}coding")),
    ("Sent to Payment Posting Team", rx(r"(route|sen[dt]|forward)\w* (\w+ ){0,3}posting", r"posting for payment")),
    ("Escalated to Onshore Team", rx(r"onshore", r"escalat", r"reach out to client", r"route\w* (\w+ ){0,3}client")),
    ("Sent for Reprocessing", rx(r"reprocess", r"re-?process")),
]
NOTE_STATUS = [
    ("Not on File", rx(r"no (claim|bill) (on|in) file", r"not on file", r"never received", r"no record of (the )?claim")),
    ("Rejected", rx(r"\brejected\b", r"rejection")),
    ("Denied", rx(r"\bden(y|ied|ial)\b", r"\bdneied\b")),
    ("Paid", rx(r"\bpaid \$?\d", r"\bpd\$?\d", r"paid (with|via|on|under)", r"eft ?#", r"check ?#")),
    ("In Process", rx(r"in process", r"under review", r"pending review")),
]
SAME_FAMILY = [  # action pairs that must not be flagged against each other
    {"Claim Resubmitted", "Corrected Claim Resubmitted", "Rebilled to Correct Payer", "Coding Error Fixed & Resubmitted"},
    {"Appeal Submitted", "Second Level Appeal Submitted", "Medical Records Submitted", "Appeal Required"},
    {"Escalated to Onshore Team", "Client Intervention Needed - Sent Back to AR"},
]


DERIVE_STATUS = [  # checked against the note in order; first hit wins
    ("Not on File", NOTE_STATUS[0][1]),
    ("Rejected", rx(r"\brejected\b", r"rejection", r"clearing ?house (error|reject)")),
    ("Claim Edit/Scrubber Edits", rx(r"batch error", r"scrubber", r"stuck in (batch|pms|validation)", r"claim edit")),
    ("Pending - Info Requested", rx(r"(requested|request(ing)?|asked for|need(s)?) (the )?(mr|medical records|records|w-?9|c-?9|cob|questionnaire|itemi[sz]ed|primary eob|wol|accident)",
                                    r"\b(adr|records request)\b")),
    ("Denied", NOTE_STATUS[2][1]),
    ("Scheduled to Pay", rx(r"scheduled to pay", r"check (is )?(not yet )?(issued|mailed)", r"payment (is )?pending", r"finali[sz]ed.{0,30}(not|yet).{0,10}paid")),
    ("Paid", NOTE_STATUS[3][1]),
    ("In Process", rx(r"in process", r"under review", r"pending review", r"in review", r"within (the )?tat", r"reprocess")),
    ("Acknowledged/Received", rx(r"acknowledg", r"claim (was )?received", r"\b277ca\b")),
]


def derive_status(r, note):
    """Claim Status column is stale: work status out from rejection/denial fields + the note."""
    if pd.notna(r["Rejection Category"]) or pd.notna(r["Rejection Reason"]):
        return "Rejected", "Rejection fields"
    for st, pat in DERIVE_STATUS:
        if pat.search(note):
            if st == "Denied" or st in ("Paid", "Scheduled to Pay", "In Process", "Acknowledged/Received"):
                if pd.notna(r["CARC"]) or pd.notna(r["Category"]):
                    return "Denied", "Denial fields + note"
            return st, "Note text"
    if pd.notna(r["CARC"]) or pd.notna(r["Category"]):
        return "Denied", "Denial fields"
    return None, "Undetermined"


def same_family(a, b):
    return a == b or any(a in f and b in f for f in SAME_FAMILY)


# ---------------------------------------------------------------- audit rules
def audit(df, grid, rca_map):
    rca_by_cat = {}
    for (st, cat), s in rca_map.items():
        rca_by_cat.setdefault(cat if st in ("Denied", "Rejected") else st, set()).update(s)
    denial_cats = {c for (s, c) in rca_map if s == "Denied"}

    findings = []

    def add(r, code, sev, area, msg):
        findings.append(dict(row=r.name, Code=code, Severity=sev, Area=area, Finding=msg))

    derived = []
    for _, r in df.iterrows():
        note = str(r[NOTES])
        st, basis = derive_status(r, note)
        derived.append((st or "Undetermined", basis))
        act_raw = str(r[ACT]).strip()
        act = ACTION_ALIAS.get(act_raw, act_raw)
        cat, rca = r["Category"], r["RCA"]
        note = str(r[NOTES])
        coding_form = r["From Queue"] == "Coding" or act in CODING_ACTIONS

        # --- A. Form / mandatory-field compliance (sheet 1)
        if not coding_form:
            if pd.isna(r["Status Source"]):
                add(r, "F02", "Non-Fatal", "Status Source", "Status Source blank (mandatory)")
            if st == "Denied" and pd.isna(cat):
                add(r, "F03", "Non-Fatal", "Denial Category", "Denied claim without Denial Category")
            if st == "Rejected" and pd.isna(r["Rejection Category"]):
                add(r, "F04", "Non-Fatal", "Rejection Category", "Rejected claim without Rejection Category")
            if st == "Rejected" and pd.isna(rca):
                sev = "Fatal" if basis == "Rejection fields" else "Non-Fatal"  # note-only status: verify first
                add(r, "F05", sev, "RCA", "Rejected claim without RCA (mandatory on Rejection)")
            if r["Status Source"] in CALL_SOURCES and pd.isna(r["Representative Name"]):
                add(r, "F06", "Non-Fatal", "Rep Name", "Status from payer call/IVR but Representative Name blank")
            if act == "Escalated to Onshore Team" and pd.isna(r["Escalation Reason"]):
                add(r, "F07", "Non-Fatal", "Escalation", "Escalated without Escalation Reason")
        elif act not in CODING_ACTIONS:
            add(r, "F08", "Fatal", "Resolution Action", f"Coding-queue claim closed with non-coding action '{act_raw}'")
        if act in CODING_ACTIONS and r["From Queue"] != "Coding":
            add(r, "F09", "Fatal", "Resolution Action", f"Coding-only action '{act_raw}' used outside Coding queue")

        # --- B. Status x Action grid
        if st and not coding_form:
            ok = grid.get((act, st))
            if ok is False:
                add(r, "G01", "Fatal", "Status-Action", f"'{act_raw}' not allowed for status '{st}' per Final Status Action Matrix")
        if act == "Appeal Required" or act == "Appeal Submitted":
            if st not in ("Denied", "Pending - Info Requested") and not coding_form:
                add(r, "G02", "Fatal", "Status-Action", f"{act_raw} on a claim that is not Denied")

        # --- C. RCA / Category validity
        if pd.notna(cat) and st == "Denied" and cat not in denial_cats:
            add(r, "R01", "Non-Fatal", "Denial Category", f"Denial Category '{cat}' not in approved list")
        if pd.notna(cat) and st and st != "Denied" and not coding_form:
            add(r, "R02", "Non-Fatal", "Denial Category", f"Denial Category filled but status is '{st}'")
        if pd.notna(rca) and not coding_form:
            key = cat if st == "Denied" else (r["Rejection Category"] if st == "Rejected" else st)
            allowed = rca_by_cat.get(key)
            if allowed is not None and rca not in allowed:
                add(r, "R03", "Non-Fatal", "RCA", f"RCA '{rca}' does not belong to '{key}' list")
            elif allowed is None and st not in ("Denied", "Rejected"):
                add(r, "R03", "Non-Fatal", "RCA", f"RCA '{rca}' entered for status '{st}' (no such RCA list)")
        if st == "Denied" and pd.notna(cat) and pd.isna(rca) and not coding_form:
            add(r, "R04", "Non-Fatal", "RCA", "Denied claim without RCA (optional in system but required for RCA trending)")

        # --- D. Note vs disposition (inference)
        n_low = note.lower()
        # notes carry history; judge only the concluding action ("hence/therefore/so ..." clause or note tail)
        concl = re.split(r"\b(?:hence|therefore|thus)\b|\bso,? (?=\w+ed\b|need)", note, flags=re.I)
        tail = concl[-1] if len(concl) > 1 else note[-int(len(note) * 0.4) - 1:]
        if not coding_form and act != "Escalated to Onshore Team":
            for inf_act, pat in NOTE_ACTION:
                if pat.search(tail):
                    if not same_family(inf_act, act):
                        add(r, "N01", "Fatal", "Note vs Action",
                            f"Note's concluding action reads as '{inf_act}' but disposition is '{act_raw}'")
                    break
        if act.startswith("No action needed - zero balance") and re.search(r"(need|allow) (more )?time to post|not posted|adjustment done", n_low):
            add(r, "N03", "Fatal", "Note vs Action", "Zero-balance closure but note shows balance still open / pending posting / adjusted")
        if act.startswith("No action needed - out of scope for OJT") and re.search(r"transferr|adjusted|billed|submitted", n_low):
            add(r, "N03", "Fatal", "Note vs Action", "OJT out-of-scope closure but note shows work was actually done")
        if act == "Claim Resubmitted" and re.search(r"\b(icn|claim ?#)\W*\*?\d", n_low) and st == "Denied":
            add(r, "N04", "Fatal", "Note vs Action", "Claim Resubmitted (freq 1) on adjudicated/denied claim with ICN - should be Corrected Claim (duplicate-denial risk)")
        if act == "Adjustment Processed" and st == "Denied" and re.search(r"appeal|resubmit|corrected", n_low) and not re.search(r"adjust", n_low):
            add(r, "N05", "Fatal", "Note vs Action", "Adjustment while note references an open recovery route and no adjustment")

        # --- E. Note documentation quality
        if len(note.strip()) < 40:
            add(r, "Q01", "Non-Fatal", "Note Quality", "Note too short to support disposition (<40 chars)")
        if re.search(r"\b(called|call was made|s/w|spoke)", n_low) and not re.search(r"ref|reference", n_low):
            add(r, "Q02", "Non-Fatal", "Note Quality", "Payer call documented without call reference #")
        if re.search(r"\b(called|call was made|s/w|spoke)", n_low) and r["Status Source"] not in CALL_SOURCES and pd.notna(r["Status Source"]):
            add(r, "Q03", "Non-Fatal", "Status Source", f"Note documents a payer call but Status Source = '{r['Status Source']}'")
        if act in ("Claim Resubmitted", "Corrected Claim Resubmitted", "Appeal Submitted", "Medical Records Submitted",
                   "Additional Information Submitted", "Follow-Up Required", "Sent for Reprocessing") \
                and not re.search(r"\btat\b|\btfl\b|days|follow ?up|allow|wait", n_low):
            add(r, "Q04", "Non-Fatal", "Note Quality", "Parked action without TAT / next follow-up in note")
    df["Derived Status"] = [d[0] for d in derived]
    df["Status Basis"] = [d[1] for d in derived]
    return pd.DataFrame(findings)


# ---------------------------------------------------------------- reporting
def main():
    xl = pd.ExcelFile(SRC)
    df = pd.read_excel(xl, xl.sheet_names[0])
    grid, rca_map = load_grid(xl), load_rca(xl)
    f = audit(df, grid, rca_map)

    key_cols = ["#", "Client Name", "Unique ID", "Agent", "Emp ID", "Derived Status", "Status Basis", "Category", "RCA",
                ACT, "Status Source", "Balance Amount", NOTES]
    f = f.merge(df[key_cols], left_on="row", right_index=True).drop(columns="row")

    fatal_rows = set(f.loc[f.Severity == "Fatal", "#"])
    any_rows = set(f["#"])
    df["Fatal"] = df["#"].isin(fatal_rows)
    df["Any Defect"] = df["#"].isin(any_rows)
    df["Fatal $ at risk"] = df["Balance Amount"].where(df["Fatal"], 0)

    def score(g):
        n = len(g)
        return pd.Series({
            "Claims Worked": n,
            "Fatal Claims": int(g["Fatal"].sum()),
            "Claims w/ Any Defect": int(g["Any Defect"].sum()),
            "Critical Accuracy %": round((n - g["Fatal"].sum()) / n * 100, 1),
            "Overall Accuracy %": round((n - g["Any Defect"].sum()) / n * 100, 1),
            "Balance at Risk (Fatal) $": round(g["Fatal $ at risk"].sum(), 2),
        })

    agents = df.groupby(["Agent", "Emp ID"]).apply(score, include_groups=False).reset_index()
    agents = agents.sort_values(["Critical Accuracy %", "Claims Worked"], ascending=[True, False])
    top_defect = f.groupby("Agent")["Code"].agg(lambda s: s.value_counts().index[0]).rename("Top Defect Code")
    agents = agents.merge(top_defect, on="Agent", how="left")
    agents["Zone"] = pd.cut(agents["Critical Accuracy %"], [-1, 90, 95, 98, 101],
                            labels=["Red (<90)", "Amber (90-95)", "Watch (95-98)", "Green (>=98)"])

    clients = df.groupby("Client Name").apply(score, include_groups=False).reset_index().sort_values("Critical Accuracy %")
    status_mix = (df.groupby(["Derived Status", "Status Basis"]).size().rename("Claims").reset_index()
                  .sort_values("Claims", ascending=False))
    pareto = (f.groupby(["Code", "Severity", "Area", f["Finding"].str.replace(r"'[^']*'", "'…'", regex=True)])
                .size().rename("Count").reset_index().sort_values("Count", ascending=False))
    pareto["Cum %"] = (pareto["Count"].cumsum() / pareto["Count"].sum() * 100).round(1)
    combo = (f.loc[f.Code == "G01"].groupby(["Derived Status", ACT]).size().rename("Count")
             .reset_index().sort_values("Count", ascending=False))
    n = len(df)
    summary = pd.DataFrame([
        ("Workbook", SRC.name), ("Claims audited (100%)", n), ("Agents", df["Agent"].nunique()),
        ("Fatal claims", int(df["Fatal"].sum())),
        ("Critical accuracy %", round((n - df["Fatal"].sum()) / n * 100, 1)),
        ("Overall accuracy %", round((n - df["Any Defect"].sum()) / n * 100, 1)),
        ("Total defects logged", len(f)),
        ("Balance on fatal claims $", round(df["Fatal $ at risk"].sum(), 2)),
        ("Duplicate Unique IDs worked", int(df["Unique ID"].duplicated().sum())),
        ("Status basis", "Claim Status column ignored (stale); status derived from rejection/denial fields + note text"),
        ("Convention", "Fatal = binary per claim; Critical acc = 1 - fatal claims/claims; Overall = 1 - claims with any defect/claims"),
        ("Caveat", "N-codes are keyword inference from notes - QA confirms before agent feedback"),
    ], columns=["Metric", "Value"])
    legend = pd.DataFrame([
        ("F02-F09", "Form / mandatory-field compliance (Overview tab)"),
        ("G01-G02", "Status x Resolution Action grid (Final Status Action Matrix)"),
        ("R01-R04", "Denial Category / RCA validity (Claim Status - RCA tab)"),
        ("N01, N03-N05", "Note vs disposition contradiction (inferred from note text)"),
        ("Q01-Q04", "Note documentation quality (action definitions: 'Notes must capture')"),
    ], columns=["Code family", "Meaning"])

    with pd.ExcelWriter(OUT, engine="openpyxl") as w:
        summary.to_excel(w, sheet_name="Summary", index=False)
        legend.to_excel(w, sheet_name="Summary", index=False, startrow=len(summary) + 2)
        agents.to_excel(w, sheet_name="Agent Scorecard", index=False)
        pareto.to_excel(w, sheet_name="Defect Pareto", index=False)
        combo.to_excel(w, sheet_name="Grid Violations", index=False)
        status_mix.to_excel(w, sheet_name="Derived Status", index=False)
        clients.to_excel(w, sheet_name="Client View", index=False)
        f.sort_values(["Severity", "Agent"]).to_excel(w, sheet_name="Findings (Line Level)", index=False)
        for ws in w.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(60, max(10, max(len(str(c.value or "")) for c in col[:200]) + 2))
            ws.freeze_panes = "A2"
    print(summary.to_string(index=False))
    print(pareto.head(15).to_string(index=False))
    print(agents.head(12).to_string(index=False))
    print(agents["Zone"].value_counts().to_string())
    print("->", OUT)


if __name__ == "__main__":
    main()
