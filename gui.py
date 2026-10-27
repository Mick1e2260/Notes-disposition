"""
Minimal launcher window: pick a workbook, choose what to run, Run, watch the log.
Steps run as separate processes of this same program (mask, then optional local-LLM review), so the window never
freezes and the PHI masking step must succeed before any LLM step starts. The log shows counts only, never cell data.
"""
import io
import json
import os
import queue
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

import pandas as pd

import daily_mask as dm

FROZEN = getattr(sys, "frozen", False)
MODES = ["Auto (by sheet type)", "Mask only", "Mask + audit review", "Mask + classify (Open AR)"]
DEFAULT_MODEL = "qwen2.5:7b-instruct"


def self_cmd(*args):
    base = [sys.executable] if FROZEN else [sys.executable, str(Path(__file__).with_name("ar_audit.py"))]
    return base + list(args)


def sheet_kind(path):
    """Profile of the first matching sheet; reads headers only."""
    xl = pd.ExcelFile(io.BytesIO(path.read_bytes()))
    for n in xl.sheet_names:
        p = dm.detect_profile(pd.read_excel(xl, n, nrows=0).columns)
        if p:
            return p
    return None


def ollama_models():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            return [m["name"] for m in json.loads(r.read())["models"]]
    except Exception:
        return []


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AR Audit - mask PHI + local review")
        self.geometry("640x470")
        self.minsize(520, 380)
        ttk.Style(self).theme_use("clam")
        self.q, self.busy = queue.Queue(), False
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(5, weight=1)

        self.path = tk.StringVar(value=self.newest())
        ttk.Label(f, text="Workbook").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.path).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Browse…", command=self.browse).grid(row=0, column=2)

        ttk.Label(f, text="Run").grid(row=1, column=0, sticky="w", pady=6)
        self.mode = tk.StringVar(value=MODES[0])
        ttk.Combobox(f, textvariable=self.mode, values=MODES, state="readonly").grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Mark PHI columns…", command=self.mark).grid(row=1, column=2)

        opts = ttk.Frame(f)
        opts.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.presidio = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Presidio + NER", variable=self.presidio).pack(side="left")
        ttk.Label(opts, text="   Model").pack(side="left")
        models = ollama_models()
        self.model = tk.StringVar(value=DEFAULT_MODEL if (not models or DEFAULT_MODEL in models) else models[0])
        ttk.Combobox(opts, textvariable=self.model, values=models or [DEFAULT_MODEL], width=22).pack(side="left", padx=4)
        ttk.Label(opts, text="  Max claims (0 = all)").pack(side="left")
        self.limit = tk.IntVar(value=0)
        ttk.Spinbox(opts, from_=0, to=5000, increment=10, textvariable=self.limit, width=6).pack(side="left", padx=4)

        bar = ttk.Frame(f)
        bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)
        self.run_btn = ttk.Button(bar, text="Run", command=self.run)
        self.run_btn.pack(side="left")
        ttk.Button(bar, text="Open output folder", command=lambda: os.startfile(dm.MASKED_DIR)).pack(side="left", padx=6)
        self.status = ttk.Label(bar, text="Ready")
        self.status.pack(side="right")
        self.pb = ttk.Progressbar(f, mode="indeterminate")
        self.pb.grid(row=4, column=0, columnspan=3, sticky="ew")
        self.log = tk.Text(f, height=10, state="disabled", wrap="word", font=("Consolas", 9))
        self.log.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        self.after(100, self.pump)

    def newest(self):
        files = sorted(dm.SOURCE_DIR.glob(dm.PATTERN), key=lambda p: p.stat().st_mtime, reverse=True) if dm.SOURCE_DIR.exists() else []
        return str(files[0]) if files else ""

    def browse(self):
        f = filedialog.askopenfilename(initialdir=dm.SOURCE_DIR, filetypes=[("Excel", "*.xlsx *.xlsm")])
        if f:
            self.path.set(f)

    def mark(self):
        p = self.path.get().strip()
        subprocess.Popen(self_cmd("mark", *([p] if p else [])))

    def say(self, s):
        self.log.config(state="normal")
        self.log.insert("end", s.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def run(self):
        p = Path(self.path.get().strip())
        if self.busy or not p.is_file():
            self.say("Pick a workbook first.")
            return
        mode = self.mode.get()
        if mode == MODES[0]:
            kind = sheet_kind(p)
            mode = MODES[2] if kind == "audit" else MODES[3] if kind == "inventory" else MODES[1]
            self.say(f"Detected sheet type: {kind or 'unknown'} -> {mode}")
        steps = [self_cmd("mask", str(p), *([] if self.presidio.get() else ["--no-presidio"]))]
        if mode != MODES[1]:
            extra = ["--model", self.model.get()] + (["--limit", str(self.limit.get())] if self.limit.get() > 0 else [])
            if mode == MODES[3]:
                extra.append("--all")
            steps.append(self_cmd("classify" if mode == MODES[3] else "review", *extra))
        self.busy = True
        self.run_btn.config(state="disabled")
        self.pb.start(12)
        self.status.config(text="Running…")
        threading.Thread(target=self.work, args=(steps,), daemon=True).start()

    def work(self, steps):
        ok = True
        for i, cmd in enumerate(steps, 1):
            name = next(c for c in cmd if c in ("mask", "review", "classify"))
            self.q.put(f"--- step {i}/{len(steps)}: {name} ---")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    creationflags=0x08000000)  # no console window
            for line in proc.stdout:
                self.q.put(line)
            if proc.wait() != 0:
                self.q.put("Step failed. Stopped here: nothing was sent to the LLM." if i == 1 else "Step failed.")
                ok = False
                break
        self.q.put(("done", ok))

    def pump(self):
        try:
            while True:
                m = self.q.get_nowait()
                if isinstance(m, tuple):
                    self.busy = False
                    self.pb.stop()
                    self.run_btn.config(state="normal")
                    self.status.config(text="Done" if m[1] else "Failed")
                else:
                    self.say(m)
        except queue.Empty:
            pass
        self.after(100, self.pump)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
