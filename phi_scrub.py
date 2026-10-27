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

STYLE = "star"  # text + PHI columns: J**n, 1*****9 (keeps the sentence); Identifier columns use "token"
key_rows = []


def _keep_ends(s):
    idx = [i for i, ch in enumerate(s) if ch.isalnum()]
    if len(idx) < 2:
        return s
    keep = {idx[0]} if len(idx) == 2 else {idx[0], idx[-1]}
    return "".join(ch if not ch.isalnum() or i in keep else "*" for i, ch in enumerate(s))


def star(value):
    """Keep only the first and last character: SMITH, JOHN -> S***H, J**N (each word).
    A number (phone, SSN, ID) is one unit, so it shows just two digits: (214) 555-0187 -> (2**) ***-***7."""
    s = str(value)
    alnum = sum(ch.isalnum() for ch in s)
    if alnum and sum(ch.isdigit() for ch in s) * 2 >= alnum:
        return _keep_ends(s)
    return re.sub(r"[^\s,;/]+", lambda m: _keep_ends(m.group(0)), s)


def token(kind, value, style=None):
    """style 'star' -> 1*****9 (in place, readable); 'token' -> [CLM-AB12CD] (groupable, exactly restorable)."""
    t = (star(value) if (style or STYLE) == "star"
         else f"[{kind}-{hashlib.sha256((SALT + str(value)).encode()).hexdigest()[:6].upper()}]")
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
# RCM words that NER and the name rules mistake for people ("Appeal Submitted", "INSURANCE PLAN, EOB").
# A candidate name made only of these words (or of words from the sheet's own status/action/category columns) is kept.
RCM_WORDS = set("""final status action matrix payer payers payor policy client team sop contract contractual guideline
guidelines rule rules claim claims denial denials denied appeal appeals sent submitted submit resubmitted resubmit
resubmission medicare medicaid the our their and or of for to on via per with from at by as is was be has have no yes
provider providers insurance portal system note notes call called calls request requested review reviewed audit audited
onshore offshore coding coder billing billed posting posted eob era website online phone fax email plan plans aetna cigna
humana uhc bcbs anthem tricare molina optum united healthcare kaiser wellcare centene ambetter oscar highmark magellan
amerigroup availity waystar trizetto ivr rep representative tfl cms ncci lcd ncd balance zero cpt hcpcs icd dx case closed
close benefits benefit check checks date dates copay coinsurance deductible correct corrected duplicate frequency code codes
not invalid valid payment payments prior upheld overturned name paid pending auth authorization authorizations precert
referral coverage eligibility eligible terminated active inactive primary secondary tertiary cob medical records record
necessity timely filing limit rejected rejection rejections clearinghouse level first second third adjustment adjusted
write off writeoff refund recoupment offset overpayment underpayment information info additional required follow up
followup reprocessing reprocess processed process processing received acknowledged scheduled pay out scope ojt patient
responsibility transfer transferred hold escalated escalation supervisor qa found missing incorrect wrong error errors
issue issues document documents documentation office member subscriber group modifier units charge charges amount
rca dob dos mrn ssn npi tin id icn dcn""".split())
VOCAB = set()  # per sheet, set by daily_mask.mask(): words from that sheet's No PHI category-style columns


