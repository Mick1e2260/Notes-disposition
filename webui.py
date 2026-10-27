"""
Local web UI (default). Serves one page on 127.0.0.1 only and opens it in your browser. No external fonts, scripts or
requests: the page and API live in this process. Every API call needs a per-run token and a loopback Host header.

Flow: 1 pick workbook  ->  2 mark which column headers hold PHI (saved per report type)  ->  3 run mask (+ local review).
Steps run as child processes of this program (see gui.self_cmd), so masking must succeed before any LLM step starts.
Only headers are ever read for the page; the log shows counts, never cell data.
"""
import io
import json
import os
import secrets
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

import daily_mask as dm
import gui
import mask_columns as mc

TOKEN = secrets.token_urlsafe(16)
JOB = {"lines": [], "done": True, "ok": True, "running": False}
LOCK = threading.Lock()
ROLES = mc.ROLES


def say(s):
    with LOCK:
        JOB["lines"].append(s.rstrip("\n"))


def sheets_of(path):
    return pd.ExcelFile(io.BytesIO(Path(path).read_bytes())).sheet_names


def columns_of(path, sheet):
    cols = [str(c) for c in pd.read_excel(io.BytesIO(Path(path).read_bytes()), sheet, nrows=0).columns]
    return {"columns": cols, "roles": mc.saved_roles(cols), "profile": dm.detect_profile(cols)}


def resolve_mode(path, mode):
    if mode != "auto":
        return mode
    kind = gui.sheet_kind(Path(path))
    say(f"Detected sheet type: {kind or 'unknown'}")
    return {"audit": "review", "inventory": "classify"}.get(kind, "mask")


