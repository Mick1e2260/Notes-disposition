"""
PHI scrub for the AR work dump (HIPAA Safe Harbor style), run BEFORE any note
leaves this machine.

Usage:  python phi_scrub.py "<workbook.xlsx>" [out_dir]

Outputs (in out_dir, default = next to the workbook):
  <name> - SCRUBBED.xlsx    safe payload: token IDs, scrubbed notes, relative dates
  <name> - QUARANTINE.xlsx  rows whose scrubbed note still looks risky (never send these)
  <name> - REID KEY.xlsx    token -> original value. Stays local, restricted access.

What is removed / replaced in notes:
  person names (rep, subscriber, patient), phone/fax, email, URL, SSN,
  street / PO Box addresses and ZIPs, every long ID (claim #, ICN, member ID,
  auth #, call ref, EFT/check #, case #, batch #), calendar dates -> day offsets
  from the Worked Date (e.g. [D-34]) so TFL/TAT logic still works.
What is kept (not identifiers under Safe Harbor, and needed for the audit):
  CPT/HCPCS, ICD-10, modifiers, CARC/RARC, $ amounts, payer names, TAT/TFL wording.

This lowers risk; it does not make data "de-identified" on its own say-so.
Have compliance sign off on the method and on a reviewed sample before using
any cloud service without a BAA.
"""
import hashlib
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

NOTES = "Notes (PHI Safe"
# Private salt, same salt -> stable tokens across days. Never hardcode: set env AR_AUDIT_SALT
# (daily_mask.py generates and stores one outside the repo on first run).
SALT = os.environ.get("AR_AUDIT_SALT", "CHANGE-ME")

# Columns that carry identifiers and are not needed by an AI reviewer
DROP_COLS = ["Representative Name", "Call Reference #", "Portal Reference #", "Insurance Phone"]
TOKEN_COLS = ["Unique ID"]
DATE_COLS = ["DOS", "Billed Date", "Due Date", "Denial Date"]

key_rows = []


def token(kind, value):
    t = f"[{kind}-{hashlib.sha256((SALT + str(value)).encode()).hexdigest()[:6].upper()}]"
    key_rows.append((t, kind, str(value)))
    return t


# --- note patterns, applied in order --------------------------------------
MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
DATE_RX = re.compile(
    rf"(?<!\d)(\d{{1,2}})[/-](\d{{1,2}})[/-](\d{{2,4}})(?!\d)"            # 10/23/2026, 9/1/26
    rf"|\b({MONTHS})\.? (\d{{1,2}}),? (\d{{4}})\b",               # October 2, 2026
    re.I)
NAME_TRIGGER = re.compile(
    r"\b(s/w|spoke (?:with|to)|spk w|talked to|rep(?:resentative)?\.?(?: name)?(?: is)?:?|"
    r"agent|transferred to|per)\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-z]+)?)(?=[\s,.;]|$)")
SUBSCRIBER_NAME = re.compile(
    r"\b((?:subscriber|patient|member|insured|pt)(?:'s)?\s*name\s*(?:is|as|:)?\s*)"
    r"([A-Z][A-Za-z'-]+,?\s+[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+)?)")
UPPER_NAME = re.compile(r"\b[A-Z]{2,}(?: [A-Z]{2,})?, [A-Z]{2,}(?: [A-Z]{2,})?\b")  # MORENO, PABLA
SUBS = [
    ("EMAIL", re.compile(r"[\w.*+-]+@[\w-]+\.[\w.]+")),
    ("URL", re.compile(r"https?://\S+|www\.\S+", re.I)),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE", re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")),
    ("ADDR", re.compile(r"\b(?:P\.?\s?O\.?\s?BOX|POB)\s*#?\s*\d+[^.;\n]{0,60}?\b[A-Z]{2}\s*[-,]?\s*\d{5}(?:-\d{4})?", re.I)),
    ("ADDR", re.compile(r"\b(?:P\.?\s?O\.?\s?BOX|POB)\s*#?\s*\d+", re.I)),
    ("ADDR", re.compile(r"\b\d{2,6}\s+(?:[NSEW]\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+"
                        r"(?:st|street|ave|avenue|rd|road|blvd|dr|drive|ln|lane|way|ct|pkwy|hwy|suite|ste)\b\.?[^.;\n]{0,40}", re.I)),
    ("ZIP", re.compile(r"\b[A-Z]{2}[\s,-]+\d{5}(?:-\d{4})?\b")),
    # long identifiers: masked (****46) or >=6 chars containing >=5 digits, e.g. claim#, ICN, member ID, EFT
    ("ID", re.compile(r"\*{2,}[\w-]*")),
    ("ID", re.compile(r"\b(?=[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,}\b", re.I)),
]
# code-shaped tokens that must survive the ID rule
KEEP = re.compile(r"^(?:(?:CPT|HCPCS|DX|ICD|CO|PR|OA|PI|MOD)[-:# ]*)?(?:\d{4}[0-9FTU]|[A-V]\d{4}|[A-Z]\d{2}(?:\.\w{1,4})?|\d{1,3}(?:\.\d+)?)$", re.I)


