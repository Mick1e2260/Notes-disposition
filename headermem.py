"""
Header memory: remembers what you decided for each column header, across all sheets and report types.
Stored in header_memory.json (headers, roles and token kinds only, never cell data).

  - saving marks or running in the UI calls remember()
  - a new sheet pre-fills every header it already knows and flags only the unknown ones for review
  - daily_mask.py can mask a brand-new sheet with no UI when the memory covers most of its headers
Headers are matched after normalising ('Patient  Name', 'patient_name' and 'PATIENT NAME' are one header).
"""
import json
import re
from datetime import datetime
from pathlib import Path

FILE = Path(__file__).with_name("header_memory.json")  # ar_audit.py points this next to the exe when frozen
COVERAGE = 0.7  # share of a sheet's headers that must be remembered before a headless run trusts the memory
_cache = {"mtime": None, "data": {}}


def norm(h):
    return re.sub(r"[^a-z0-9]+", " ", str(h).lower()).strip()


def _data():
    if not FILE.exists():
        _cache.update(mtime=None, data=_seed())
        return _cache["data"]
    m = FILE.stat().st_mtime
    if _cache["mtime"] != m:
        _cache.update(mtime=m, data=json.loads(FILE.read_text(encoding="utf-8")).get("headers", {}))
    return _cache["data"]


def _seed():
    """First use: learn from any column profiles saved before header memory existed (marked columns only)."""
    pf = FILE.with_name("column_profiles.json")
    out = {}
    if pf.exists():
        for p in json.loads(pf.read_text(encoding="utf-8")).get("profiles", []):
            for role, cols in (("Date", p.get("dates", [])), ("Free text", p.get("text", [])), ("Drop", p.get("drop", []))):
                for c in cols:
                    out[norm(c)] = {"name": c, "role": role, "seen": 1}
            for c, k in p.get("tokens", {}).items():
                out[norm(c)] = {"name": c, "role": "Identifier", "kind": k, "seen": 1}
    return out


def lookup(col):
    return _data().get(norm(col))


def remember(roles, kind_for=None):
    """roles: {header: role}. Records every header so the next sheet starts from these decisions."""
    d = dict(_data())
    for c, r in roles.items():
        e = d.get(norm(c), {})
        d[norm(c)] = {"name": str(c), "role": r, "seen": e.get("seen", 0) + 1, "last": datetime.now().strftime("%Y-%m-%d"),
                      **({"kind": (kind_for(c) if kind_for else e.get("kind", "ID"))} if r == "Identifier" else {})}
    FILE.write_text(json.dumps({"headers": d}, indent=1), encoding="utf-8")
    _cache["mtime"] = None
    return len(d)


def coverage(cols):
    cols = list(cols)
    return sum(lookup(c) is not None for c in cols) / len(cols) if cols else 0


def role_map(cols, guess_role, guess_kind):
    """-> (roles, sources). Source is 'remembered' (you decided it before) or 'guessed' (new header, please review)."""
    roles, src = {}, {}
    for c in cols:
        e = lookup(c)
        roles[c], src[c] = (e["role"], "remembered") if e else (guess_role(c), "guessed")
    return roles, src


def kind_for(col, guess_kind):
    e = lookup(col)
    return e.get("kind") if e and e.get("kind") else guess_kind(col)


def profile_from_memory(cols, guess_role, guess_kind):
    """Ad-hoc profile for a sheet: remembered roles where known, header-name guesses for the rest. Returns (profile, guessed)."""
    roles, src = role_map(cols, guess_role, guess_kind)
    prof = {"detect": set(cols), "marked": False,
            "tokens": {c: kind_for(c, guess_kind) for c, r in roles.items() if r == "Identifier"},
            "dates": [c for c, r in roles.items() if r == "Date"],
            "text": [c for c, r in roles.items() if r == "Free text"],
            "drop": [c for c, r in roles.items() if r == "Drop"]}
    return prof, [c for c, s in src.items() if s == "guessed"]
