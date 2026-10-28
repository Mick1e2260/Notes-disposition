"""
LLM note-vs-disposition reviewer (local Ollama only). Runs on the MASKED file from daily_mask.py.

Usage:  python llm_audit.py [masked.xlsx] [--model qwen2.5:7b-instruct] [--limit N] [--sample 0.10]
        (no path -> newest '* - MASKED.xlsx' in MASKED_DIR)

Which claims are reviewed: every claim the rules engine flagged on a note-vs-action code
(N01, N03, N04, N05, G01), plus a random --sample of the rest as a control group.

Privacy guards
  - refuses any input that is not a '- MASKED' file; re-runs the residual-PHI scan on every note
    and skips (counts) any note that still looks risky;
  - only talks to a loopback Ollama (127.0.0.1 / localhost); nothing leaves the PC.
Results are advisory: a QA person confirms before agent feedback.
"""
import argparse
import json
import os
import random
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

import phi_scrub as ps

HERE = Path(__file__).parent
MASKED_DIR = Path(os.environ.get("AR_AUDIT_MASKED_DIR", r"C:\AI_Agents\Notes-Disposition-Automation\masked_out"))
HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not HOST.startswith("http"):
    HOST = "http://" + HOST
assert urlparse(HOST).hostname in ("127.0.0.1", "localhost", "::1"), f"Refusing non-local Ollama host: {HOST}"

ACT, NOTES = "Last Resolution Action", ps.NOTES
NOTE_CODES = {"N01", "N03", "N04", "N05", "G01"}
STATUSES = ["Denied", "Rejected", "Paid", "Scheduled to Pay", "In Process", "Acknowledged/Received",
            "Pending - Info Requested", "Not on File", "Claim Edit/Scrubber Edits", "Undetermined"]


def build_schema(actions):
    return {
        "type": "object",
        "properties": {
            "note_status": {"type": "string", "enum": STATUSES},
            "note_action": {"type": "string", "enum": actions + ["Unclear"]},
            "verdict": {"type": "string", "enum": ["Match", "Mismatch", "Unclear"]},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "reason": {"type": "string"},
        },
        "required": ["note_status", "note_action", "verdict", "confidence", "reason"],
    }


