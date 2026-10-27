"""
PHI mask for the AR Excel sheets (Open AR inventory, WFM aging, AR audit result). Run BEFORE anything touches an LLM.

Usage:  python daily_mask.py [workbook.xlsx ...] [--date YYYY-MM-DD] [--no-presidio]
        (no path -> newest 'AR Audit Result*.xlsx' in SOURCE_DIR)

Each sheet's type is detected from its headers (see PROFILES). Per profile:
  1. identity columns -> stable salted tokens; staff/provider columns too
  2. date columns -> day offsets from the work/anchor date
  3. free-text columns -> phi_scrub regex rules, then Presidio + NER (presidio_layer.py), then a sweep that masks every
     value found anywhere (incl. patient names from the name column) in every other text cell
  4. residual-risk scan; risky rows go to QUARANTINE (never sent anywhere)

Guarantees
  - The source workbook is read into memory only; never copied, moved, modified or deleted.
  - Only masked sheets are written (MASKED_DIR). The salt and the token -> value key live in PRIVATE_DIR
    (per-user ACL, outside the repo and OneDrive). The run log holds counts only.
Pattern + NER masking is not proof of de-identification: compliance has to approve the method.
"""
import argparse
import io
import json
import os
import re
import secrets
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd

import headermem as hm
import phi_scrub as ps

SOURCE_DIR = Path(os.environ.get("AR_AUDIT_SOURCE_DIR", r"C:\Users\mick.harper\OneDrive - Anka"))
MASKED_DIR = Path(os.environ.get("AR_AUDIT_MASKED_DIR", r"C:\AI_Agents\Notes-Disposition-Automation\masked_out"))
PRIVATE_DIR = Path(os.environ.get("AR_AUDIT_PRIVATE_DIR", Path(os.environ["LOCALAPPDATA"]) / "AnkaARAudit" / "private"))
PATTERN = "AR Audit Result*.xlsx"

INV_TEXT = ["Notes", "Denial Description", "Denial Reason", "Rejection Reason", "Adjustment Reason"]
PROFILES = {
    "inventory": dict(  # Open AR inventory report
        detect={"Claim Number", "Patient MRN", "Notes"},
        tokens={"Claim Number": "CLM", "Bill ID": "BIL", "Patient Name": "PAT", "Patient MRN": "MRN",
                "Primary Provider": "PRV", "Assigned User": "USR", "Submitted By": "USR"},
        dates=["Date of Service", "Billed Date", "Latest Denial Date", "Latest Rejection Date", "Bill Follow-Up Date",
               "Next Follow-Up Date", "Due Date", "Processed Date (API)", "Assigned On", "Submitted On"],
        text=INV_TEXT),
    "wfm": dict(  # WFM aging report (all structured, no free text)
        detect={"Claim ID", "Account #", "0-30 Sum"},
        tokens={"Claim ID": "CLM", "Account #": "ACC", "Patient Name": "PAT", "Responsible Party": "PAT",
                "Billing Pro Name": "PRV"},
        dates=["Claim Date", "Cut-off Date"], text=[]),
    "audit": dict(  # output of ar_auto_audit.py
        detect={ps.NOTES, "Unique ID"},
        tokens={"Unique ID": "CLM", "Agent": "AGT", "Emp ID": "EMP"},
        dates=ps.DATE_COLS, text=[ps.NOTES]),
}
PROFILE_FILE = Path(__file__).with_name("column_profiles.json")  # written by mask_columns.py (headers + roles only, no data)
ALLOW_COLS = ["Payer", "Payer Name", "Insurance Group", "Client", "Client Name", "Location", "Facility"]  # not PHI: never masked as people
SKIP_KINDS = {"CLM", "AGT", "EMP", "BIL", "ACC", "USR", "PRV", "MRN", "PAT"}  # structured-column tokens (not swept as text)


def private_setup():
    """Create the private dir (current user only) and return a persistent salt."""
    first = not PRIVATE_DIR.exists()
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    if first:
        subprocess.run(["icacls", str(PRIVATE_DIR), "/inheritance:r", "/grant:r",
                        f"{os.environ['USERNAME']}:(OI)(CI)F"], capture_output=True)
    salt_file = PRIVATE_DIR / "salt.txt"
    if not salt_file.exists():
        salt_file.write_text(secrets.token_hex(24))
    return salt_file.read_text().strip()


