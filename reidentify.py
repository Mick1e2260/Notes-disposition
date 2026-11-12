"""
Restore a masked workbook to its original values. Local only.

Every '... - REID KEY ....xlsx' in PRIVATE_DIR is used (the salt is stable, so keys from earlier days still resolve).
  - Identifier tokens ([CLM-AB12CD]) come back exactly.
  - Starred values (PHI columns, and names/numbers inside Text) come back only when the mask is unambiguous:
    John and Joan both star to J**n, so J**n is left as it is rather than restored to the wrong person.
  - Day offsets become dates again: 'X (days vs worked)' columns become 'X' (MM/DD/YYYY).

What cannot come back: dropped columns, rows that went to QUARANTINE, ambiguous starred values, and original
date formats (dates return as MM/DD/YYYY).
The restored workbook contains PHI. It is built in memory and handed straight to the person who needs it; never stored.
"""
import io
import re
from datetime import timedelta

import pandas as pd

import daily_mask as dm

TOKEN = re.compile(r"\[[A-Z]+-[0-9A-F]{6}\]")
DAYS_SUFFIX = " (days vs worked)"


def key_map():
    """-> (exact: {mask: original}, ambiguous: set of masks that stand for more than one original)."""
    seen = {}
    for f in sorted(dm.PRIVATE_DIR.glob("* - REID KEY *.xlsx"), key=lambda p: p.stat().st_mtime):
        k = pd.read_excel(f)
        for t, o in zip(k["Token"].astype(str), k["Original"].astype(str)):
            seen.setdefault(t, set()).add(o)
    exact = {t: next(iter(o)) for t, o in seen.items() if len(o) == 1}
    return exact, {t for t, o in seen.items() if len(o) > 1}


def restore_bytes(raw, anchor):
    """Return (xlsx bytes, stats). `anchor` is the work date (datetime) the day offsets were taken from."""
    exact, ambiguous = key_map()
    stats = {"restored": 0, "unresolved": 0, "ambiguous": 0, "dates": 0, "keys": len(exact)}
    starred = sorted((t for t in exact if "*" in t), key=len, reverse=True)
    star_rx = re.compile(r"(?<![\w*])(" + "|".join(map(re.escape, starred)) + r")(?![\w*])") if starred else None
    amb_rx = re.compile(r"(?<![\w*])(" + "|".join(map(re.escape, sorted(ambiguous, key=len, reverse=True))) + r")(?![\w*])") \
        if ambiguous else None

    def sub_tok(m):
        v = exact.get(m.group(0))
        stats["restored" if v is not None else "unresolved"] += 1
        return v if v is not None else m.group(0)

    def sub_star(m):
        stats["restored"] += 1
        return exact[m.group(1)]

    def text(v):
        if not isinstance(v, str):
            return v
        v = TOKEN.sub(sub_tok, v)
        if amb_rx is not None:
            stats["ambiguous"] += len(amb_rx.findall(v))
        if star_rx is not None:
            v = star_rx.sub(sub_star, v)
        return v

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
