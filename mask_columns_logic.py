"""
Column roles and the first-pass guess from a header name (pure logic, no GUI).

  No PHI      leave the column exactly as it is
  PHI         mask the whole value, keeping first and last character:  SMITH, JOHN -> S***H, J**N
  Identifier  replace with a stable token you can group/join on and restore exactly:  [CLM-AB12CD]
  Date        replace with days relative to the work date
  Text        a sentence: mask only the sensitive parts inside it (names, numbers, phones), keep the wording
  Drop        remove the column from the masked file
"""
import re

ROLES = ["No PHI", "PHI", "Identifier", "Date", "Text", "Drop"]

KIND_GUESS = [("mrn|chart", "MRN"), (r"patient|\bpt\b|responsible|insured|subscriber|guarantor|^name|\bname$", "PAT"),
              ("claim", "CLM"), ("bill", "BIL"), ("account|acct", "ACC"),
              ("provider|pro name|doctor|physician|rendering", "PRV"),
              ("user|agent|submitted by|emp|assigned to|worked by", "USR")]
DATE_HINT = r"date|\bdos\b|\bdob\b|birth|assigned on|submitted on|\bdue\b|cut-?off|visit|admit|discharge|service|\bdt\b"
NOT_DATE = r"updated|modified|refresh|\bage\b|aging|^days|touches|over due"
TEXT_HINT = r"notes?$|^notes? |description|reason|comment|remark|narrative|memo|detail"
# a person's own details -> PHI (starred); a reference number -> Identifier (tokenised, groupable)
NOT_PHI = r"client|payer|insurance|facility|location|practice|\bplan\b|company|carrier|clinic|hospital|vendor"
# money, counts, statuses and audit codes are never personal, even when the header says "claim" or "ID"
NOT_ID = (r"amount|\bamt\b|charge|balance|paid|payment|total|\bcounts?\b|\bsum\b|weight|%|\brate\b|\bdays?\b|\bage\b|"
          r"aging|touches|score|accuracy|\brule|finding|defect|audit|code\b|status|queue|action|severity|priority|"
          r"classification|category|\btype\b")
PERSON_ID = r"member|subscriber|policy|mrn|patient|\bpt\b|ssn|guarantor|insured|chart|\bzip\b|address|phone|email|\bdob\b"
STRONG_ID = r"mrn|chart|claim|account|\bacct\b|member|policy|\bicn\b|\bdcn\b|\bnpi\b|unique|\bid\b|\bid$|encounter"
PHI_HINT = (r"patient|\bpt\b|\bname\b|names?$|responsible|guarantor|insured|subscriber|"
            r"\bssn\b|social|phone|fax|mobile|email|address|street|\bzip\b|\bcity\b")
ID_HINT = (r"mrn|chart|claim|bill|\bacct\b|account|member|policy|group #|encounter|visit (id|no|#)|"
           r"auth(orization)? ?(no|#|number)|\bicn\b|\bdcn\b|\bnpi\b|\bref(erence)? ?(no|#|number)|"
           r"assigned user|submitted by|worked by|provider|pro name|physician|doctor|\bid$|\bid\b|unique|\bagent\b|emp")


def guess_role(col):
    """First-pass role from the header text; you confirm or change it in the UI."""
    c = str(col).lower()
    if re.search(DATE_HINT, c) and not re.search(NOT_DATE, c):
        return "Date"
    if re.search(TEXT_HINT, c):
        return "Text"
    if re.search(NOT_PHI, c) and not re.search(PERSON_ID, c):
        return "No PHI"
    if re.search(NOT_ID, c) and not re.search(PERSON_ID, c):
        return "No PHI"
    if re.search(STRONG_ID, c):
        return "Identifier"
    if re.search(PHI_HINT, c):
        return "PHI"
    if re.search(ID_HINT, c):
        return "Identifier"
    return "No PHI"


def guess_kind(col):
    c = str(col).lower()
    return next((k for pat, k in KIND_GUESS if re.search(pat, c)), "ID")


SENSITIVE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|[\w.+-]+@[\w-]+\.[\w.]+|\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b|\b\d{6,}\b")


def guess_role_data(col, series=None):
    """Header guess, then a look at the values: a 'No PHI' column that holds sentences or phone/SSN/ID-looking
    values is suggested as Text, so whatever is inside it gets masked."""
    r = guess_role(col)
    if r != "No PHI" or series is None:
        return r
    s = series.dropna()
    s = s[s.map(lambda v: isinstance(v, str))]
    if s.empty:
        return r
    if s.str.len().mean() >= 40 and s.str.contains(" ").mean() >= 0.5 and s.nunique() >= 0.5 * len(s):
        return "Text"  # sentences, mostly unique (a category list repeats, so it stays No PHI)
    return "Text" if s.str.contains(SENSITIVE).any() else r
