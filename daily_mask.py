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
import hashlib
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
import mask_columns_logic as ml
import phi_scrub as ps
import phiscan

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
PROFILE_FILE = hm.CONFIG_DIR / "column_profiles.json"  # one saved config per file type (headers + roles only, no data)
ALLOW_COLS = ["Payer", "Payer Name", "Insurance Group", "Client", "Client Name", "Location", "Facility"]  # not PHI: never masked as people
SKIP_KINDS = {"NUM", "CLM", "AGT", "EMP", "BIL", "ACC", "USR", "PRV", "MRN", "PAT", "PHI"}  # column values: not swept as text
NAME_KINDS = {"PAT", "PHI"}  # ...except names, which are also masked wherever they appear inside a sentence


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


def newest_sources():
    """Source files newest first; copies marked (OLD) and Excel lock files are skipped."""
    fs = [f for f in SOURCE_DIR.glob(PATTERN) if not f.name.startswith("~$") and not re.search(r"\bold\b", f.stem, re.I)] \
        if SOURCE_DIR.exists() else []
    return sorted(fs, key=lambda p: p.stat().st_mtime, reverse=True)


def pick_sources(args):
    if args:
        return [Path(p) for p in args]
    files = newest_sources()
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
                                           text=p.get("text", []), drop=p.get("drop", []), phi=p.get("phi", []), marked=True)
                for p in json.loads(PROFILE_FILE.read_text(encoding="utf-8")).get("profiles", [])}
    return {}


def config_name(filename, cols):
    """One config per header set: 'AR Audit Result 2026-10-23.xlsx' and next week's file share a name."""
    label = re.sub(r"[\s_-]*\(?\d[\d\s()_-]*\)?(?:\(old\))?$", "", Path(str(filename)).stem, flags=re.I)
    label = re.sub(r"\d{4}-\d{2}-\d{2}", "", label).strip(" _-") or "Sheet"
    sig = hashlib.sha1("|".join(sorted(hm.norm(c) for c in cols)).encode()).hexdigest()[:6]
    return re.sub(r"\W+", "_", label)[:30] + "_" + sig


def profile_from_roles(name, roles):
    """The whole header set identifies the report type, so each of your file types keeps its own config."""
    return {"name": name, "match": list(roles),
            "phi": [c for c, r in roles.items() if r == "PHI"],
            "tokens": {c: hm.kind_for(c, ml.guess_kind) for c, r in roles.items() if r == "Identifier"},
            "dates": [c for c, r in roles.items() if r == "Date"],
            "text": [c for c, r in roles.items() if r == "Text"],
            "drop": [c for c, r in roles.items() if r == "Drop"]}


def save_profile(profile):
    data = json.loads(PROFILE_FILE.read_text(encoding="utf-8")) if PROFILE_FILE.exists() else {"profiles": []}
    data["profiles"] = [p for p in data["profiles"] if p["name"] != profile["name"]] + [profile]
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def detect_profile(cols):
    """A config you saved wins; otherwise the built-in sheet types."""
    allp = {**saved_profiles(), **PROFILES}
    fits = [(len(p["detect"]), name) for name, p in allp.items() if p["detect"] <= set(cols)]
    if fits:
        return max(fits)[1]  # the most specific config wins (your saved configs list every header)
    if hm.coverage(cols) >= hm.COVERAGE:  # a new sheet made mostly of headers you have already decided on
        return "memory"
    return None


def builtin_profile(cols):
    """Built-in sheet type only (inventory / wfm / audit), ignoring saved column marks."""
    for name, p in PROFILES.items():
        if p["detect"] <= set(cols):
            return name
    return None


def suggested(cols, df=None):
    """Header memory first, then a guess from the header name and (when given) the values."""
    guess = (lambda c: ml.guess_role_data(c, df[c])) if df is not None else ml.guess_role
    return hm.profile_from_memory(cols, guess, ml.guess_kind)


def get_profile(name, cols=None, df=None):
    if name in ("memory", "suggest"):
        return suggested(cols, df)[0]
    return {**saved_profiles(), **PROFILES}[name]


