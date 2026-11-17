"""
Warn when a column tagged No PHI holds something the masking rules would have masked.

Instead of keeping a second list of patterns, each value is run through phi_scrub.scrub_note (the same rules that mask a
Text column: names after "spoke with", phones, emails, SSNs, addresses, reference and long numbers, dates of birth) and the
column is flagged if the masker would have changed anything. When the masking rules change, this check follows them.

Nothing is changed or stored. Results are counts and value *shapes* (999-999-9999), never the values themselves.
Columns with few distinct values (statuses, codes) are checked once per distinct value, so a big sheet stays fast.
"""
import re
from collections import Counter

import pandas as pd

import phi_scrub as ps

LIMIT = 3000  # distinct values checked per column
CAT = {"NAME": "name", "EMAIL": "email", "URL": "url", "SSN": "ssn", "PHONE": "phone", "ADDR": "address", "ZIP": "address",
       "REF": "id", "ID": "id", "NUM": "id", "DOB": "dob"}
LABEL = {"name": "person names", "email": "emails", "url": "links", "ssn": "SSN-like numbers", "phone": "phone numbers",
         "address": "addresses", "id": "IDs / long numbers", "dob": "dates of birth"}
PHI_FIRST = ("ssn", "phone", "email", "address", "name", "dob")


def shape(v):
    return re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", str(v)[:24])))


def suggest(hits, avg_len):
    """A long sentence -> Text (keep the wording, mask the parts); a short value -> PHI or Identifier."""
    if not hits:
        return None
    if avg_len >= 40:
        return "Text"
    if any(k in hits for k in PHI_FIRST):
        return "PHI"
    return "Identifier" if "id" in hits else "Text"


def scan_series(series, anchor, vocab=None, limit=LIMIT):
    """-> {"cells", "avg_len", "hits": {category: cells}, "shapes": {category: [[shape, n], ...]}, "suggest"}.
    `vocab`: this sheet's status/action words (without this column's own), so "Appeal Submitted" is not a person."""
    res = {"cells": 0, "avg_len": 0, "hits": {}, "shapes": {}, "suggest": None}
    s = series.dropna()
    if s.empty or pd.api.types.is_bool_dtype(s) or pd.api.types.is_datetime64_any_dtype(s):
        return res
    res["cells"] = int(len(s))
    if pd.api.types.is_numeric_dtype(s):
        # whole numbers of 7+ digits look like IDs; money, counts and rates do not
        whole = s[s.mod(1) == 0]
        big = int((whole.abs() >= 1_000_000).sum())
        if big >= 3 and big >= 0.5 * len(s):
            res["hits"] = {"id": big}
            res["shapes"] = {"id": [[shape(int(whole.abs().iloc[0])), big]]}
            res["suggest"] = "Identifier"
        return res

    txt = s.astype(str)
    res["avg_len"] = int(txt.str.len().mean())
    vc = txt.value_counts()
    uniq = vc.index if len(vc) <= limit else vc.sample(limit, random_state=7).index
    res["cells"] = int(vc[uniq].sum())  # the cells those values stand for, so the counts stay honest
    hits, shapes = Counter(), {}
    old_vocab, k0 = ps.VOCAB, len(ps.key_rows)
    ps.VOCAB = vocab or set()
    try:
        for v in uniq:
            n0 = len(ps.key_rows)
            ps.scrub_note(v, anchor)
            if len(ps.key_rows) == n0:
                continue
            w, seen = int(vc[v]), set()
            for _tok, kind, orig in ps.key_rows[n0:]:
                cat = CAT.get(kind)
                if cat is None:
                    continue
                if cat not in seen:
                    hits[cat] += w
                    seen.add(cat)
                shapes.setdefault(cat, Counter())[shape(orig)] += w
    finally:
        del ps.key_rows[k0:]  # a scan must never leak into a real run's key
        ps.VOCAB = old_vocab
    res["hits"] = dict(hits)
    res["shapes"] = {c: [[k, int(n)] for k, n in cnt.most_common(2)] for c, cnt in shapes.items()}
    res["suggest"] = suggest(res["hits"], res["avg_len"])
    return res


def describe(res):
    """'12 phone numbers, 3 emails (e.g. 999-999-9999)'"""
    parts = [f"{n} {LABEL[c]}" for c, n in sorted(res["hits"].items(), key=lambda x: -x[1])]
    top = max(res["hits"], key=res["hits"].get) if res["hits"] else None
    eg = res["shapes"].get(top, [[""]])[0][0] if top else ""
    return ", ".join(parts) + (f" (e.g. {eg})" if eg else "")


def scan_columns(df, cols, anchor, vocab_by_col=None):
    """Scan the columns tagged No PHI. -> [{"column", "cells", "found", "hits", "suggest"}] for the ones that are flagged."""
    out = []
    for c in cols:
        r = scan_series(df[c], anchor, (vocab_by_col or {}).get(c))
        if r["hits"]:
            out.append({"column": str(c), "cells": r["cells"], "found": describe(r), "hits": r["hits"], "suggest": r["suggest"]})
    return out
