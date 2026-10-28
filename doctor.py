"""
`ar_audit doctor` (or `run_daily.bat doctor`): checks everything AR Audit depends on and prints PASS / FAIL with the fix.
Reads no workbook data. Paste the output if something is still wrong.
"""
import importlib
import socket
import sys
import urllib.request
from pathlib import Path


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:  # a check must never crash the report
        ok, detail = False, f"{type(e).__name__}: {e}"
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def main():
    import daily_mask as dm
    import headermem
    import webui

    frozen = getattr(sys, "frozen", False)
    print(f"AR Audit doctor | {'exe' if frozen else 'python ' + sys.version.split()[0]} | {sys.executable}\n")

    for mod in ("pandas", "openpyxl", "presidio_analyzer", "spacy"):
        check(f"library {mod}", lambda m=mod: (bool(importlib.import_module(m)), ""))
    check("spaCy model en_core_web_sm", lambda: (bool(__import__("spacy").load("en_core_web_sm")), ""))

    def ollama():
        models = webui.ollama_models()
        return (bool(models), f"models: {', '.join(models)}" if models else
                "Ollama is not answering on 127.0.0.1:11434. Start it (Start menu > Ollama) or run: ollama serve")
    check("Ollama server", ollama)
    def model():
        ok = "qwen2.5:7b-instruct" in webui.ollama_models()
        return ok, "" if ok else "missing: run  ollama pull qwen2.5:7b-instruct"
    check("model qwen2.5:7b-instruct", model)

    def source():
        f = sorted(dm.SOURCE_DIR.glob(dm.PATTERN), key=lambda p: p.stat().st_mtime, reverse=True) if dm.SOURCE_DIR.exists() else []
        return (bool(f), f"newest: {f[0].name}" if f else
                f"no '{dm.PATTERN}' in {dm.SOURCE_DIR}. Use Browse / drop a file instead, or set AR_AUDIT_SOURCE_DIR")
    check("default source folder", source)

    def writable(d):
        d = Path(d)
        d.mkdir(parents=True, exist_ok=True)
        t = d / ".w"
        t.write_text("x")
        t.unlink()
        return True, str(d)
    check("masked output folder writable", lambda: writable(dm.MASKED_DIR))
    check("private key folder writable", lambda: writable(dm.PRIVATE_DIR))

    def key():
        return (webui.persistent_key() is not None, "link key present")
    check("link key", key)

    ip = webui.lan_ip()
    print(f"\nThis PC's network address: {ip}   host name: {socket.gethostname()}")

    def port_state():
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 8765))
            return True, "port 8765 is free"
        except OSError:
            try:
                urllib.request.urlopen("http://127.0.0.1:8765/", timeout=2)
            except Exception as e:
                code = getattr(e, "code", None)
                if code == 403:
                    return True, "port 8765 is in use by a running AR Audit (the page answers). Close its window to restart"
            return False, "port 8765 is taken by another program; AR Audit will use the next free port (the link changes)"
        finally:
            s.close()
    check("port 8765", port_state)

    def reach():
        s = socket.socket()
        s.settimeout(2)
        try:
            lsn = socket.socket()
            lsn.bind(("0.0.0.0", 0))
            lsn.listen(1)
            s.connect((ip, lsn.getsockname()[1]))
            lsn.close()
            return True, f"this PC can reach itself on {ip} (firewall is not blocking the local network address)"
        except OSError as e:
            return False, f"cannot reach {ip}: {e}. Allow Python/AR-Audit on Private networks in Windows Firewall, or use 'run_daily.bat local'"
        finally:
            s.close()
    check("network link reachable", reach)
    print(f"\nHeader memory: {len(headermem._data())} headers remembered ({headermem.FILE})")
    print("Run 'run_daily.bat' to start; if the page opens but a step fails, the log in the page shows the exact error.")


if __name__ == "__main__":
    main()
