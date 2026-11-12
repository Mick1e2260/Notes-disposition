"""
Single entry point (also what the .exe runs).

  ar_audit ui [--network]     browser UI (default with no command); --network serves it on this PC's LAN address with a secret key
  ar_audit daily              mask the newest 'AR Audit Result*.xlsx' in OneDrive, then local-Qwen review
  ar_audit mask [file ...]    mask workbook(s)          (flags: --date, --no-presidio, --suggest)
  ar_audit review [masked]    note-vs-disposition LLM review of a masked audit-result workbook
  ar_audit classify [masked]  LLM classification of a masked Open AR inventory workbook
  ar_audit doctor             check Python/libraries/Ollama/model/folders/port/network and print what is wrong

Next to the exe, keep llm_context.md (the editable brief for the model).
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
    if cmd == "ui":
        import webui
        webui.main(network="--network" in rest)
    elif cmd == "doctor":
        import doctor
        doctor.main()
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