TOKEN_RX = re.compile(r"(\[(?:[A-Z]+-[0-9A-F]{6}|D[+-]\d+|DATE)\])")  # odd-indexed items after split() are tokens


def shift_dates(text, worked):
    def repl(m):
        try:
            if m.group(1):
                y = int(m.group(3)); y = y + 2000 if y < 100 else y
                d = datetime(y, int(m.group(1)), int(m.group(2)))
            else:
                d = datetime.strptime(f"{m.group(4)[:3]} {m.group(5)} {m.group(6)}", "%b %d %Y")
        except ValueError:
            return "[DATE]"
        days = (d - worked).days
        return f"[D{days:+d}]" if abs(days) <= 1100 else "[DATE]"  # far-off dates (DOB etc.) would reveal age
    return DATE_RX.sub(repl, text)


def scrub_note(text, worked):
    t = str(text)
    t = shift_dates(t, worked)
    t = SUBSCRIBER_NAME.sub(lambda m: m.group(1) + token("NAME", m.group(2)), t)
    t = UPPER_NAME.sub(lambda m: token("NAME", m.group(0)), t)
    t = NAME_TRIGGER.sub(lambda m: f"{m.group(1)} {token('NAME', m.group(2))}", t)
    # never re-scan our own tokens (the ID rule would match e.g. NAME-12345A inside [NAME-12345A])
    parts = TOKEN_RX.split(t)
    for kind, rx in SUBS:
        parts = [p if i % 2 else rx.sub(lambda m, k=kind: m.group(0) if KEEP.match(m.group(0)) else token(k, m.group(0)), p)
                 for i, p in enumerate(parts)]
        parts = TOKEN_RX.split("".join(parts))
    return "".join(parts)


# --- residual-risk scan: anything here sends the row to quarantine --------
RESIDUAL = {
    "digits_6plus": re.compile(r"\d{6,}"),
    "phone_like": re.compile(r"\d{3}[\s.-]\d{3}[\s.-]\d{4}"),
    "name_after_trigger": re.compile(r"\b(s/w|spoke with|rep\.?)\s+[A-Z][a-z]{2,}\b(?! ?\[)"),
    "calendar_date": re.compile(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b"),
    "dob_ssn_word": re.compile(r"\b(dob|date of birth|ssn|social security)\b", re.I),
}


def residual(t):
    t = re.sub(r"\[[A-Z]+[-+]?[0-9A-F]*\]", " ", t)  # ignore our own tokens
    return ", ".join(k for k, rx in RESIDUAL.items() if rx.search(t))


def main():
    SRC = Path(sys.argv[1])
    OUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else SRC.parent
    df = pd.read_excel(SRC, sheet_name=0)
    out = df.drop(columns=[c for c in DROP_COLS if c in df.columns]).copy()
    worked = pd.to_datetime(df["Worked Date"])
    for c in TOKEN_COLS:
        out[c] = df[c].map(lambda v: token("CLM", v))
    for c in DATE_COLS:
        if c in df.columns:
            out[c] = (pd.to_datetime(df[c], errors="coerce") - worked.dt.normalize()).dt.days
            out = out.rename(columns={c: f"{c} (days vs worked)"})
    out[NOTES] = [scrub_note(n, w) for n, w in zip(df[NOTES], worked)]
    out["Residual Risk"] = out[NOTES].map(residual)

    safe = out[out["Residual Risk"] == ""].drop(columns="Residual Risk")
    quar = out[out["Residual Risk"] != ""]
    key = pd.DataFrame(key_rows, columns=["Token", "Type", "Original"]).drop_duplicates("Token")

    stem = SRC.stem
    safe.to_excel(OUT_DIR / f"{stem} - SCRUBBED.xlsx", index=False)
    quar.to_excel(OUT_DIR / f"{stem} - QUARANTINE.xlsx", index=False)
    key.to_excel(OUT_DIR / f"{stem} - REID KEY.xlsx", index=False)
    print(f"rows {len(df)} | safe {len(safe)} | quarantined {len(quar)} | tokens {len(key)}")
    print(key["Type"].value_counts().to_string())
    print(quar["Residual Risk"].str.split(", ").explode().value_counts().to_string())


if __name__ == "__main__":
    main()
