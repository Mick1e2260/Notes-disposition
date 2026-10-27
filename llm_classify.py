"""
Local-LLM classifier for the MASKED Open AR inventory sheet (Presidio + NER masked upstream, Qwen via Ollama here).

Usage:  python llm_classify.py [masked.xlsx] [--model qwen2.5:7b-instruct] [--limit N] [--all] [--sample 0.25]
        (no path -> newest '* - MASKED.xlsx' in MASKED_DIR that has an inventory sheet)

For each worked claim the model reads the masked Notes (+ denial/rejection text) and predicts
  claim status, action taken, denial category   -- each constrained to the labels that actually exist in the sheet
then the script compares the prediction with what the agent entered (Claim Status / ActionCode / Category).
Output '- CLASSIFIED.xlsx' keeps masked data only. Advisory: QA confirms before agent feedback.
Selection (default): rows whose note is >= 40 chars, a random --sample of them (or --all).
"""
import argparse
import json
import os
import time
from pathlib import Path

import pandas as pd

import daily_mask as dm
import llm_audit as la
import phi_scrub as ps

TEXT = ["Notes", "Denial Description", "Denial Reason", "Rejection Reason", "Adjustment Reason"]
BRIEF = """

## This sheet (Open AR inventory)
Each row is one claim an AR agent worked. The agent recorded a Claim Status, a Category (denial category) and an
ActionCode, and wrote Notes. From the NOTES and denial/rejection text ONLY, predict:
- claim_status: the payer-side status of the claim the note describes
- action: the action the agent concluded this touch (use the ActionCode label set)
- category: the denial category, or "None" if the claim is not denied / not stated
Use only the label sets given in the request. If the note does not support a label, answer "Unclear". Never guess.
"""


def schema(statuses, actions, cats):
    return {"type": "object", "required": ["claim_status", "action", "category", "confidence", "reason"],
            "properties": {"claim_status": {"type": "string", "enum": statuses + ["Unclear"]},
                           "action": {"type": "string", "enum": actions + ["Unclear"]},
                           "category": {"type": "string", "enum": cats + ["None", "Unclear"]},
                           "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                           "reason": {"type": "string"}}}


def labels(s):
    return sorted({str(v).strip() for v in s.dropna() if str(v).strip()})


def agree(pred, actual):
    return "n/a" if pred == "Unclear" or pd.isna(actual) else ("same" if str(actual).strip() == pred else "DIFFERENT")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("masked", nargs="?")
    ap.add_argument("--model", default=os.environ.get("AR_AUDIT_MODEL", "qwen2.5:7b-instruct"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--sample", type=float, default=0.25)
    a = ap.parse_args()

    src = Path(a.masked) if a.masked else None
    if src is None:
        for f in sorted(dm.MASKED_DIR.glob("* - MASKED.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True):
            if any(dm.detect_profile(pd.read_excel(f, sheet_name=n, nrows=0).columns) == "inventory"
                   for n in pd.ExcelFile(f).sheet_names):
                src = f
                break
    if src is None or "MASKED" not in src.name:
        raise SystemExit("Need a '- MASKED' workbook with an inventory sheet (run daily_mask.py first).")
    xl = pd.ExcelFile(src)
    name = next(n for n in xl.sheet_names if dm.detect_profile(pd.read_excel(xl, n, nrows=0).columns) == "inventory")
    df = pd.read_excel(xl, name)

    for c in TEXT:
        if c not in df.columns:
            df[c] = ""
    df[TEXT] = df[TEXT].fillna("").astype(str)
    ok = df["Notes"].str.len() >= 40
    ok &= df[TEXT].apply(lambda col: col.map(ps.residual)).eq("").all(axis=1)  # re-check PHI, skip risky rows
    pool = df[ok]
    todo = pool if a.all else pool.sample(frac=min(max(a.sample, 0), 1), random_state=7)
    if a.limit:
        todo = todo.head(a.limit)

    statuses, actions = labels(df.get("Claim Status", pd.Series(dtype=str))), labels(df.get("ActionCode", pd.Series(dtype=str)))
    cats = labels(df.get("Category", pd.Series(dtype=str)))
    sch = schema(statuses, actions, cats)
    system = (la.HERE / "llm_context.md").read_text(encoding="utf-8") + BRIEF
    cache_f = dm.MASKED_DIR / f"{src.stem}.classify-cache.jsonl"
    cache = {}
    if cache_f.exists():
        for line in cache_f.read_text().splitlines():
            d = json.loads(line)
            cache[(d["row"], d["model"])] = d["res"]
    print(f"{src.name} [{name}]: {len(df)} rows | eligible {len(pool)} | classify {len(todo)} | model {a.model}")

    rows, t0 = [], time.time()
    with open(cache_f, "a") as cf:
        for i, (idx, r) in enumerate(todo.iterrows(), 1):
            res = cache.get((int(idx), a.model))
            if res is None:
                user = (f"Claim Status labels: {json.dumps(statuses)}\nActionCode labels: {json.dumps(actions)}\n"
                        f"Denial Category labels: {json.dumps(cats)}\n\n"
                        + "\n".join(f"{c}: {r[c]}" for c in TEXT if r[c].strip()))
                try:
                    res = la.ask(a.model, system, user, sch)
                except Exception as e:
                    res = {"claim_status": "Unclear", "action": "Unclear", "category": "Unclear",
                           "confidence": "low", "reason": f"model error: {type(e).__name__}"}
                cf.write(json.dumps({"row": int(idx), "model": a.model, "res": res}) + "\n")
                cf.flush()
            rows.append({"Row": idx, "Claim": r.get("Claim Number"), "Client": r.get("Client"), "Payer": r.get("Payer"),
                         "Balance": r.get("Balance Amount"),
                         "Entered Status": r.get("Claim Status"), "LLM Status": res["claim_status"],
                         "Status": agree(res["claim_status"], r.get("Claim Status")),
                         "Entered Action": r.get("ActionCode"), "LLM Action": res["action"],
                         "Action": agree(res["action"], r.get("ActionCode")),
                         "Entered Category": r.get("Category"), "LLM Category": res["category"],
                         "Category Check": agree(res["category"], r.get("Category")),
                         "Confidence": res["confidence"], "Reason": res["reason"], "Notes (masked)": r["Notes"]})
            if i % 10 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} | {(time.time() - t0) / i:.1f}s per row")

    res_df = pd.DataFrame(rows)
    out = dm.MASKED_DIR / f"{src.stem.replace(' - MASKED', '')} - CLASSIFIED.xlsx"
    summ = pd.DataFrame({f: res_df[f].value_counts() for f in ("Status", "Action", "Category Check")}).fillna(0).astype(int)
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        summ.rename_axis("Result").reset_index().to_excel(w, sheet_name="Summary", index=False)
        res_df.sort_values(["Action", "Confidence"]).to_excel(w, sheet_name="Classified", index=False)
        for ws in w.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(60, max(10, max(len(str(c.value or "")) for c in col[:100]) + 2))
            ws.freeze_panes = "A2"
    print(summ.to_string())
    print("->", out)


if __name__ == "__main__":
    main()
