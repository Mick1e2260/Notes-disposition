"""Header-name guesses (pure logic, no GUI): first-pass role and token kind for a column header."""
import re

KIND_GUESS = [("mrn|chart", "MRN"), (r"patient|\bpt\b|responsible|insured|subscriber|guarantor|^name|\bname$", "PAT"), ("claim", "CLM"),
              ("bill", "BIL"), ("account|acct", "ACC"), ("provider|pro name|doctor|physician|rendering", "PRV"),
              ("user|agent|submitted by|emp|assigned to|worked by", "USR")]
DATE_HINT = r"date|\bdos\b|\bdob\b|birth|assigned on|submitted on|\bdue\b|cut-?off|visit|admit|discharge|service|\bdt\b"
TEXT_HINT = r"notes?$|^notes? |description|reason|comment|remark|narrative|memo|detail"
ID_HINT = (r"patient|\bpt\b|\bname\b|names?$|mrn|chart|claim (number|id|#|no)|bill id|\bacct\b|account|responsible|guarantor|"
           r"member|subscriber|insured|policy|group #|ssn|social|\bdob\b|birth|phone|fax|mobile|email|address|street|zip|"
           r"assigned user|submitted by|worked by|provider|pro name|physician|doctor|encounter|visit (id|no|#)|auth(orization)? (no|#|number)|"
           r"\bicn\b|\bdcn\b|\bnpi\b|\bref(erence)? ?(no|#|number)")


def guess_role(col):
    """First-pass role from the header text; you confirm or change it in the UI."""
    c = str(col).lower()
    if re.search(DATE_HINT, c) and not re.search(r"updated|modified|refresh|\bage\b|aging|^days|touches|over due", c):
        return "Date"
    if re.search(TEXT_HINT, c):
        return "Free text"
    if re.search(ID_HINT, c):
        return "Identifier"
    return "Not PHI"


def guess_kind(col):
    c = str(col).lower()
    return next((k for pat, k in KIND_GUESS if re.search(pat, c)), "ID")
