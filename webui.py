"""
Local web UI (default). Serves one page on 127.0.0.1 only and opens it in your browser. No external fonts, scripts or
requests: the page and API live in this process. Every API call needs a per-run token and a loopback Host header.

Flow: 1 pick workbook  ->  2 mark which column headers hold PHI (saved per report type)  ->  3 run mask (+ local review).
Steps run as child processes of this program (see self_cmd), so masking must succeed before any LLM step starts.
Only headers are ever read for the page; the log shows counts, never cell data.
"""
import contextlib
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime
from urllib.parse import parse_qs, quote, urlparse

import pandas as pd

import daily_mask as dm
import headermem as hm
import mask_columns_logic as ml
import phiscan
import phi_scrub as ps
import reidentify

TOKEN = secrets.token_urlsafe(16)  # replaced by a persistent key in main() so the link stays the same between runs
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
SERVER_IP = {"ip": None}  # this PC's LAN address when running in network mode
JOB = {"lines": [], "done": True, "ok": True, "running": False}
LOCK = threading.Lock()
ROLES = ml.ROLES
FROZEN = getattr(sys, "frozen", False)
DEFAULT_MODEL = "qwen2.5:7b-instruct"


def self_cmd(*args):
    """Command line that runs this same program (the exe, or python) with `args`: used for the masking and review steps."""
    return ([sys.executable] if FROZEN else [sys.executable, str(Path(__file__).with_name("ar_audit.py"))]) + list(args)


def ollama_models():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            return [m["name"] for m in json.loads(r.read())["models"]]
    except Exception:
        return []


def say(s):
    with LOCK:
        JOB["lines"].append(s.rstrip("\n"))


UPLOADS = {}  # id -> (filename, bytes). Added workbooks live in memory only and are never written to disk.
MAX_UPLOAD = 100 * 1024 * 1024


def raw_of(path):
    return UPLOADS[path[4:]][1] if str(path).startswith("mem:") else Path(path).read_bytes()


def name_of(path):
    return UPLOADS[path[4:]][0] if str(path).startswith("mem:") else Path(path).name


def sheet_kind(path):
    xl = pd.ExcelFile(io.BytesIO(raw_of(path)))
    for n in xl.sheet_names:
        k = dm.builtin_profile(dm.read_sheet(xl, n, nrows=0).columns)
        if k:
            return k


def sheets_of(path):
    """Every sheet with its column count and whether its headers already have saved tags."""
    xl = pd.ExcelFile(io.BytesIO(raw_of(path)))
    out = []
    for n in xl.sheet_names:
        cols = list(dm.read_sheet(xl, n, nrows=0).columns)
        prof = dm.detect_profile(cols)
        out.append({"name": n, "cols": len(cols), "status": "tagged" if prof and prof not in dm.PROFILES else "suggested"})
    return out


_DF = {}  # (path, mtime, sheet) -> DataFrame, memory only (never written to disk)
SAMPLE = 400  # headers appear at once; stats and previews use the first rows (the full sheet is only read by Run)
def frame(path, sheet):
    mem = str(path).startswith("mem:")
    key = (str(path), 0 if mem else Path(path).stat().st_mtime, sheet)
    if key not in _DF:
        _DF.clear()
        _DF[key] = dm.read_sheet(pd.ExcelFile(io.BytesIO(raw_of(path))), sheet, nrows=SAMPLE)
    return _DF[key]


def shape(v):
    return re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", str(v)[:28])))


def col_stats(s, anchor=None, vocab=None):
    """Facts about a column that do not reveal its values: fill, distinct, type, what the masking rules find, value shapes."""
    nn = s.dropna()
    txt = nn.astype(str)
    out = {"filled": round(len(nn) / max(len(s), 1) * 100), "distinct": int(nn.nunique()),
           "type": "date" if pd.api.types.is_datetime64_any_dtype(s) else "number" if pd.api.types.is_numeric_dtype(s) else "text",
           "avg_len": int(txt.str.len().mean()) if len(txt) else 0}
    # same rules the masker uses: would it have masked anything in this column?
    sc = phiscan.scan_series(s, anchor or datetime.now(), vocab)
    out["hits"], out["suggest"], out["scanned"] = sc["hits"], sc["suggest"], sc["cells"]
    out["shapes"] = [[k, int(v)] for k, v in txt.map(shape).value_counts().head(3).items()] if out["type"] != "number" else []
    return out


def columns_of(path, sheet):
    df = frame(path, sheet)
    cols = [str(c) for c in df.columns]
    roles, src = hm.role_map(cols, lambda c: ml.guess_role_data(c, df[c]), ml.guess_kind)
    prof = dm.detect_profile(cols)
    cand = dm.vocab_candidates(df, {k: [c for c, r in roles.items() if r == role] for k, role in
                                    (("tokens", "Identifier"), ("text", "Text"), ("phi", "PHI"), ("drop", "Drop"))})
    ps.VOCAB = set().union(*cand.values()) if cand else set()
    anchor = dm.anchor_date(Path(name_of(path)), None)
    # every column is scanned (not only the ones tagged No PHI now), so choosing No PHI later flags instantly
    stats = {str(c): col_stats(df[c], anchor, set().union(*[w for k, w in cand.items() if k != str(c)]) if cand else set())
             for c in df.columns}
    return {"columns": cols, "roles": roles, "src": src, "profile": None if prof in dm.PROFILES else prof, "stats": stats}


def preview(path, sheet, col, role):
    """What the first 3 filled cells become under `role` (masked output only; No PHI shows the value as-is)."""
    df = frame(path, sheet)
    anchor = dm.anchor_date(Path(name_of(path)), None)
    ps.SALT = dm.private_setup()
    k0 = len(ps.key_rows)
    out = []
    try:
        for v in df[col].dropna().head(3):
            if role == "PHI":
                out.append(ps.star(v))
            elif role == "Identifier":
                out.append(ps.token(hm.kind_for(col, ml.guess_kind), v, "token"))
            elif role == "Text":
                import presidio_layer as pl
                out.append(pl.presidio_scrub(ps.scrub_note(str(v), anchor)))
            elif role == "Drop":
                out.append("column removed from the masked file")
            else:
                out.append(str(v))
    finally:
        del ps.key_rows[k0:]  # previews must not leak into a real run's key
    return out


def resolve_mode(path, mode):
    if mode != "auto":
        return mode
    kind = sheet_kind(path)
    say(f"Detected sheet type: {kind or 'unknown'}")
    return {"audit": "review", "inventory": "classify"}.get(kind, "mask")


