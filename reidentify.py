"""
Restore a masked workbook to its original values. Local only.

Tokens ([CLM-AB12CD], [NAME-...], ...) are looked up in every '... - REID KEY ....xlsx' in PRIVATE_DIR (the salt is
stable, so keys from earlier days still resolve). Date offsets are turned back into dates from the work date:
the 'X (days vs worked)' columns become 'X', and [D-34] inside notes becomes MM/DD/YYYY.

What cannot come back:
  - columns that were dropped when masking
  - far-off dates that were masked as [DATE] (e.g. a date of birth), by design
  - rows that went to QUARANTINE (they are not in the masked file)
  - original date formats / case variants: dates return as MM/DD/YYYY, and a name matched in a different capitalisation
    returns in its first-seen form
The restored workbook contains PHI. Build it in memory and hand it straight to the person who needs it; never store it.
"""
import io
import re
from datetime import timedelta

import pandas as pd

import daily_mask as dm

TOKEN = re.compile(r"\[[A-Z]+-[0-9A-F]{6}\]")
DOFF = re.compile(r"\[D([+-]\d+)\]")
DAYS_SUFFIX = " (days vs worked)"


def key_map():
    m = {}
    for f in sorted(dm.PRIVATE_DIR.glob("* - REID KEY *.xlsx"), key=lambda p: p.stat().st_mtime):
        k = pd.read_excel(f)
        m.update({str(t): str(o) for t, o in zip(k["Token"], k["Original"])})
    return m


def restore_bytes(raw, anchor):
    """Return (xlsx bytes, stats). `anchor` is the work date (datetime) the date offsets were taken from."""
    km = key_map()
    stats = {"restored": 0, "unresolved": 0, "dates": 0, "keys": len(km)}

    def sub_tok(m):
        v = km.get(m.group(0))
        stats["restored" if v is not None else "unresolved"] += 1
        return v if v is not None else m.group(0)

    def sub_off(m):
        stats["dates"] += 1
        return (anchor + timedelta(days=int(m.group(1)))).strftime("%m/%d/%Y")

    def text(v):
        return DOFF.sub(sub_off, TOKEN.sub(sub_tok, v)) if isinstance(v, str) else v

    xl = pd.ExcelFile(io.BytesIO(raw))
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as w:
        for name in xl.sheet_names:
            df = pd.read_excel(xl, name)
            for c in list(df.columns):
                if str(c).endswith(DAYS_SUFFIX) and pd.api.types.is_numeric_dtype(df[c]):
                    df[c] = df[c].map(lambda d: (anchor + timedelta(days=int(d))).strftime("%m/%d/%Y") if pd.notna(d) else d)
                    stats["dates"] += int(df[c].notna().sum())
                    df = df.rename(columns={c: str(c)[: -len(DAYS_SUFFIX)]})
                elif not pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_datetime64_any_dtype(df[c]):
                    df[c] = df[c].map(text)
            df.to_excel(w, sheet_name=name[:31], index=False)
    return buf.getvalue(), stats