def run_job(opts):
    p = Path(opts["path"])
    try:
        mode = resolve_mode(p, opts.get("mode", "auto"))
    except Exception as e:
        say(f"Could not read the workbook: {e}")
        JOB.update(done=True, ok=False, running=False)
        return
    steps = [gui.self_cmd("mask", str(p), *([] if opts.get("presidio", True) else ["--no-presidio"]))]
    if mode != "mask":
        extra = ["--model", opts.get("model") or gui.DEFAULT_MODEL]
        if int(opts.get("limit") or 0) > 0:
            extra += ["--limit", str(int(opts["limit"]))]
        if mode == "classify":
            extra.append("--all")
        steps.append(gui.self_cmd(mode, *extra))
    ok = True
    for i, cmd in enumerate(steps, 1):
        name = next(c for c in cmd if c in ("mask", "review", "classify"))
        say(f"--- step {i}/{len(steps)}: {name} ---")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=0x08000000)
        for line in proc.stdout:
            say(line)
        if proc.wait() != 0:
            say("Masking failed. Nothing was sent to the LLM." if i == 1 else "Step failed.")
            ok = False
            break
    JOB.update(done=True, ok=ok, running=False)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if not self._ok():
            return self._send(403, {"error": "bad host"})
        if u.path == "/":
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html")
        if self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "bad token"})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/state":
                files = sorted(dm.SOURCE_DIR.glob(dm.PATTERN), key=lambda p: p.stat().st_mtime, reverse=True) if dm.SOURCE_DIR.exists() else []
                models = gui.ollama_models()
                return self._send(200, {"path": str(files[0]) if files else "", "models": models,
                                        "model": gui.DEFAULT_MODEL if (not models or gui.DEFAULT_MODEL in models) else models[0],
                                        "ollama": bool(models), "out": str(dm.MASKED_DIR), "roles": ROLES})
            if u.path == "/api/sheets":
                return self._send(200, {"sheets": sheets_of(q["path"])})
            if u.path == "/api/columns":
                return self._send(200, columns_of(q["path"], q["sheet"]))
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
        body = json.loads(self.rfile.read(n) or b"{}")
        u = urlparse(self.path).path
        try:
            if u == "/api/browse":
                r = subprocess.run(gui.self_cmd("pick"), capture_output=True, text=True, creationflags=0x08000000)
                return self._send(200, {"path": r.stdout.strip()})
            if u == "/api/profile":
                roles = body["roles"]
                name = mc.re.sub(r"\W+", "_", f"{Path(body['path']).stem}_{body['sheet']}")[:40]
                mc.save_profile(mc.profile_from_roles(name, roles))
                return self._send(200, {"saved": sum(r != "Not PHI" for r in roles.values()), "of": len(roles)})
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


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"AR Audit running at {url}  (close this window or press Quit in the page to stop)", flush=True)
    webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AR Audit</title>
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
.col span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:700px){.col{grid-template-columns:1fr;gap:5px;padding:10px 2px}.seg{width:100%}.seg button{flex:1;padding:0 4px}}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;border-radius:0;min-height:30px;padding:0 11px;font:500 12.5px var(--body);background:none;color:var(--muted);border-right:1px solid var(--line)}
.seg button:last-child{border-right:0}
.seg button.sel{background:var(--accent);color:var(--accent-ink)}
.seg button.sel.drop{background:var(--bad);color:#fff}
.meta{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:12px;color:var(--muted);font-size:13.5px;flex-wrap:wrap}
label.chk{display:inline-flex;gap:8px;align-items:center;cursor:pointer}
input[type=number]{width:92px}
#log{background:var(--log-bg);color:var(--log-ink);font:12.5px/1.6 var(--mono);border-radius:10px;padding:14px 16px;min-height:150px;max-height:320px;overflow:auto;white-space:pre-wrap;margin-top:16px}
#log:empty::before{content:"Output appears here. Counts only, never cell data.";opacity:.55}
.bar{height:3px;background:var(--line);border-radius:2px;overflow:hidden;margin-top:16px;opacity:0;transition:opacity .3s var(--ease)}
.bar.on{opacity:1}.bar b{display:block;height:100%;width:35%;background:var(--accent);animation:slide 1.3s var(--ease) infinite}
@keyframes slide{from{transform:translateX(-100%)}to{transform:translateX(290%)}}
.state{font:600 13px var(--display);letter-spacing:.04em}
.state.ok{color:var(--good)}.state.fail{color:var(--bad)}
.err{color:var(--bad);font-size:13.5px;margin-top:8px}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body><main>
<header>
  <div><h1>AR Audit</h1><p class="sub">Mask PHI on this PC, then review notes with a local model. Nothing leaves this machine.</p></div>
  <div class="chips"><span class="chip" id="oll"><i></i><span>Ollama</span></span><button class="quiet" id="quit" title="Stop the local server">Quit</button></div>
</header>

<section><div class="step">Step 1</div><div>
  <h2>Workbook</h2><p class="hint">Defaults to the newest audit result in OneDrive. Only column headers are read here.</p>
  <div class="row"><input type="text" id="path" placeholder="Path to an .xlsx file" spellcheck="false"><button id="browse">Browse…</button></div>
  <div class="row" style="margin-top:10px"><select id="sheet" aria-label="Sheet"></select><span class="chip" id="kind" style="display:none"></span></div>
  <div class="err" id="err1"></div>
</div></section>

<section><div class="step">Step 2</div><div>
  <h2>Which columns contain PHI</h2>
  <p class="hint">Mark by header, once per report type. Columns left as “Not PHI” are not touched, so mark anything with patient or staff details.</p>
  <div class="row"><input type="text" id="filter" placeholder="Filter headers" style="max-width:260px;flex:none"></div>
  <div class="cols" id="cols"></div>
  <div class="meta"><span id="count">No columns loaded</span><span class="row"><span class="state" id="saved"></span><button id="save">Save marks</button></span></div>
</div></section>

<section><div class="step">Step 3</div><div>
  <h2>Run</h2><p class="hint">Masking always runs first. If it fails, the review is skipped.</p>
  <div class="row">
    <select id="mode" aria-label="What to run">
      <option value="auto">Auto (by sheet type)</option><option value="mask">Mask only</option>
      <option value="review">Mask + audit review</option><option value="classify">Mask + classify (Open AR)</option></select>
    <select id="model" aria-label="Model"></select>
    <label class="chk">Max claims <input type="number" id="limit" min="0" value="0" title="0 = all"></label>
    <label class="chk"><input type="checkbox" id="presidio" checked> Presidio + NER</label>
  </div>
  <div class="row" style="margin-top:16px"><button class="primary" id="run">Run</button><button id="open">Open output folder</button><span class="state" id="state"></span></div>
  <div class="bar" id="bar"><b></b></div>
  <pre id="log"></pre>
</div></section>
</main>
<script>
const T="__TOKEN__",$=id=>document.getElementById(id);
const api=(p,o={})=>fetch(p,{...o,headers:{"X-Token":T,"Content-Type":"application/json"}}).then(r=>r.json());
const post=(p,b)=>api(p,{method:"POST",body:JSON.stringify(b||{})});
let roles={},ROLES=[],timer=null,since=0;

function renderCols(){
  const f=$("filter").value.toLowerCase(),box=$("cols");box.innerHTML="";
  Object.keys(roles).filter(c=>c.toLowerCase().includes(f)).forEach(c=>{
    const d=document.createElement("div");d.className="col";
    const s=document.createElement("span");s.textContent=c;s.title=c;
    const g=document.createElement("div");g.className="seg";g.setAttribute("role","group");g.setAttribute("aria-label","Role for "+c);
    ROLES.forEach(r=>{const b=document.createElement("button");b.textContent=r;
      b.className=(roles[c]===r?"sel ":"")+(r==="Drop"?"drop":"");b.setAttribute("aria-pressed",roles[c]===r);
      b.onclick=()=>{roles[c]=r;$("saved").textContent="";renderCols()};g.appendChild(b)});
    d.append(s,g);box.appendChild(d)});
  const n=Object.values(roles).filter(r=>r!=="Not PHI").length;
  $("count").textContent=Object.keys(roles).length?`${n} of ${Object.keys(roles).length} columns marked`:"No columns loaded";
}
async function loadCols(){
  $("err1").textContent="";
  const q=new URLSearchParams({path:$("path").value.trim(),sheet:$("sheet").value});
  const r=await api("/api/columns?"+q);
  if(r.error){$("err1").textContent=r.error;return}
  roles=r.roles;const k=$("kind");k.style.display=r.profile?"inline-flex":"none";k.textContent=r.profile?("Detected: "+r.profile.replace("saved:","saved profile ")):"";
  renderCols();
}
async function loadSheets(){
  const p=$("path").value.trim();if(!p)return;$("err1").textContent="";
  const r=await api("/api/sheets?"+new URLSearchParams({path:p}));
  if(r.error){$("err1").textContent="Cannot read that file: "+r.error;return}
  $("sheet").innerHTML=r.sheets.map(s=>`<option>${s.replace(/</g,"&lt;")}</option>`).join("");loadCols();
}
$("browse").onclick=async()=>{const r=await post("/api/browse");if(r.path){$("path").value=r.path;loadSheets()}};
$("path").onchange=loadSheets;$("sheet").onchange=loadCols;$("filter").oninput=renderCols;
$("save").onclick=async()=>{const r=await post("/api/profile",{path:$("path").value.trim(),sheet:$("sheet").value,roles});
  $("saved").className="state ok";$("saved").textContent=r.error?r.error:`Saved ${r.saved}/${r.of}`};
$("open").onclick=()=>post("/api/open");
$("quit").onclick=async()=>{await post("/api/quit");document.body.innerHTML='<main><h1>Stopped</h1><p class="sub">You can close this tab.</p></main>'};
$("run").onclick=async()=>{
  const p=$("path").value.trim();if(!p){$("err1").textContent="Pick a workbook first.";return}
  $("log").textContent="";since=0;$("state").textContent="";$("run").disabled=true;$("bar").classList.add("on");
  const r=await post("/api/run",{path:p,mode:$("mode").value,model:$("model").value,limit:+$("limit").value,presidio:$("presidio").checked});
  if(r.error){$("log").textContent=r.error;$("run").disabled=false;$("bar").classList.remove("on");return}
  timer=setInterval(poll,700);
};
async function poll(){
  const r=await api("/api/job?since="+since);since=r.n;
  if(r.lines.length){const l=$("log");l.textContent+=r.lines.join("\n")+"\n";l.scrollTop=l.scrollHeight}
  if(r.done){clearInterval(timer);$("run").disabled=false;$("bar").classList.remove("on");
    $("state").className="state "+(r.ok?"ok":"fail");$("state").textContent=r.ok?"Done":"Failed"}
}
(async()=>{
  const s=await api("/api/state");ROLES=s.roles;
  $("oll").className="chip "+(s.ollama?"on":"off");$("oll").lastChild.textContent=s.ollama?"Ollama ready":"Ollama offline";
  $("model").innerHTML=(s.models.length?s.models:[s.model]).map(m=>`<option ${m===s.model?"selected":""}>${m}</option>`).join("");
  if(s.path){$("path").value=s.path;loadSheets()}
})();
</script></body></html>'''


if __name__ == "__main__":
    main()