def pick_sources(args):
    if args:
        return [Path(p) for p in args]
    files = sorted(SOURCE_DIR.glob(PATTERN), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit(f"No '{PATTERN}' in {SOURCE_DIR}")
    return files[:1]


def anchor_date(path, override):
    if override:
        return datetime.strptime(override, "%Y-%m-%d")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", path.name)
    return datetime(*map(int, m.groups())) if m else datetime.today()


def saved_profiles():
    if PROFILE_FILE.exists():
        return {f"saved:{p['name']}": dict(detect=set(p["match"]), tokens=p.get("tokens", {}), dates=p.get("dates", []),
                                           text=p.get("text", []), drop=p.get("drop", []), marked=True)
                for p in json.loads(PROFILE_FILE.read_text(encoding="utf-8")).get("profiles", [])}
    return {}


def detect_profile(cols):
    """A profile you marked in mask_columns.py wins; otherwise the built-in sheet types."""
    allp = {**saved_profiles(), **PROFILES}
    for name, p in allp.items():
        if p["detect"] <= set(cols):
            return name
    if hm.coverage(cols) >= hm.COVERAGE:  # a new sheet made mostly of headers you have already decided on
        return "memory"
    return None


def builtin_profile(cols):
    """Built-in sheet type only (inventory / wfm / audit), ignoring saved column marks."""
    for name, p in PROFILES.items():
        if p["detect"] <= set(cols):
            return name
    return None


def get_profile(name, cols=None):
    if name == "memory":
        import mask_columns_logic as mcl
        return hm.profile_from_memory(cols, mcl.guess_role, mcl.guess_kind)[0]
    return {**saved_profiles(), **PROFILES}[name]


def load_sheets(raw):
    """Every sheet whose headers match a profile -> [(sheet name, profile, DataFrame)]."""
    xl = pd.ExcelFile(io.BytesIO(raw))
    found = []
    for name in xl.sheet_names:
        df = pd.read_excel(xl, name)
        prof = detect_profile(df.columns)
        if prof:
            found.append((name, prof, df))
    if not found:
        raise SystemExit(f"No sheet matches a saved or built-in profile: {xl.sheet_names}. "
                         "Run `python mask_columns.py` to mark which columns contain PHI.")
    return found


def name_variants(orig):
    """'MORENO, PABLA M' -> full value, 'PABLA M MORENO', and each name part of 4+ letters."""
    out = {orig}
    parts = [p for p in re.split(r"[,\s]+", orig) if p]
    if "," in orig and len(parts) >= 2:
        out.add(" ".join(parts[1:] + parts[:1]))
    out.update(p for p in parts if len(p) >= 4 and p.isalpha())
    return out


def sweep(series, extra_names=None):
    """A value masked once is masked everywhere it appears (longest first, never inside tokens, any capitalisation)."""
    m = {}
    for tok, kind, orig in ps.key_rows:
        if kind not in SKIP_KINDS and len(orig) >= 4:
            m[orig.lower()] = tok
    for tok, kind, orig in ps.key_rows:
        if kind == "PAT":
            for v in name_variants(orig):
                m.setdefault(v.lower(), tok)
    if not m:
        return series
    rx = re.compile(r"(?<![\w\[])(" + "|".join(re.escape(n) for n in sorted(m, key=len, reverse=True)) + r")(?![\w\]])", re.I)
    return series.map(lambda t: rx.sub(lambda g: m[g.group(1).lower()], t) if t else t)


def mask(df, prof, anchor, use_presidio):
    p = get_profile(prof, list(df.columns))
    out = df.drop(columns=[c for c in ps.DROP_COLS + p.get("drop", []) if c in df.columns]).copy()
    for col, kind in p["tokens"].items():
        if col in out.columns:
            out[col] = df[col].map(lambda v, k=kind: ps.token(k, v) if pd.notna(v) else v)
    worked = pd.to_datetime(df["Worked Date"]) if "Worked Date" in df.columns else pd.Series(anchor, index=df.index)
    for c in p["dates"]:
        if c in df.columns:
            out[c] = (pd.to_datetime(df[c], errors="coerce") - worked.dt.normalize()).dt.days
            out = out.rename(columns={c: f"{c} (days vs worked)"})
    if prof == "memory":
        import mask_columns_logic as mcl
        guessed = hm.profile_from_memory(list(df.columns), mcl.guess_role, mcl.guess_kind)[1]
        if guessed:
            print(f"  NOTE: headers not in memory, role guessed from the name (review in the UI): {', '.join(map(str, guessed))}")
    text_cols = [c for c in p["text"] if c in df.columns]
    if use_presidio and text_cols:
        import presidio_layer as pl
        pl.allow(v for c in ALLOW_COLS if c in df.columns for v in df[c].dropna().astype(str).unique())
    for c in text_cols:
        vals = [ps.scrub_note(n if pd.notna(n) else "", w) for n, w in zip(df[c], worked)]
        if use_presidio:
            vals = [pl.presidio_scrub(v) for v in vals]
        out[c] = sweep(pd.Series(vals, index=df.index), None)
    risk = pd.Series("", index=df.index)
    for c in text_cols:
        r = out[c].map(ps.residual)
        risk = risk.where(r == "", r)
    out["Residual Risk"] = risk
    if p.get("marked"):
        handled = set(p["tokens"]) | set(p["dates"]) | set(p["text"]) | set(p.get("drop", []))
        warn_unmarked(df, [c for c in df.columns if c not in handled])
    return out


SENSITIVE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|[\w.+-]+@[\w-]+\.[\w.]+|\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")


def warn_unmarked(df, cols):
    """You told us these columns are not PHI and they are left untouched; flag any that look like it anyway."""
    for c in cols:
        if df[c].dtype == object:
            n = int(df[c].dropna().astype(str).str.contains(SENSITIVE).sum())
            if n:
                print(f"  WARNING: unmarked column '{c}' has {n} cells that look like SSN/phone/email. "
                      "Mark it in mask_columns.py if it holds PHI.")


def process(raw, filename, anchor, use_presidio=True):
    """Mask every matching sheet of a workbook given as bytes. Returns the masked file's path.
    `raw` is never written anywhere; only masked output, the quarantine file and the private key are."""
    ps.SALT = private_setup()
    MASKED_DIR.mkdir(parents=True, exist_ok=True)
    sheets = load_sheets(raw)
    stem = re.sub(r"\s*\(\d+\)$", "", Path(filename).stem)
    safe_path = MASKED_DIR / f"{stem} - MASKED.xlsx"
    ps.key_rows.clear()
    with pd.ExcelWriter(safe_path) as ws, pd.ExcelWriter(MASKED_DIR / f"{stem} - QUARANTINE.xlsx") as wq:
        for name, prof, df in sheets:
            out = mask(df, prof, anchor, use_presidio)
            safe = out[out["Residual Risk"] == ""].drop(columns="Residual Risk")
            quar = out[out["Residual Risk"] != ""]
            safe.to_excel(ws, sheet_name=name[:31], index=False)
            quar.to_excel(wq, sheet_name=name[:31], index=False)
            line = (f"{datetime.now():%Y-%m-%d %H:%M} | {filename} | '{name}' ({prof}) | rows {len(df)} | "
                    f"safe {len(safe)} | quarantined {len(quar)} | presidio {use_presidio}")
            with open(PRIVATE_DIR / "run_log.txt", "a") as f:
                f.write(line + chr(10))
            print(line)
            if len(quar):
                print(quar["Residual Risk"].str.split(", ").explode().value_counts().to_string())
    key = pd.DataFrame(ps.key_rows, columns=["Token", "Type", "Original"]).drop_duplicates("Token")
    key.to_excel(PRIVATE_DIR / f"{stem} - REID KEY {datetime.now():%Y%m%d-%H%M%S}.xlsx", index=False)
    print(f"masked -> {safe_path} | tokens {len(key)}")
    return safe_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook", nargs="*")
    ap.add_argument("--date")
    ap.add_argument("--no-presidio", action="store_true")
    a = ap.parse_args()
    for src in pick_sources(a.workbook):
        process(src.read_bytes(), src.name, anchor_date(src, a.date), not a.no_presidio)  # source stays in memory


if __name__ == "__main__":
    main()
