"""
Mark which columns hold PHI. Opens a small window, you pick the file and sheet, then set each column header's role.
Only the HEADERS are read (no cell data is shown or stored). The choice is saved to column_profiles.json and
daily_mask.py uses it automatically for every sheet with those headers, so you do this once per report type.

Usage:  python mask_columns.py [workbook.xlsx]

Roles (one per column header):
  Not PHI            leave the column untouched
  Identifier         replace each value with a stable token (name, MRN, claim #, account #, user ...)
  Date               replace with days relative to the work date
  Free text          scan with regex + Presidio + NER and mask what is found (notes, denial text ...)
  Drop               remove the column from the masked file
Columns left as 'Not PHI' are NOT masked. daily_mask.py still warns if one looks like SSN/phone/email.
"""
import io
import json
import re
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pandas as pd

import daily_mask as dm

ROLES = ["Not PHI", "Identifier", "Date", "Free text", "Drop"]
KIND_GUESS = [("mrn", "MRN"), ("patient|responsible|insured|subscriber|^name", "PAT"), ("claim", "CLM"), ("bill", "BIL"),
              ("account|acct", "ACC"), ("provider|pro name|doctor|physician", "PRV"), ("user|agent|submitted by|emp", "USR")]


def guess_role(col):
    c = col.lower()
    if re.search(r"date|\bdos\b|assigned on|submitted on|\bdue\b|cut-off", c):
        return "Date"
    if re.search(r"notes?$|note |description|reason|comment|remark", c):
        return "Free text"
    if re.search(r"patient|mrn|claim (number|id)|bill id|account|responsible|member|subscriber|assigned user|submitted by|"
                 r"provider|pro name|phone|email|ssn|address|dob", c):
        return "Identifier"
    return "Not PHI"


def guess_kind(col):
    c = col.lower()
    return next((k for pat, k in KIND_GUESS if re.search(pat, c)), "ID")


def profile_from_roles(name, roles):
    return {"name": name, "match": [c for c, r in roles.items() if r != "Not PHI"] or list(roles),
            "tokens": {c: guess_kind(c) for c, r in roles.items() if r == "Identifier"},
            "dates": [c for c, r in roles.items() if r == "Date"],
            "text": [c for c, r in roles.items() if r == "Free text"],
            "drop": [c for c, r in roles.items() if r == "Drop"]}


def save_profile(profile):
    data = json.loads(dm.PROFILE_FILE.read_text(encoding="utf-8")) if dm.PROFILE_FILE.exists() else {"profiles": []}
    data["profiles"] = [p for p in data["profiles"] if p["name"] != profile["name"]] + [profile]
    dm.PROFILE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def saved_roles(cols):
    """Pre-fill from an existing saved profile for these headers, else from header-name guesses."""
    if dm.PROFILE_FILE.exists():
        for p in json.loads(dm.PROFILE_FILE.read_text(encoding="utf-8")).get("profiles", []):
            if set(p["match"]) <= set(cols):
                m = {c: "Identifier" for c in p.get("tokens", {})}
                m.update({c: "Date" for c in p.get("dates", [])}); m.update({c: "Free text" for c in p.get("text", [])})
                m.update({c: "Drop" for c in p.get("drop", [])})
                return {c: m.get(c, "Not PHI") for c in cols}
    return {c: guess_role(c) for c in cols}


class App(tk.Tk):
    def __init__(self, path=None):
        super().__init__()
        self.title("Mark PHI columns")
        self.geometry("620x680")
        self.path, self.vars = None, {}
        top = ttk.Frame(self, padding=8); top.pack(fill="x")
        ttk.Button(top, text="Open workbook…", command=self.pick).pack(side="left")
        self.sheet = tk.StringVar(); self.cb = ttk.Combobox(top, textvariable=self.sheet, state="readonly", width=28)
        self.cb.pack(side="left", padx=8); self.cb.bind("<<ComboboxSelected>>", lambda e: self.load_cols())
        self.lbl = ttk.Label(self, text="Pick a workbook. Only column headers are read.", padding=(8, 0)); self.lbl.pack(anchor="w")
        box = ttk.Frame(self); box.pack(fill="both", expand=True, padx=8, pady=6)
        cv = tk.Canvas(box, highlightthickness=0); sb = ttk.Scrollbar(box, command=cv.yview)
        self.grid_f = ttk.Frame(cv); self.grid_f.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=self.grid_f, anchor="nw"); cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        bot = ttk.Frame(self, padding=8); bot.pack(fill="x")
        ttk.Button(bot, text="Save", command=self.save).pack(side="left")
        ttk.Button(bot, text="Save and mask now", command=lambda: self.save(run=True)).pack(side="left", padx=8)
        if path:
            self.open(Path(path))

    def pick(self):
        f = filedialog.askopenfilename(initialdir=dm.SOURCE_DIR, filetypes=[("Excel", "*.xlsx *.xlsm")])
        if f:
            self.open(Path(f))

    def open(self, path):
        self.path = path
        self.names = pd.ExcelFile(io.BytesIO(path.read_bytes())).sheet_names
        self.cb["values"] = self.names; self.sheet.set(self.names[0]); self.load_cols()

    def load_cols(self):
        cols = [str(c) for c in pd.read_excel(io.BytesIO(self.path.read_bytes()), self.sheet.get(), nrows=0).columns]
        for w in self.grid_f.winfo_children():
            w.destroy()
        self.vars = {}
        pre = saved_roles(cols)
        self.lbl.config(text=f"{self.path.name} / {self.sheet.get()}: {len(cols)} columns. Set the role of each header.")
        for i, c in enumerate(cols):
            ttk.Label(self.grid_f, text=c, width=36, anchor="w").grid(row=i, column=0, sticky="w", pady=1)
            v = tk.StringVar(value=pre[c]); self.vars[c] = v
            ttk.Combobox(self.grid_f, textvariable=v, values=ROLES, state="readonly", width=14).grid(row=i, column=1, padx=6)

    def save(self, run=False):
        if not self.vars:
            return
        roles = {c: v.get() for c, v in self.vars.items()}
        name = re.sub(r"\W+", "_", f"{self.path.stem}_{self.sheet.get()}")[:40]
        save_profile(profile_from_roles(name, roles))
        marked = sum(r != "Not PHI" for r in roles.values())
        if run:
            r = subprocess.run([sys.executable, str(Path(__file__).with_name("daily_mask.py")), str(self.path)],
                               capture_output=True, text=True, cwd=Path(__file__).parent)
            messagebox.showinfo("Masked", (r.stdout or r.stderr)[-1500:])
        else:
            messagebox.showinfo("Saved", f"{marked} of {len(roles)} columns marked. daily_mask.py will use this "
                                         "for every sheet with these headers.")


if __name__ == "__main__":
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