class _Say:
    def write(self, s):
        for line in s.splitlines():
            if line.strip():
                say(line)

    def flush(self):
        pass


def mask_uploaded(opts):
    """Mask an in-memory workbook inside this process (no file ever holds the original)."""
    path = opts["path"]
    try:
        with contextlib.redirect_stdout(_Say()):
            dm.process(raw_of(path), name_of(path), dm.anchor_date(Path(name_of(path)), None),
                       opts.get("presidio", True), suggest_all=True)
        return True
    except BaseException as e:  # includes SystemExit raised for 'no matching sheet'
        say(f"Masking failed: {e if isinstance(e, SystemExit) else type(e).__name__ + ': ' + str(e)}. Nothing was sent to the LLM.")
        return False


def run_job(opts):
    try:
        _run_job(opts)
    except BaseException as e:
        say(f"Unexpected error: {type(e).__name__}: {e}")
        JOB.update(ok=False)
    finally:
        JOB.update(done=True, running=False)


def _run_job(opts):
    p = opts["path"]
    mem = str(p).startswith("mem:")
    try:
        mode = resolve_mode(p, opts.get("mode", "auto"))
    except Exception as e:
        say(f"Could not read the workbook: {e}")
        JOB.update(done=True, ok=False, running=False)
        return
    steps = [None if mem else self_cmd("mask", str(p), "--suggest", *([] if opts.get("presidio", True) else ["--no-presidio"]))]
    if mode != "mask":
        extra = ["--model", opts.get("model") or DEFAULT_MODEL]
        if int(opts.get("limit") or 0) > 0:
            extra += ["--limit", str(int(opts["limit"]))]
        if mode == "classify":
            extra.append("--all")
        steps.append(self_cmd(mode, *extra))
    ok = True
    for i, cmd in enumerate(steps, 1):
        name = "mask" if cmd is None else next(c for c in cmd if c in ("mask", "review", "classify"))
        say(f"--- step {i}/{len(steps)}: {name} ---")
        if cmd is None:
            if not mask_uploaded(opts):
                ok = False
                break
            continue
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=0x08000000)
        for line in proc.stdout:
            say(line)
        if proc.wait() != 0:
            say("Masking failed. Nothing was sent to the LLM." if i == 1 else "Step failed.")
            ok = False
            break
    JOB.update(done=True, ok=ok, running=False)


def outputs(local=False):
    """Masked results that may be downloaded. QUARANTINE files stay local (they hold rows that still look risky);
    WITH ORIGINALS (masked rows plus the real Identifier values) is offered only to the person at this PC."""
    if not dm.MASKED_DIR.exists():
        return []
    fs = [f for f in dm.MASKED_DIR.glob("*.xlsx") if not f.name.startswith("~$") and not f.stem.endswith("QUARANTINE")
          and (local or not f.stem.endswith("WITH ORIGINALS"))]
    fs.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"name": f.name, "kind": "masked" if f.stem.endswith("MASKED") else "result",
             "when": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d %b %H:%M"), "kb": round(f.stat().st_size / 1024)}
            for f in fs]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _client(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0].split("%")[0])
            return getattr(ip, "ipv4_mapped", None) or ip
        except ValueError:
            return None

    def _ok(self):
        """Loopback or private-network clients only, and only for Host names this server was started with."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        ip = self._client()
        return host in ALLOWED_HOSTS and ip is not None and (ip.is_loopback or ip.is_private)

    def _local_client(self):
        """The person sitting at this PC (loopback, or this PC reaching itself through its LAN address)."""
        ip = self._client()
        return ip is not None and (ip.is_loopback or str(ip) == SERVER_IP["ip"])

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _file(self, name, data, extra=None):
        ascii_name = name.encode("ascii", "replace").decode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", 'attachment; filename="' + ascii_name + '"; filename*=UTF-8\'\'' + quote(name))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.send_header("Access-Control-Expose-Headers", "Content-Disposition, X-Restored, X-Unresolved, X-Dates, X-Ambiguous")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if not self._ok():
            return self._send(403, {"error": "bad host"})
        if u.path == "/":
            ip = SERVER_IP["ip"]
            if ip and (self.headers.get("Host") or "").split(":")[0].lower() != ip:  # network mode: always land on the network link
                self.send_response(302)
                self.send_header("Location", f"http://{ip}:{self.server.server_address[1]}/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html")
        if self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "bad token"})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/state":
                files = dm.newest_sources()
                models = ollama_models()
                return self._send(200, {"path": str(files[0]) if files else "", "models": models,
                                        "model": DEFAULT_MODEL if (not models or DEFAULT_MODEL in models) else models[0],
                                        "ollama": bool(models), "out": str(dm.MASKED_DIR), "roles": ROLES})
            if u.path == "/api/sheets":
                return self._send(200, {"sheets": sheets_of(q["path"])})
            if u.path == "/api/columns":
                return self._send(200, columns_of(q["path"], q["sheet"]))
            if u.path == "/api/outputs":
                return self._send(200, {"files": outputs(self._local_client())})
            if u.path == "/api/download":
                if q["file"] not in {o["name"] for o in outputs(self._local_client())}:
                    return self._send(404, {"error": "not an available result"})
                return self._file(q["file"], (dm.MASKED_DIR / q["file"]).read_bytes())
            if u.path == "/api/preview":
                return self._send(200, {"preview": preview(q["path"], q["sheet"], q["col"], q["role"])})
            if u.path == "/api/job":
                since = int(q.get("since", 0))
                with LOCK:
                    return self._send(200, {"lines": JOB["lines"][since:], "n": len(JOB["lines"]), "done": JOB["done"], "ok": JOB["ok"]})
        except Exception as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._ok() or self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            return self._send(413, {"error": "file too large"})
        raw = self.rfile.read(n)
        u, qs = urlparse(self.path).path, {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            if u == "/api/upload":
                name = Path(qs.get("name", "upload.xlsx")).name
                pd.ExcelFile(io.BytesIO(raw))  # must be a readable workbook
                uid = secrets.token_hex(6)
                UPLOADS.clear()  # one added workbook at a time keeps memory bounded
                UPLOADS[uid] = (name, raw)
                return self._send(200, {"path": "mem:" + uid, "name": name})
            if u == "/api/restore":
                if not self._local_client():
                    return self._send(403, {"error": "Restore is only available from the PC that runs AnkaShield"})
                known = {o["name"] for o in outputs()}
                src = raw if raw else ((dm.MASKED_DIR / qs["out"]).read_bytes() if qs.get("out") in known else None)
                if src is None:
                    return self._send(400, {"error": "choose a masked workbook"})
                fname = qs.get("name") or qs.get("out") or "masked.xlsx"
                anchor = dm.anchor_date(Path(fname), None)
                data, st = reidentify.restore_bytes(src, anchor)
                stem = re.sub(r"\s*-\s*(MASKED|CLASSIFIED|LLM REVIEW)$", "", Path(fname).stem)
                return self._file(stem + " - RESTORED.xlsx", data,
                                  {"X-Restored": st["restored"], "X-Unresolved": st["unresolved"],
                                   "X-Dates": st["dates"], "X-Ambiguous": st["ambiguous"]})
            body = json.loads(raw or b"{}")
            if u == "/api/profile":
                roles = body["roles"]
                name = dm.config_name(name_of(body["path"]), list(roles))  # one config per header set
                hm.remember(roles, lambda c: hm.kind_for(c, ml.guess_kind))  # header memory: next sheet pre-fills these decisions
                dm.save_profile(dm.profile_from_roles(name, roles))
                return self._send(200, {"saved": sum(r != "No PHI" for r in roles.values()), "of": len(roles), "config": name})
            if u == "/api/run":
                with LOCK:
                    if JOB["running"]:
                        return self._send(409, {"error": "already running"})
                    JOB.update(lines=[], done=False, ok=True, running=True)
                threading.Thread(target=run_job, args=(body,), daemon=True).start()
                return self._send(200, {"started": True})
            if u == "/api/open":
                os.startfile(dm.MASKED_DIR)
                return self._send(200, {"ok": True})
            if u == "/api/quit":
                self._send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
        except Exception as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})


def lan_ip():
    """This PC's LAN address (no packet is sent; the OS just picks the outgoing interface)."""
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        c.connect(("10.255.255.255", 1))
        return c.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        c.close()