def find_header_row(xl, sheet, scan=15):
    """Report exports often put a title and blank rows above the real header row: take the first row with the most
    text cells (Worked Claims: title, blank, then the headers on row 3)."""
    top = pd.read_excel(xl, sheet, header=None, nrows=scan)
    best, best_n = 0, 0
    for i, row in top.iterrows():
        n = sum(isinstance(v, str) and v.strip() != "" for v in row)
        if n > best_n:
            best, best_n = i, n
    return best


def read_sheet(xl, sheet, nrows=None):
    return pd.read_excel(xl, sheet, header=find_header_row(xl, sheet), nrows=nrows)


def load_sheets(raw, suggest_all=False):
    """Every sheet -> [(sheet name, profile, DataFrame)]. A sheet without saved tags uses the suggested tags when
    suggest_all (the UI shows them first); a headless run skips it unless header memory covers it."""
    xl = pd.ExcelFile(io.BytesIO(raw))
    found = []
    for name in xl.sheet_names:
        df = read_sheet(xl, name)
        prof = detect_profile(df.columns)
        if suggest_all and (prof is None or prof in PROFILES):
            prof = "suggest"
        if prof:
            found.append((name, prof, df))
        else:
            print(f"  skipped sheet '{name}': no tags for its headers yet (tag them in the UI, or run with --suggest)")
    if not found:
        raise SystemExit(f"No sheet has saved tags: {xl.sheet_names}. Tag its headers in the UI (run_daily.bat) or use --suggest.")
    return found


def name_variants(orig):
    """'MORENO, PABLA M' -> full value, 'PABLA M MORENO', and each name part of 4+ letters."""
    out = {orig}
    parts = [p for p in re.split(r"[,\s]+", orig) if p]
    if "," in orig and len(parts) >= 2:
        out.add(" ".join(parts[1:] + parts[:1]))
    out.update(p for p in parts if len(p) >= 4 and p.isalpha())
    return out


def sweep(series):
    """A value masked once is masked everywhere it appears (longest first, never inside tokens, any capitalisation)."""
    m = {}
    for tok, kind, orig in ps.key_rows:
        if kind not in SKIP_KINDS and len(orig) >= 4:
            m[orig.lower()] = tok
    for tok, kind, orig in ps.key_rows:
        if kind in NAME_KINDS:  # "MORENO, PABLA" in the name column also catches "Pabla Moreno" / "Pabla" in notes
            for v in name_variants(orig):
                m.setdefault(v.lower(), tok if tok.startswith("[") else ps.star(v))
    if not m:
        return series
    rx = re.compile(r"(?<![\w\[])(" + "|".join(re.escape(n) for n in sorted(m, key=len, reverse=True)) + r")(?![\w\]])", re.I)
    return series.map(lambda t: rx.sub(lambda g: m[g.group(1).lower()], t) if t else t)


def mask(df, prof, anchor, use_presidio):
    p = get_profile(prof, list(df.columns), df)
    fixed_drops = ps.DROP_COLS if prof in PROFILES else []  # your own config decides drops for everything else
    out = df.drop(columns=[c for c in fixed_drops + p.get("drop", []) if c in df.columns]).copy()
    for col in p.get("phi", []):  # whole value starred: SMITH, JOHN -> S***H, J**N
        if col in out.columns:
            out[col] = df[col].map(lambda v: ps.token("PHI", v, "star") if pd.notna(v) and str(v).strip() else v)
    for col, kind in p["tokens"].items():  # stable token you can group on and restore exactly
        if col in out.columns:
            out[col] = df[col].map(lambda v, k=kind: ps.token(k, v, "token") if pd.notna(v) else v)
    worked = pd.to_datetime(df["Worked Date"]) if "Worked Date" in df.columns else pd.Series(anchor, index=df.index)
    for c in p["dates"]:
        if c in df.columns:
            out[c] = (pd.to_datetime(df[c], errors="coerce") - worked.dt.normalize()).dt.days
            out = out.rename(columns={c: f"{c} (days vs worked)"})
    if prof in ("memory", "suggest"):
        guessed = suggested(list(df.columns), df)[1]
        if guessed:
            print(f"  NOTE: '{', '.join(map(str, guessed))}' used suggested tags (not tagged before; review them in the UI)")
    text_cols = [c for c in p["text"] if c in df.columns]
    ps.VOCAB = sheet_vocab(df, p)
    if use_presidio and text_cols:
        import presidio_layer as pl
        pl.allow(v for c in ALLOW_COLS if c in df.columns for v in df[c].dropna().astype(str).unique())
    for c in text_cols:
        vals = [ps.scrub_note(n if pd.notna(n) else "", w) for n, w in zip(df[c], worked)]
        if use_presidio:
            vals = [pl.presidio_scrub(v) for v in vals]
        out[c] = sweep(pd.Series(vals, index=df.index))
    risk = pd.Series("", index=df.index)
    for c in text_cols:
        r = out[c].map(ps.residual)
        risk = risk.where(r == "", r)
    out["Residual Risk"] = risk
    # a column left as No PHI that still holds something the masking rules would mask -> warn (never changed here)
    handled = set(p["tokens"]) | set(p["dates"]) | set(p["text"]) | set(p.get("drop", [])) | set(p.get("phi", []))
    untagged = [c for c in df.columns if c not in handled and c in out.columns]
    cand = vocab_candidates(df, p)
    out.attrs["phi_warnings"] = phiscan.scan_columns(
        df, untagged, anchor, {c: set().union(*[w for k, w in cand.items() if k != c]) for c in untagged})
    return out