def ask(model, system, user, schema):
    body = json.dumps({"model": model, "stream": False, "format": schema, "keep_alive": "30m",
                       "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 200},
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(HOST + "/api/chat", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(json.loads(r.read())["message"]["content"])


def pick_input(arg):
    if arg:
        return Path(arg)
    files = sorted(MASKED_DIR.glob("* - MASKED.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit(f"No '* - MASKED.xlsx' in {MASKED_DIR}. Run daily_mask.py first.")
    return files[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("masked", nargs="?")
    ap.add_argument("--model", default=os.environ.get("AR_AUDIT_MODEL", "qwen2.5:7b-instruct"))
    ap.add_argument("--limit", type=int, default=0, help="max claims to review (0 = all selected)")
    ap.add_argument("--sample", type=float, default=0.10, help="fraction of unflagged claims added as control")
    a = ap.parse_args()

    src = pick_input(a.masked)
    if "MASKED" not in src.name:
        raise SystemExit("Refusing: input must be the '- MASKED' file produced by daily_mask.py")
    # the masked workbook holds every sheet of the audit result; the review needs the line-level findings sheet
    xl = pd.ExcelFile(src)
    need = {"#", "Code", NOTES}
    sheet = next((n for n in xl.sheet_names if need <= set(pd.read_excel(xl, n, nrows=0).columns)), None)
    if sheet is None:
        raise SystemExit(f"No sheet in {src.name} has the columns {sorted(need)} (the audit's line-level findings).")
    df = pd.read_excel(xl, sheet)

    # one row per claim: its codes, fatal flag, and the fields the model needs
    g = df.groupby("#")
    claims = g.agg(codes=("Code", lambda s: sorted(set(s))), fatal=("Severity", lambda s: (s == "Fatal").any()),
                   action=(ACT, "first"), status=("Derived Status", "first"), note=(NOTES, "first"),
                   client=("Client Name", "first"), agent=("Agent", "first"), claim=("Unique ID", "first"),
                   balance=("Balance Amount", "first")).reset_index()
    claims["note"] = claims["note"].fillna("").astype(str)
    claims["rule_flag"] = claims["codes"].map(lambda c: bool(NOTE_CODES & set(c)))

    risky = claims["note"].map(ps.residual) != ""
    flagged = claims[claims.rule_flag & ~risky]
    rest = claims[~claims.rule_flag & ~risky]
    random.seed(7)
    ctrl = rest.sample(frac=min(max(a.sample, 0), 1), random_state=7)
    todo = pd.concat([flagged, ctrl])
    if a.limit:
        todo = todo.head(a.limit)

    system = (HERE / "llm_context.md").read_text(encoding="utf-8")
    actions = sorted({str(x) for x in claims["action"].dropna()})
    schema = build_schema(actions)
    cache_f = MASKED_DIR / f"{src.stem}.llm-cache.jsonl"
    cache = {}
    if cache_f.exists():
        for line in cache_f.read_text().splitlines():
            d = json.loads(line)
            cache[(d["#"], d["model"])] = d

    print(f"{src.name}: {len(claims)} claims | skipped risky {int(risky.sum())} | review {len(todo)} "
          f"({len(flagged)} flagged + {len(ctrl)} control) | model {a.model}")
    rows, t0 = [], time.time()
    with open(cache_f, "a") as cf:
        for i, r in enumerate(todo.itertuples(), 1):
            hit = cache.get((int(r._1), a.model))
            if hit:
                res = hit["res"]
            else:
                user = (f"Allowed actions: {json.dumps(actions)}\n"
                        f"Disposition the agent picked: {r.action}\n"
                        f"Status the rules engine derived: {r.status}\n"
                        f"NOTE:\n{r.note}")
                try:
                    res = ask(a.model, system, user, schema)
                except Exception as e:  # keep going; mark and move on
                    res = {"note_status": "Undetermined", "note_action": "Unclear", "verdict": "Unclear",
                           "confidence": "low", "reason": f"model error: {type(e).__name__}"}
                cf.write(json.dumps({"#": int(r._1), "model": a.model, "res": res}) + "\n")
                cf.flush()
            if res["verdict"] == "Mismatch":
                cmp_ = "Confirmed by LLM" if r.rule_flag else "NEW finding (rules missed)"
            elif res["verdict"] == "Match":
                cmp_ = "Possible false positive" if r.rule_flag else "Agree: clean"
            else:
                cmp_ = "LLM unclear - QA review"
            rows.append({"#": r._1, "Client": r.client, "Claim": r.claim, "Agent": r.agent, "Balance": r.balance,
                         "Disposition": r.action, "Rules Codes": ", ".join(r.codes), "Rules Flag": r.rule_flag,
                         "LLM Note Action": res["note_action"], "LLM Verdict": res["verdict"],
                         "Confidence": res["confidence"], "LLM Reason": res["reason"], "Comparison": cmp_,
                         "Note (masked)": r.note})
            if i % 10 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} | {(time.time() - t0) / i:.1f}s per claim")

    res_df = pd.DataFrame(rows)
    out = MASKED_DIR / f"{src.stem.replace(' - MASKED', '')} - LLM REVIEW.xlsx"
    summ = res_df.groupby(["Rules Flag", "LLM Verdict"]).size().rename("Claims").reset_index()
    comp = res_df["Comparison"].value_counts().rename_axis("Comparison").reset_index(name="Claims")
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        comp.to_excel(w, sheet_name="Summary", index=False)
        summ.to_excel(w, sheet_name="Summary", index=False, startrow=len(comp) + 3)
        pd.DataFrame({"Note": [f"Model {a.model} (local Ollama), run {datetime.now():%Y-%m-%d %H:%M}. Advisory only: "
                               "QA confirms before agent feedback. Input is the masked file only."]}).to_excel(
            w, sheet_name="Summary", index=False, startrow=len(comp) + len(summ) + 7)
        res_df.sort_values(["Comparison", "Confidence"]).to_excel(w, sheet_name="LLM Review", index=False)
        for ws in w.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(60, max(10, max(len(str(c.value or "")) for c in col[:100]) + 2))
            ws.freeze_panes = "A2"
    print(comp.to_string(index=False))
    print("->", out)


if __name__ == "__main__":
    main()