def is_vocab(text):
    """True when every word is RCM vocabulary, so the text is a status/action phrase, not a person."""
    words = re.findall(r"[A-Za-z]{2,}", str(text))
    return bool(words) and all(w.lower() in RCM_WORDS or w.lower() in VOCAB for w in words)
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
    # a number introduced by a reference word: only the number is masked ("claim 12345" -> "claim 1***5")
    ("REF", re.compile(r"\b(?:claim|clm|icn|dcn|acct|account|member|mbr|subscriber|auth|authori[sz]ation|ref|reference|"
                       r"case|batch|eft|check|cheque|trace|voucher|policy|seq|confirmation|ticket)\b"
                       r"(?:\s*(?:no|number|num|id|#))?[\s:#.=-]{0,4}(?P<v>\d{5,}|(?!(?:19|20)\d\d\b)\d{4})\b", re.I)),
    # long identifiers: masked (****46) or >=6 chars containing >=5 digits, e.g. claim#, ICN, member ID, EFT
    ("ID", re.compile(r"\*{2,}[\w-]*")),
    ("ID", re.compile(r"\b(?=[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,}\b", re.I)),
]
# code-shaped tokens that must survive the ID rule
KEEP = re.compile(r"^(?:(?:CPT|HCPCS|DX|ICD|CO|PR|OA|PI|MOD)[-:# ]*)?(?:\d{4}[0-9FTU]|[A-V]\d{4}|[A-Z]\d{2}(?:\.\w{1,4})?|\d{1,3}(?:\.\d+)?)$", re.I)


# already masked, never re-scanned: our tokens, starred values, and placeholders the source system already put in the
# notes ("[CLAIM NUMBER-12]", "MEMBER ID-3"); a one-word code like ICN-1234 is NOT a placeholder and still gets masked
TOKEN_RX = re.compile(r"(\[(?:[A-Z]+-[0-9A-F]{6}|D[+-]\d+|DATE)\]|[A-Za-z0-9]*\*{2,}[A-Za-z0-9]*"
                      r"|\[[A-Z][A-Z ]{2,30}-\d{1,4}\]|\b[A-Z]{2,}(?: [A-Z]{2,}){1,3}-\d{1,3}\b)")


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


def _mask_match(m, kind):
    """Mask a match; a pattern with a (?P<v>...) group masks only that part and keeps the words around it."""
    if "v" in m.re.groupindex:
        a, b = m.span("v")
        g0 = m.start()
        return m.group(0)[: a - g0] + token(kind, m.group("v")) + m.group(0)[b - g0:]
    return m.group(0) if KEEP.match(m.group(0)) else token(kind, m.group(0))


def scrub_note(text, worked):
    t = str(text)
    t = shift_dates(t, worked)
    t = SUBSCRIBER_NAME.sub(lambda m: m.group(1) + token("NAME", m.group(2)), t)
    t = UPPER_NAME.sub(lambda m: m.group(0) if is_vocab(m.group(0)) else token("NAME", m.group(0)), t)
    t = NAME_TRIGGER.sub(lambda m: m.group(0) if is_vocab(m.group(2)) else f"{m.group(1)} {token('NAME', m.group(2))}", t)
    # never re-scan our own tokens (the ID rule would match e.g. NAME-12345A inside [NAME-12345A])
    parts = TOKEN_RX.split(t)
    for kind, rx in SUBS:
        parts = [p if i % 2 else rx.sub(lambda m, k=kind: _mask_match(m, k), p) for i, p in enumerate(parts)]
        parts = TOKEN_RX.split("".join(parts))
    return "".join(parts)


# --- residual-risk scan: anything here sends the row to quarantine --------
RESIDUAL = {
    "digits_6plus": re.compile(r"\d{6,}"),
    "phone_like": re.compile(r"\d{3}[\s.-]\d{3}[\s.-]\d{4}"),
    "name_after_trigger": re.compile(r"\b(s/w|spoke with|rep\.?)\s+([A-Z][a-z]{2,})\b"),
    "calendar_date": re.compile(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b"),
    "dob_ssn_word": re.compile(r"\b(dob|date of birth|ssn|social security)\b\W{0,6}\d", re.I),  # label + an unmasked number
}


def residual(t):
    t = TOKEN_RX.sub(" ", t)  # ignore what we already masked
    def hit(k, rx):  # "spoke with Payer" is a status phrase, not a missed name
        if k == "name_after_trigger":
            return any(not is_vocab(m.group(2)) for m in rx.finditer(t))
        return bool(rx.search(t))
    return ", ".join(k for k, rx in RESIDUAL.items() if hit(k, rx))


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