def vocab_candidates(df, p):
    """{column: words} for this sheet's No PHI category-style columns (Last Resolution Action, Status Source, RCA...).
    Phrases built only from these words are statuses, not people, so the name rules leave them alone."""
    tagged = set(p["tokens"]) | set(p["dates"]) | set(p["text"]) | set(p.get("phi", [])) | set(p.get("drop", []))
    cand = {}
    for c in df.columns:
        s = df[c].dropna()
        if c in tagged or s.empty or not s.map(lambda v: isinstance(v, str)).all():
            continue
        if s.nunique() <= max(60, 0.05 * len(s)):
            cand[c] = {w.lower() for v in s.unique() for w in re.findall(r"[A-Za-z]{2,}", v)}
    return cand


def sheet_vocab(df, p):
    return set().union(*vocab_candidates(df, p).values())


def process(raw, filename, anchor, use_presidio=True, suggest_all=False):
    """Mask every matching sheet of a workbook given as bytes. Returns the masked file's path.
    `raw` is never written anywhere; only masked output, the quarantine file and the private key are."""
    ps.SALT = private_setup()
    MASKED_DIR.mkdir(parents=True, exist_ok=True)
    sheets = load_sheets(raw, suggest_all)
    stem = re.sub(r"\s*\(\d+\)$", "", Path(filename).stem)
    safe_path = MASKED_DIR / f"{stem} - MASKED.xlsx"
    ps.key_rows.clear()
    warnings = []
    with pd.ExcelWriter(safe_path) as ws, pd.ExcelWriter(MASKED_DIR / f"{stem} - QUARANTINE.xlsx") as wq:
        for name, prof, df in sheets:
            out = mask(df, prof, anchor, use_presidio)
            for w in out.attrs.get("phi_warnings", []):
                warnings.append({"Sheet": name, "Column": w["column"], "Found (cells)": w["found"],
                                 "Cells checked": w["cells"], "Tagged as": "No PHI", "Suggested tag": w["suggest"]})
                print(f"  WARNING: {name} | {w['column']} | {w['found']} | {w['suggest']}")
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
        if warnings:  # the flag travels with the file: anyone opening the masked workbook sees which columns to double-check
            pd.DataFrame(warnings).to_excel(ws, sheet_name="PHI Warnings", index=False)
    if warnings:
        print(f"  {len(warnings)} column(s) tagged No PHI still look sensitive: see the PHI Warnings sheet")
    key = pd.DataFrame(ps.key_rows, columns=["Token", "Type", "Original"]).drop_duplicates(["Token", "Original"])
    key.to_excel(PRIVATE_DIR / f"{stem} - REID KEY {datetime.now():%Y%m%d-%H%M%S}.xlsx", index=False)
    print(f"masked -> {safe_path} | tokens {len(key)}")
    return safe_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook", nargs="*")
    ap.add_argument("--date")
    ap.add_argument("--no-presidio", action="store_true")
    ap.add_argument("--suggest", action="store_true", help="mask sheets you have not tagged with the suggested tags")
    a = ap.parse_args()
    for src in pick_sources(a.workbook):  # source stays in memory
        process(src.read_bytes(), src.name, anchor_date(src, a.date), not a.no_presidio, a.suggest)


if __name__ == "__main__":
    main()
