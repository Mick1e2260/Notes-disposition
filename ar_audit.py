"""
Single entry point (also what the .exe runs).

  ar_audit daily              mask the newest 'AR Audit Result*.xlsx' in OneDrive, then local-Qwen review
  ar_audit mark [file]        open the window to mark which columns hold PHI
  ar_audit mask [file ...]    mask workbook(s)          (extra flags: --date, --no-presidio)
  ar_audit review [masked]    note-vs-disposition LLM review of a masked audit-result workbook
  ar_audit classify [masked]  LLM classification of a masked Open AR inventory workbook
  ui                          browser UI on 127.0.0.1 (default with no command): file, mark PHI columns, run, live log
  classic                     the older plain desktop window (Tk)
  help                        prints this help

Next to the exe, keep llm_context.md (editable brief for the model) and column_profiles.json (your saved column marks).
"""
import subprocess
import sys
import os
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).parent
    os.environ["PATH"] += os.pathsep + str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama")
else:
    BASE = Path(__file__).parent
    sys.path.insert(0, str(BASE))


def ensure_ollama():
    import urllib.request
    import time
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3)
    except Exception:
        subprocess.Popen(["ollama", "serve"], creationflags=0x08000000)  # CREATE_NO_WINDOW
        time.sleep(8)


def run(mod_main, argv):
    sys.argv = ["ar_audit"] + argv
    mod_main()


def main():
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("ui", [])
    import daily_mask as dm
    dm.PROFILE_FILE = BASE / "column_profiles.json"
    if cmd == "ui":
        import webui
        webui.main()
    elif cmd in ("classic", "gui"):
        import gui
        gui.main()
    elif cmd == "pick":  # native file dialog, used by the browser UI's Browse button
        import tkinter as tk
        from tkinter import filedialog
        r = tk.Tk()
        r.withdraw()
        r.attributes("-topmost", True)
        print(filedialog.askopenfilename(initialdir=dm.SOURCE_DIR, filetypes=[("Excel", "*.xlsx *.xlsm")]))
    elif cmd == "mark":
        import mask_columns as mc
        mc.App(rest[0] if rest else None).mainloop()
    elif cmd == "mask":
        run(dm.main, rest)
    elif cmd in ("review", "daily", "classify"):
        import llm_audit as la
        import llm_classify as lc
        la.HERE = BASE
        if cmd == "daily":
            run(dm.main, rest)  # masking must succeed (SystemExit/exception stops here) before any LLM call
        ensure_ollama()
        run(lc.main if cmd == "classify" else la.main, rest if cmd != "daily" else [])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