def persistent_key():
    """A secret kept in the private dir so the link (and bookmarks) survive restarts."""
    dm.private_setup()
    f = dm.PRIVATE_DIR / "ui_key.txt"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(18))
    return f.read_text().strip()


class Server(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: never share the port with another copy of AnkaShield
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def open_server(host, port=8765):
    """The first free port from `port` up (so the link is stable when nothing else uses it)."""
    for pt in list(range(port, port + 10)) + [0]:
        try:
            return Server((host, pt), H)
        except OSError:
            continue


def main(network=False, port=8765):
    global TOKEN
    TOKEN = persistent_key()
    host, ip = ("0.0.0.0", lan_ip()) if network else ("127.0.0.1", None)
    ALLOWED_HOSTS.update({ip, socket.gethostname().lower(), socket.getfqdn().lower()} - {None})
    SERVER_IP["ip"] = ip
    srv = open_server(host, port)
    pt = srv.server_address[1]
    if pt != port:
        print(f"  Port {port} is already in use by another program (probably another AnkaShield window), so port {pt} is used.", flush=True)
    local = f"http://127.0.0.1:{pt}/"
    print("AnkaShield is running. Close this window or press Quit in the page to stop.", flush=True)
    if network:
        link = f"http://{ip}:{pt}/"
        print(f"  Network link : {link}", flush=True)
        print("  Anyone on this office network who has the link can use AnkaShield: share it only with people allowed to see masked data.", flush=True)
        print("  Restore (original values) works only from this PC. If Windows asks, allow access on Private networks.", flush=True)
        webbrowser.open(link)
    else:
        print(f"  Link : {local}", flush=True)
        webbrowser.open(local)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AnkaShield</title>
<style>
:root{
  --bg:oklch(97.5% .008 175);--surface:oklch(99% .004 175);--ink:oklch(24% .02 195);--muted:oklch(46% .02 195);
  --line:oklch(88% .012 185);--accent:oklch(46% .09 185);--accent-ink:oklch(98% .01 185);--accent-soft:oklch(93% .035 185);
  --warn:oklch(52% .13 60);--bad:oklch(48% .16 28);--good:oklch(44% .1 155);
  --log-bg:oklch(22% .02 200);--log-ink:oklch(90% .02 175);
  --display:"Bahnschrift","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
  --body:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;--mono:"Cascadia Mono","Consolas",monospace;
  --ease:cubic-bezier(.25,1,.5,1)}
@media(prefers-color-scheme:dark){:root{
  --bg:oklch(20% .015 200);--surface:oklch(24% .018 200);--ink:oklch(93% .012 175);--muted:oklch(72% .02 185);
  --line:oklch(34% .02 195);--accent:oklch(74% .11 175);--accent-ink:oklch(20% .03 195);--accent-soft:oklch(30% .05 190);
  --warn:oklch(78% .12 75);--bad:oklch(72% .14 28);--good:oklch(76% .12 155);--log-bg:oklch(16% .015 205)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--body)}
main{max-width:880px;margin:0 auto;padding:clamp(20px,5vw,56px) 16px 80px}
header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;padding-bottom:20px;border-bottom:1px solid var(--line)}
h1{font:600 28px/1.1 var(--display);letter-spacing:.01em;margin:0}
.sub{color:var(--muted);margin:6px 0 0;max-width:46ch}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{font:500 12px var(--display);letter-spacing:.04em;text-transform:uppercase;padding:5px 10px;border:1px solid var(--line);border-radius:99px;color:var(--muted);display:inline-flex;gap:7px;align-items:center}
.chip i{width:7px;height:7px;border-radius:50%;background:var(--muted)}
.chip.on{color:var(--good)}.chip.on i{background:var(--good)}.chip.off{color:var(--warn)}.chip.off i{background:var(--warn)}
section{padding:28px 0;border-bottom:1px solid var(--line);display:grid;grid-template-columns:150px 1fr;gap:12px 28px}
@media(max-width:700px){section{grid-template-columns:1fr}}
.step{font:600 13px var(--display);letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
h2{font:600 20px/1.2 var(--display);margin:2px 0 4px}
.hint{color:var(--muted);font-size:13.5px;margin:0 0 14px;max-width:60ch}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input,select{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:9px 12px;min-height:40px;transition:border-color .2s var(--ease),box-shadow .2s var(--ease)}
input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
input[type=text]{flex:1;min-width:220px}
button{font:600 14px var(--display);letter-spacing:.02em;border-radius:8px;min-height:40px;padding:0 18px;cursor:pointer;border:1px solid var(--line);background:var(--surface);color:var(--ink);transition:transform .15s var(--ease),background .2s var(--ease),border-color .2s var(--ease)}
button:hover{border-color:var(--accent)}button:active{transform:translateY(1px)}
button:focus-visible{outline:3px solid var(--accent-soft);outline-offset:2px}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);padding:0 26px}
button.primary:hover{filter:brightness(1.08)}button:disabled{opacity:.5;cursor:default}
button.quiet{border-color:transparent;background:none;color:var(--muted)}
.cols{max-height:340px;overflow:auto;border-top:1px solid var(--line);margin-top:12px}
.col{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;padding:7px 2px;border-bottom:1px solid var(--line)}
@media(max-width:700px){.col{grid-template-columns:1fr;gap:5px;padding:10px 2px}.seg{width:100%}.seg button{flex:1;padding:0 4px}.detail{padding-left:8px}.dgrid{grid-template-columns:repeat(2,1fr)}}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;border-radius:0;min-height:30px;padding:0 9px;white-space:nowrap;font:500 12.5px var(--body);background:none;color:var(--muted);border-right:1px solid var(--line)}
.seg button:last-child{border-right:0}
.seg button.sel{background:var(--accent);color:var(--accent-ink)}
.seg button.sel.isdrop{background:var(--bad);color:#fff}
.meta{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:12px;color:var(--muted);font-size:13.5px;flex-wrap:wrap}
label.chk{display:inline-flex;gap:8px;align-items:center;cursor:pointer}
input[type=number]{width:92px}
#log{background:var(--log-bg);color:var(--log-ink);font:12.5px/1.6 var(--mono);border-radius:10px;padding:14px 16px;min-height:150px;max-height:320px;overflow:auto;white-space:pre-wrap;margin-top:16px}
#log:empty::before{content:"Output appears here. Counts only, never cell data.";opacity:.55}
.state{font:600 13px var(--display);letter-spacing:.04em}
.state.ok{color:var(--good)}.state.fail{color:var(--bad)}
.err{color:var(--bad);font-size:13.5px;margin-top:8px}
.colwrap{border-bottom:1px solid var(--line)}.colwrap .col{border-bottom:0}
.cname{all:unset;box-sizing:border-box;display:flex;align-items:center;gap:9px;min-height:32px;cursor:pointer;min-width:0;border-radius:6px;padding:2px 4px}
.cname:hover{background:var(--accent-soft)}.cname:focus-visible{outline:3px solid var(--accent-soft);outline-offset:1px}
.cname span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chev{width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(-45deg);transition:transform .2s var(--ease);flex:none;margin:0 3px}
.open .chev{transform:rotate(45deg)}
.newtag{font:500 11px var(--display);font-style:normal;letter-spacing:.05em;text-transform:uppercase;color:var(--accent);border:1px solid var(--accent);border-radius:99px;padding:0 7px}
.flag{font:500 11.5px var(--display);font-style:normal;letter-spacing:.03em;color:var(--warn);border:1px solid var(--warn);border-radius:99px;padding:1px 8px;white-space:nowrap}
.detail{padding:4px 6px 18px 24px;animation:rise .25s var(--ease)}
@keyframes rise{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.detail h4{font:600 12px var(--display);letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin:16px 0 6px}
.dgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;max-width:420px}
.dgrid b{display:block;font:600 18px var(--display)}.dgrid small{color:var(--muted);font-size:12px}
.tags{display:flex;gap:6px;flex-wrap:wrap}.tag{font-size:12.5px;padding:2px 9px;border-radius:99px;background:var(--accent-soft)}
.tag.warn{background:none;border:1px solid var(--warn);color:var(--warn)}
.muted{color:var(--muted);font-size:13px;margin:0}.warn-t{color:var(--warn);font-size:13px;margin:8px 0 0}
.shapes div{display:flex;gap:14px;align-items:baseline;font-size:13px;color:var(--muted)}
code{font:12.5px var(--mono)}.pv{display:grid;gap:5px;margin-top:6px}
.pv code{background:var(--log-bg);color:var(--log-ink);padding:6px 10px;border-radius:6px;white-space:pre-wrap;word-break:break-word}
.drop{margin-top:12px;padding:16px;border:1.5px dashed var(--line);border-radius:10px;color:var(--muted);font-size:13.5px;cursor:pointer;transition:border-color .2s var(--ease),background .2s var(--ease)}
.drop:hover,.drop.over,.drop:focus-visible{border-color:var(--accent);background:var(--accent-soft);outline:none}.drop u{color:var(--accent)}
.frow{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)}
.fn{display:grid;min-width:0}.fn b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}.fn small{color:var(--muted);font-size:12.5px}
input[type=date]{min-height:40px}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

/* Guided workspace */
main{max-width:1040px} header{align-items:center;padding-bottom:24px}
header h1{font-size:25px} .workspace-nav{display:flex;gap:8px;margin:28px 0 16px;flex-wrap:wrap}
.navstep{display:flex;align-items:center;gap:9px;border:0;background:transparent;color:var(--muted);padding:8px 12px}
.navstep .num{display:grid;place-items:center;width:27px;height:27px;border:1px solid var(--line);border-radius:50%;font-size:12px}
.navstep.active{color:var(--accent);background:var(--accent-soft)}.navstep.active .num{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
section.panel{display:none;grid-template-columns:1fr;padding:32px;border:1px solid var(--line);border-radius:18px;background:var(--surface);box-shadow:0 12px 36px #1232;min-height:340px}
section.panel.active{display:block;animation:rise .25s var(--ease)} section.panel>.step{margin-bottom:12px}
section.panel h2{font-size:27px;margin-bottom:8px} section.panel .hint{font-size:15px;max-width:70ch}
.actions{display:flex;justify-content:space-between;gap:12px;margin-top:28px;padding-top:20px;border-top:1px solid var(--line)}
.feature-help{padding:15px 17px;background:var(--accent-soft);border-radius:10px;margin:14px 0;color:var(--ink)}
.feature-help strong{display:block;margin-bottom:3px}.feature-help p{margin:0;font-size:13.5px}
.options{display:grid;gap:12px;margin-top:16px}.options label{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.options .muted{display:block}
#log{min-height:0;max-height:220px} details{margin-top:20px} summary{cursor:pointer;color:var(--muted);font-weight:600}
.loading{position:fixed;inset:0;z-index:5;background:var(--bg);display:none;place-items:center;padding:24px}.loading.active{display:grid}
.loading-card{text-align:center;max-width:440px}.spinner{width:70px;height:70px;border:5px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite;margin:0 auto 28px}
@keyframes spin{to{transform:rotate(360deg)}} .loading h2{font-size:28px}.loading p{color:var(--muted)}
@media(max-width:700px){section.panel{padding:22px}.navstep{padding:7px}.navstep .navlabel{display:none}.navstep.active .navlabel{display:inline}.actions{flex-wrap:wrap}}
@media(prefers-reduced-motion:reduce){.spinner{animation:none;border-top-color:var(--accent);border-right-color:var(--accent)}}
.flagbar{margin:12px 0 0;padding:12px 14px;border:1px solid var(--warn);border-radius:10px;font-size:13.5px}
.flagbar b{color:var(--warn)}.flagbar ul{margin:6px 0 10px;padding-left:20px}.flagbar li{margin:2px 0}
.warnbox{margin:16px 0;padding:14px 16px;border:1px solid var(--warn);border-radius:10px}
.warnbox h3{margin:0 0 6px;font:600 15px var(--display);color:var(--warn)}
.warnbox ul{margin:8px 0 12px;padding-left:20px}.warnbox li{margin:3px 0;font-size:13.5px}
.hot{color:var(--warn);font-weight:600}button.small{min-height:30px;padding:0 12px;font-size:12.5px}
</style></head><body><main>
<header>
  <div><h1>AnkaShield</h1><p class="sub">Mask PHI on this PC, then review notes with a local model. Nothing leaves this machine.</p></div>
  <div class="chips"><span class="chip" id="oll"><i></i><span>Ollama</span></span><button class="quiet" id="quit" title="Stop the local server">Quit</button></div>
</header>

<nav class="workspace-nav" aria-label="Workflow steps">
  <button class="navstep active" data-step="1"><span class="num">1</span><span class="navlabel">Workbook</span></button>
  <button class="navstep" data-step="2"><span class="num">2</span><span class="navlabel">Column privacy</span></button>
  <button class="navstep" data-step="3"><span class="num">3</span><span class="navlabel">Run options</span></button>
  <button class="navstep" data-step="4"><span class="num">4</span><span class="navlabel">Results</span></button>
  <button class="navstep" data-step="5"><span class="num">↺</span><span class="navlabel">Restore</span></button>
</nav>
<section class="panel active" id="step1"><div class="step">Step 1 of 4 · Choose a workbook</div><div>
  <h2>Workbook</h2><p class="hint">Defaults to the newest audit result in OneDrive. Only column headers are read here.</p>
  <div class="row"><input type="text" id="path" placeholder="Path to an .xlsx file" spellcheck="false"><button id="browse">Browse…</button></div>
  <div class="row" style="margin-top:10px"><select id="sheet" aria-label="Sheet"></select><span class="chip" id="kind" style="display:none"></span></div>
  <p class="muted" style="margin-top:8px">Every sheet goes into the masked file. A sheet you have not tagged uses the suggested tags; pick it here to check them. Title rows above the headers are found automatically.</p>
  <div class="drop" id="drop" tabindex="0" role="button" aria-label="Add a workbook">Drop an .xlsx here, or <u>add a workbook</u>. It is held in memory only and never saved.<input type="file" id="file" accept=".xlsx,.xlsm" hidden></div>
  <div class="err" id="err1" role="alert"></div>
<div class="actions"><span></span><button class="primary" data-next="2">Continue to column privacy →</button></div>
</div></section>

<section class="panel" id="step2"><div class="step">Step 2 of 4 · Review privacy</div><div>
  <h2>Which columns contain PHI</h2>
  <p class="hint">Tag each header once per file type; the next file with these headers is filled in for you. <b>PHI</b> keeps only the first and last character (S***H, J**N). <b>Text</b> keeps the sentence and masks only the names and numbers inside it.</p>
  <div class="row"><input type="text" id="filter" placeholder="Filter headers" style="max-width:260px;flex:none"><label class="chk"><input type="checkbox" id="flagged"> Only flagged</label></div>
  <div class="flagbar" id="flagbar" hidden></div>
  <div class="cols" id="cols"></div>
  <div class="meta"><span id="count">No columns loaded</span><span class="row"><span class="state" id="saved"></span><button id="save">Save marks</button></span></div>
<div class="actions"><button data-next="1">← Back</button><button class="primary" data-next="3">Continue to run options →</button></div>
</div></section>

<section class="panel" id="step3"><div class="step">Step 3 of 4 · Configure</div><div>
  <h2>Run</h2><p class="hint">Masking always runs first. If it fails, the review is skipped.</p>
  <div class="row">
    <select id="mode" aria-label="What to run">
      <option value="mask">Masked Excel only</option><option value="auto">Masked Excel + LLM review (by sheet type)</option>
      <option value="review">Mask + audit review</option><option value="classify">Mask + classify (Open AR)</option></select>
  </div>
  <div class="feature-help" id="modeHelp"></div>
  <div class="options">
    <label>Local model <select id="model" aria-label="Model"></select> <span class="muted">Used for review and classification.</span></label>
    <label>Max claims <input type="number" id="limit" min="0" value="0" title="0 = all"> <span class="muted">Limit the review; 0 processes all claims.</span></label>
    <label class="chk"><input type="checkbox" id="presidio" checked> Presidio + NER <span class="muted">Adds entity detection to help identify sensitive text.</span></label>
  </div>
  <div class="actions"><button data-next="2">← Back</button><button class="primary" id="run">Start processing →</button></div>
</div></section>
<section class="panel" id="step4"><div class="step">Step 4 of 4 · Results</div><div>
  <h2>Your results</h2><p class="hint">Download your processed files or open the output folder on this PC.</p>
  <span class="state" id="state" role="status"></span><div class="warnbox" id="warnbox" hidden></div><div id="results" class="files"></div>
  <div class="row" style="margin-top:20px"><button id="open">Open output folder</button><button data-next="5">Restore originals</button></div>
  <details><summary>Processing details</summary><pre id="log"></pre></details>
  <div class="actions"><button data-next="3">← Run options</button><button data-next="1">Start another workbook</button></div>
</div></section>

<section class="panel" id="step5"><div class="step">Separate tool · Restore</div><div>
  <h2>Restore originals</h2>
  <p class="hint">Turns a masked workbook back into the real values using the private key on this PC. The restored file contains PHI: it is built in memory and downloaded straight to you, never saved here.</p>
  <div class="row"><select id="rsel" aria-label="Masked workbook"></select><span class="muted">or</span>
    <button id="raddbtn">Add a masked workbook…</button><input type="file" id="rfile" accept=".xlsx,.xlsm" hidden><span id="rname" class="muted"></span></div>
  <div class="row" style="margin-top:16px"><button class="primary" id="restore">Restore and download</button><span class="state" id="rstate"></span></div>
  <p class="muted" id="rnote" style="margin-top:10px"></p>
</div></section>
</main>
<div class="loading" id="loading" role="status" aria-live="polite"><div class="loading-card"><div class="spinner" aria-hidden="true"></div><h2>Processing your workbook</h2><p>Masking sensitive information. This may take a few minutes.</p><p>Please keep this window open.</p></div></div>
<script>
const T="__TOKEN__",$=id=>document.getElementById(id);
const api=(p,o={})=>fetch(p,{...o,headers:{"X-Token":T,"Content-Type":"application/json"}}).then(r=>r.json());
const post=(p,b)=>api(p,{method:"POST",body:JSON.stringify(b||{})});
let CUR="",rfile=null;const P=()=>CUR;function setPath(p,d){CUR=p;$("path").value=d||p;loadSheets()}
let roles={},ROLES=[],timer=null,since=0,STATS={},OPEN=new Set(),PREV={},SRC={},WARN=[];
function goStep(n){
  if((n===2||n===3)&&(!P()||!Object.keys(roles).length)){document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));$('step1').classList.add('active');document.querySelectorAll('.navstep').forEach(x=>x.classList.toggle('active',x.dataset.step==='1'));$('err1').textContent='Choose a readable workbook before continuing.';return}
  document.querySelectorAll('.panel').forEach(x=>x.classList.toggle('active',x.id==='step'+n));
  document.querySelectorAll('.navstep').forEach(x=>{x.classList.toggle('active',x.dataset.step==n);if(x.dataset.step==n)x.setAttribute('aria-current','step');else x.removeAttribute('aria-current')});
  window.scrollTo(0,0);
}
document.querySelectorAll('[data-step]').forEach(b=>b.onclick=()=>goStep(+b.dataset.step));
document.querySelectorAll('[data-next]').forEach(b=>b.onclick=()=>goStep(+b.dataset.next));
const modeDescriptions={mask:['Masked Excel only','Creates a masked workbook without running a model.'],auto:['Automatic review','Masks first, then selects a local model review based on the sheet type.'],review:['Audit review','Masks first, then reviews audit notes with the local model.'],classify:['Open AR classification','Masks first, then classifies Open AR notes with the local model.']};
function showMode(){const d=modeDescriptions[$('mode').value];$('modeHelp').innerHTML=`<strong>${d[0]}</strong><p>${d[1]}</p>`;}
$('mode').onchange=showMode;showMode();
const known=()=>Object.keys(SRC).forEach(k=>SRC[k]="remembered");
const esc=x=>String(x).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const HITLBL={phone:"phone numbers",email:"emails",ssn:"SSN-like numbers",address:"addresses",id:"IDs / long numbers",name:"person names",url:"links",dob:"dates of birth"};
const ROLEHELP={"No PHI":"Left exactly as it is.","PHI":"Whole value masked, first and last character kept: S***H, J**N.","Identifier":"Each value becomes a stable token you can group on and restore: [CLM-AB12CD].","Text":"Sentence kept; only names, numbers, phones and emails inside it are masked.","Drop":"Removed from the masked file."};
const sens=c=>Object.values((STATS[c]||{}).hits||{}).reduce((a,v)=>a+v,0);
const flagged=c=>roles[c]==="No PHI"&&sens(c)>0;
const foundText=c=>Object.entries((STATS[c]||{}).hits||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${v} ${HITLBL[k]}`).join(", ");
function detail(c){
  const s=STATS[c]||{},role=roles[c],pv=PREV[c+"|"+role];
  const hits=Object.entries(s.hits||{}).map(([k,v])=>`<span class="tag ${role==="No PHI"?"warn":""}">${v} ${HITLBL[k]}</span>`).join("")||'<span class="muted">The masking rules find nothing sensitive in this column.</span>';
  const shapes=(s.shapes||[]).map(([k,v])=>`<div><code>${esc(k)}</code><span>${v} cells</span></div>`).join("");
  const sg=esc(s.suggest||"Text");
  const fnote=flagged(c)?`<p class="warn-t">Tagged <b>No PHI</b>, but the masking rules would have masked this in ${sens(c)} of ${s.scanned} checked cells. Suggested tag: <b>${sg}</b>. <button class="small setrole" data-r="${sg}">Set to ${sg}</button></p>`:"";
  const dnote=role==="No PHI"&&s.type==="date"?'<p class="warn-t">Dates tied to a patient count as PHI under Safe Harbor. Consider “PHI”.</p>':"";
  return `<div class="dgrid"><div><b>${s.filled}%</b><small>filled</small></div><div><b>${s.distinct}</b><small>distinct</small></div><div><b>${s.type}</b><small>type</small></div><div><b>${s.avg_len}</b><small>avg length</small></div></div>
  <h4>The masking rules find</h4><div class="tags">${hits}</div>${fnote}${dnote}
  ${shapes?`<h4>Common value shapes</h4><div class="shapes">${shapes}</div>`:""}
  <h4>After masking as “${esc(role)}”</h4><p class="muted">${ROLEHELP[role]}</p>
  <div class="pv">${pv?pv.map(x=>`<code>${esc(x)}</code>`).join(""):'<span class="muted">Loading…</span>'}</div>`;
}
async function loadPrev(c){
  const role=roles[c],k=c+"|"+role;if(PREV[k])return;
  const r=await api("/api/preview?"+new URLSearchParams({path:P(),sheet:$("sheet").value,col:c,role}));
  PREV[k]=r.error?[r.error]:r.preview;renderCols();
}
function renderCols(){
  const f=$("filter").value.toLowerCase(),only=$("flagged").checked,box=$("cols"),st=box.scrollTop;box.innerHTML="";
  Object.keys(roles).filter(c=>c.toLowerCase().includes(f)&&(!only||flagged(c))).forEach(c=>{
    const w=document.createElement("div"),open=OPEN.has(c);w.className="colwrap"+(open?" open":"");
    const d=document.createElement("div");d.className="col";
    const n=document.createElement("button");n.className="cname";n.setAttribute("aria-expanded",open);n.title=c;
    n.innerHTML=`<i class="chev"></i><span>${esc(c)}</span>${SRC[c]==="guessed"?'<em class="newtag" title="Not seen before: role guessed from the name. Confirm it, then Save or Run to remember it.">new</em>':""}${flagged(c)?`<em class="flag" title="Tagged No PHI, but the masking rules find: ${esc(foundText(c))}">⚠ ${sens(c)} sensitive</em>`:""}`;
    n.onclick=()=>{open?OPEN.delete(c):OPEN.add(c);if(!open)loadPrev(c);renderCols()};
    const g=document.createElement("div");g.className="seg";g.setAttribute("role","group");g.setAttribute("aria-label","Role for "+c);
    ROLES.forEach(r=>{const b=document.createElement("button");b.textContent=r;
      b.className=(roles[c]===r?"sel ":"")+(r==="Drop"?"isdrop":"");b.setAttribute("aria-pressed",roles[c]===r);
      b.onclick=()=>{roles[c]=r;$("saved").textContent="";if(OPEN.has(c))loadPrev(c);renderCols()};g.appendChild(b)});
    d.append(n,g);w.appendChild(d);
    if(open){const x=document.createElement("div");x.className="detail";x.innerHTML=detail(c);
      x.querySelectorAll(".setrole").forEach(b=>b.onclick=()=>{roles[c]=b.dataset.r;$("saved").textContent="";loadPrev(c);renderCols()});w.appendChild(x)}
    box.appendChild(w)});
  box.scrollTop=st;
  const all=Object.keys(roles),m=all.filter(c=>roles[c]!=="No PHI").length,fl=all.filter(flagged).length,nw=all.filter(c=>SRC[c]==="guessed").length;
  $("count").className=fl?"hot":"";
  $("count").textContent=all.length?`${m} of ${all.length} columns marked`+(nw?` · ${nw} new to review`:" · all remembered")+(fl?` · ⚠ ${fl} tagged No PHI look sensitive`:""):"No columns loaded";
  renderFlagbar(all.filter(flagged));
}
function renderFlagbar(fl){
  const b=$("flagbar");
  if(!fl.length){b.hidden=true;b.innerHTML="";return}
  b.hidden=false;
  b.innerHTML=`<b>⚠ ${fl.length} column${fl.length>1?"s":""} tagged No PHI contain what the masking rules would mask</b><ul>${fl.slice(0,8).map(c=>`<li>${esc(c)}: ${esc(foundText(c))} → suggest ${esc(STATS[c].suggest||"Text")}</li>`).join("")}${fl.length>8?`<li>…and ${fl.length-8} more</li>`:""}</ul><div class="row"><button class="small" id="fbApply">Apply suggested tags</button><button class="small" id="fbShow">${$("flagged").checked?"Show all columns":"Show only these"}</button></div>`;
  $("fbApply").onclick=()=>{fl.forEach(c=>roles[c]=STATS[c].suggest||"Text");$("saved").textContent="";OPEN.clear();renderCols()};
  $("fbShow").onclick=()=>{$("flagged").checked=!$("flagged").checked;renderCols()};
}
function showWarnings(){
  const b=$("warnbox");
  if(!WARN.length){b.hidden=true;b.innerHTML="";return}
  b.hidden=false;
  b.innerHTML=`<h3>⚠ Check before sharing: ${WARN.length} column${WARN.length>1?"s":""} tagged No PHI still look sensitive</h3><p class="muted">These columns were left unchanged in the masked file. The same list is on its “PHI Warnings” sheet.</p><ul>${WARN.map(w=>`<li><b>${esc(w.col)}</b> (${esc(w.sheet)}): ${esc(w.found)} → suggest ${esc(w.suggest)}</li>`).join("")}</ul><button class="small" id="wbFix">Review these columns</button>`;
  $("wbFix").onclick=()=>{const f=WARN.find(w=>[...$("sheet").options].some(o=>o.value===w.sheet));if(f)$("sheet").value=f.sheet;loadCols().then(()=>{$("flagged").checked=true;renderCols();goStep(2)})};
}
async function loadCols(){
  $("err1").textContent="";
  const q=new URLSearchParams({path:P(),sheet:$("sheet").value});
  const r=await api("/api/columns?"+q);
  if(r.error){$("err1").textContent=r.error;return}
  roles=r.roles;SRC=r.src||{};STATS=r.stats;PREV={};OPEN=new Set();
  const k=$("kind");k.style.display=r.profile?"inline-flex":"none";k.textContent=r.profile?(r.profile.startsWith("saved:")?"Saved config: "+r.profile.slice(6).replace(/_[0-9a-f]{6}$/,"").replace(/_/g," "):"From header memory"):"Suggested tags";k.style.display="inline-flex";
  renderCols();
}
async function loadSheets(){
  const p=P();if(!p)return;$("err1").textContent="";
  const r=await api("/api/sheets?"+new URLSearchParams({path:p}));
  if(r.error){$("err1").textContent="Cannot read that file: "+r.error;return}
  const best=r.sheets.reduce((a,s)=>s.cols>a.cols?s:a,r.sheets[0]);
  $("sheet").innerHTML=r.sheets.map(s=>`<option value="${esc(s.name)}"${s===best?" selected":""}>${esc(s.name)} · ${s.cols} columns · ${s.status==="tagged"?"tagged":"suggested tags"}</option>`).join("");loadCols();
}
$("browse").onclick=()=>$("file").click();
$("path").onchange=()=>{CUR=$("path").value.trim();loadSheets()};$("sheet").onchange=loadCols;$("filter").oninput=renderCols;$("flagged").onchange=renderCols;
$("save").onclick=async()=>{const r=await post("/api/profile",{path:P(),sheet:$("sheet").value,roles});
  $("saved").className="state ok";$("saved").textContent=r.error?r.error:`Saved ${r.saved}/${r.of} · remembered`;if(!r.error){known();renderCols()}};
$("open").onclick=()=>post("/api/open");
$("quit").onclick=async()=>{await post("/api/quit");document.body.innerHTML='<main><h1>Stopped</h1><p class="sub">You can close this tab.</p></main>'};
$("run").onclick=async()=>{
  const p=P();if(!p){$("err1").textContent="Pick a workbook first.";return}
  const bad=Object.keys(roles).filter(flagged);
  if(bad.length&&!confirm(`${bad.length} column${bad.length>1?"s":""} tagged No PHI look sensitive:\n\n`+bad.slice(0,10).map(c=>`• ${c}: ${foundText(c)}`).join("\n")+`\n\nStart processing anyway?\n(Cancel goes back so you can review them.)`)){$("flagged").checked=true;goStep(2);renderCols();return}
  WARN=[];showWarnings();
  $("log").textContent="";since=0;$("state").textContent="";$("run").disabled=true;$("loading").classList.add("active");
  try {
  if(Object.keys(roles).length){const m=await post("/api/profile",{path:p,sheet:$("sheet").value,roles});$("log").textContent=m.error?"":`Using the ${m.saved} column marks shown in Step 2 (saved and remembered).\n`;if(!m.error){known();renderCols()}}
  const r=await post("/api/run",{path:p,mode:$("mode").value,model:$("model").value,limit:+$("limit").value,presidio:$("presidio").checked});
  if(r.error){$("log").textContent=r.error;$("run").disabled=false;$("loading").classList.remove("active");$("state").className="state fail";$("state").textContent=r.error;goStep(4);return}
  timer=setInterval(poll,700);
  } catch(e){finishError(e)}
};
function finishError(e){clearInterval(timer);$('loading').classList.remove('active');$('run').disabled=false;$('state').className='state fail';$('state').textContent='Could not contact the local server. Check the connection and try again.';$('log').textContent=String(e);goStep(4)}
async function poll(){
  try {const r=await api("/api/job?since="+since);since=r.n;
  if(r.lines.length){const l=$("log");l.textContent+=r.lines.join("\n")+"\n";l.scrollTop=l.scrollHeight;
    r.lines.forEach(t=>{const m=t.match(/WARNING: (.+?) \| (.+?) \| (.+) \| (\w+)\s*$/);if(m)WARN.push({sheet:m[1],col:m[2],found:m[3],suggest:m[4]})})}
  if(r.done){clearInterval(timer);loadOutputs();$("run").disabled=false;$("loading").classList.remove("active");
    $("state").className="state "+(r.ok?"ok":"fail");$("state").textContent=r.ok?(WARN.length?"Processing complete, with warnings (see below)":"Processing complete"):"Processing failed. Open processing details for more information.";showWarnings();goStep(4)}
  } catch(e){finishError(e)}
}

async function fetchFile(url,opts){
  const r=await fetch(url,{...opts,headers:{"X-Token":T,...((opts||{}).headers||{})}});
  if(!r.ok){let m="Failed";try{m=(await r.json()).error||m}catch(e){}throw new Error(m)}
  const blob=await r.blob(),cd=r.headers.get("Content-Disposition")||"";
  const m=/filename\*=UTF-8''([^;]+)/.exec(cd)||/filename="([^"]+)"/.exec(cd);
  const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=m?decodeURIComponent(m[1]):"download.xlsx";
  document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),4000);return r;
}
const download=n=>fetchFile("/api/download?"+new URLSearchParams({file:n})).catch(e=>alert(e.message));
async function loadOutputs(){
  const f=(await api("/api/outputs")).files||[];
  $("results").innerHTML=f.length?f.map(o=>`<div class="frow"><span class="fn"><b>${esc(o.name)}</b><small>${o.kind==="masked"?"Masked workbook":"Review output"} · ${esc(o.when)} · ${o.kb} KB</small></span><button data-n="${esc(o.name)}" class="dl ${o.kind==="masked"?"primary":""}">Download</button></div>`).join(""):'<p class="muted">Nothing yet. Run a mask to create a masked workbook.</p>';
  document.querySelectorAll(".dl").forEach(b=>b.onclick=()=>download(b.dataset.n));
  const keep=$("rsel").value;$("rsel").innerHTML=f.map(o=>`<option value="${esc(o.name)}">${esc(o.name)}</option>`).join("");if(keep)$("rsel").value=keep;
}
async function upload(file){
  if(!file)return;$("err1").textContent="Reading "+file.name+"…";
  const r=await fetch("/api/upload?"+new URLSearchParams({name:file.name}),{method:"POST",headers:{"X-Token":T,"Content-Type":"application/octet-stream"},body:file}).then(x=>x.json());
  if(r.error){$("err1").textContent="Cannot read that file: "+r.error;return}
  $("err1").textContent="";setPath(r.path,r.name+"  (added, memory only)");
}
const dz=$("drop");
dz.onclick=()=>$("file").click();dz.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();$("file").click()}};
$("file").onchange=e=>{upload(e.target.files[0]);e.target.value=""};
["dragenter","dragover"].forEach(t=>dz.addEventListener(t,e=>{e.preventDefault();dz.classList.add("over")}));
["dragleave","drop"].forEach(t=>dz.addEventListener(t,e=>{e.preventDefault();dz.classList.remove("over")}));
dz.addEventListener("drop",e=>upload(e.dataTransfer.files[0]));
$("raddbtn").onclick=()=>$("rfile").click();
$("rfile").onchange=e=>{rfile=e.target.files[0]||null;$("rname").textContent=rfile?rfile.name:"";e.target.value=""};
$("restore").onclick=async()=>{
  $("rstate").textContent="";$("rnote").textContent="";
  if(!rfile&&!$("rsel").value){$("rstate").className="state fail";$("rstate").textContent="Choose a masked workbook";return}
  if(!confirm("The restored workbook contains PHI. It is created in memory and downloaded to this browser only. Continue?"))return;
  const q=new URLSearchParams(rfile?{name:rfile.name}:{out:$("rsel").value,name:$("rsel").value});
  $("restore").disabled=true;
  try{
    const r=await fetchFile("/api/restore?"+q,{method:"POST",headers:{"Content-Type":"application/octet-stream"},body:rfile||undefined});
    const un=+r.headers.get("X-Unresolved");
    $("rstate").className="state ok";$("rstate").textContent="Downloaded";
    $("rnote").textContent=`${r.headers.get("X-Restored")} values restored, ${r.headers.get("X-Dates")} dates rebuilt`+(un?`; ${un} tokens had no key and were left as they are`:"")+(+r.headers.get("X-Ambiguous")?`; ${r.headers.get("X-Ambiguous")} starred values match more than one person and stay masked.`:".");
  }catch(e){$("rstate").className="state fail";$("rstate").textContent=e.message}
  $("restore").disabled=false;
};
(async()=>{
  const s=await api("/api/state");ROLES=s.roles;
  $("oll").className="chip "+(s.ollama?"on":"off");$("oll").lastChild.textContent=s.ollama?"Ollama ready":"Ollama offline";
  $("model").innerHTML=(s.models.length?s.models:[s.model]).map(m=>`<option ${m===s.model?"selected":""}>${m}</option>`).join("");
  if(s.path)setPath(s.path);loadOutputs();
})();
</script></body></html>'''


if __name__ == "__main__":
    main()

