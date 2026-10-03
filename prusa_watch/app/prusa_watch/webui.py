"""Labelling page served through Home Assistant ingress (sidebar panel "Prusa Watch").

Only the Supervisor's ingress proxy may connect; all URLs in the page are relative because ingress
serves it under /api/hassio_ingress/<token>/.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .dataset import Dataset, TRUTHS
from .vision import ISSUES

log = logging.getLogger(__name__)

ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1"}   # HA ingress proxy, local tests

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prusa Watch labels</title>
<style>
:root{--bg:#fafafa;--card:#fff;--fg:#1c1c1e;--muted:#6b6b70;--line:#e2e2e6;--ok:#2e7d32;--warn:#b26a00;--bad:#c62828;--acc:#1565c0}
@media (prefers-color-scheme:dark){:root{--bg:#111214;--card:#1c1d20;--fg:#ececef;--muted:#9a9aa2;--line:#2c2d31}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;z-index:2}
h1{font-size:17px;margin:0 12px 0 0}.stats{color:var(--muted);margin-left:auto}
button,select{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:6px 10px;cursor:pointer}
button.on{border-color:var(--acc);color:var(--acc)}
main{padding:16px;display:grid;gap:14px;max-width:1100px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;display:grid;gap:10px}
.top{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}.id{color:var(--muted);font-size:12px}
.pill{border-radius:999px;padding:2px 9px;font-weight:600;font-size:12px;color:#fff}
.ok{background:var(--ok)}.warning{background:var(--warn)}.failure{background:var(--bad)}.camera_problem{background:#546e7a}
.imgs{display:grid;grid-template-columns:1fr 1fr;gap:8px}.imgs figure{margin:0}.imgs img{width:100%;border-radius:8px;cursor:zoom-in;display:block}
figcaption{color:var(--muted);font-size:12px}.desc{margin:0}.ev{color:var(--muted);margin:0}
.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.lab{font-weight:600}.lab.ok{color:var(--ok);background:none}.lab.failure{color:var(--bad);background:none}
#zoom{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;z-index:9}
#zoom img{max-width:96vw;max-height:96vh}
@media (max-width:640px){.imgs{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Prusa Watch - labels</h1>
<button data-f="unlabelled" class="on">Unlabelled</button><button data-f="flagged">Flagged</button>
<button data-f="labelled">Labelled</button><button data-f="all">All</button>
<span class="stats" id="stats"></span></header>
<main id="list"><p>Loading...</p></main>
<div id="zoom" onclick="this.style.display='none'"><img alt=""></div>
<script>
const ISSUES = %ISSUES%;
let filt = "unlabelled";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function load(){
  const [st, items] = await Promise.all([fetch("api/stats").then(r=>r.json()), fetch("api/samples?filter="+filt).then(r=>r.json())]);
  document.getElementById("stats").textContent =
    `${st.samples} samples · ${st.flagged} flagged · ${st.labelled} labelled · ${st.truth_failure} real failures`;
  const list = document.getElementById("list");
  if(!items.length){ list.innerHTML = "<p>Nothing here.</p>"; return; }
  list.innerHTML = items.map(card).join("");
}
function card(m){
  const v = m.verdict, l = m.label, ref = m.has_reference;
  const issueSel = `<select id="is-${m.id}">${ISSUES.filter(i=>i!=="none").map(i =>
      `<option ${i===(l?.issue||v.issue)?"selected":""}>${i}</option>`).join("")}</select>`;
  return `<section class="card">
   <div class="top"><span class="pill ${esc(v.status)}">${esc(v.status)}</span>
     <b>${esc(v.issue)}</b> <span>${Math.round((v.confidence||0)*100)}%</span>
     <span>part ${esc(v.part_visible||"?")}</span>
     <span class="id">${esc(m.time)} · job ${esc(m.job_id)} · ${esc(m.id)}</span></div>
   <div class="imgs"><figure><img loading="lazy" src="img/${m.id}/current.jpg" alt="current frame"><figcaption>current</figcaption></figure>
     ${ref ? `<figure><img loading="lazy" src="img/${m.id}/reference.jpg" alt="reference frame"><figcaption>reference</figcaption></figure>` : ""}</div>
   <p class="desc">${esc(v.description)}</p>
   ${v.evidence ? `<p class="ev">Evidence: ${esc(v.evidence)}</p>` : ""}
   ${v.downgraded ? `<p class="ev">Downgraded: ${esc(v.downgraded)}</p>` : ""}
   <div class="actions">
     ${l ? `<span class="lab ${l.truth}">Label: ${l.truth}${l.truth==="failure"?" / "+esc(l.issue):""}</span>` : "<span>Ground truth?</span>"}
     <button onclick="label('${m.id}','ok')">✅ OK print</button>
     <button onclick="label('${m.id}','failure')">❌ Real failure</button> ${issueSel}
   </div></section>`;
}
async function label(id, truth){
  const issue = truth === "failure" ? document.getElementById("is-"+id).value : "none";
  const r = await fetch("api/label", {method:"POST", headers:{"Content-Type":"application/json"},
                                      body: JSON.stringify({id, truth, issue})});
  if(!r.ok){ alert("Saving failed: " + await r.text()); return; }
  load();
}
document.querySelectorAll("header button").forEach(b => b.onclick = () => {
  document.querySelectorAll("header button").forEach(x => x.classList.remove("on"));
  b.classList.add("on"); filt = b.dataset.f; load(); });
document.getElementById("list").addEventListener("click", e => {
  if(e.target.tagName === "IMG"){ const z = document.getElementById("zoom"); z.querySelector("img").src = e.target.src; z.style.display = "flex"; }});
load();
</script></body></html>"""


def make_handler(ds: Dataset):
    page = PAGE.replace("%ISSUES%", json.dumps(list(ISSUES))).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):          # keep the add-on log clean
            log.debug("web: " + fmt, *args)

        def _allowed(self) -> bool:
            if self.client_address[0] in ALLOWED_CLIENTS:
                return True
            self._send(403, b"forbidden", "text/plain")
            return False

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def do_GET(self):
            if not self._allowed():
                return
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            if not parts:
                return self._send(200, page, "text/html; charset=utf-8")
            if parts == ["api", "stats"]:
                return self._json(ds.stats())
            if parts == ["api", "samples"]:
                filt = parse_qs(url.query).get("filter", ["unlabelled"])[0]
                return self._json(ds.samples(filt))
            if len(parts) == 3 and parts[0] == "img" and parts[2] in ("current.jpg", "reference.jpg"):
                path = ds.image_path(parts[1], parts[2][:-4])
                if path:
                    with open(path, "rb") as f:
                        return self._send(200, f.read(), "image/jpeg")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._allowed():
                return
            if urlparse(self.path).path.strip("/") != "api/label":
                return self._send(404, b"not found", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                truth = body.get("truth")
                issue = body.get("issue", "none")
                if truth not in TRUTHS or issue not in ISSUES:
                    raise ValueError("bad truth/issue")
                self._json(ds.label(str(body.get("id")), truth, issue, source="web"))
            except KeyError:
                self._send(404, b"unknown sample", "text/plain")
            except (ValueError, json.JSONDecodeError) as e:
                self._send(400, str(e).encode(), "text/plain")

    return Handler


def start(ds: Dataset, port: int = 8099) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(ds))
    threading.Thread(target=server.serve_forever, name="webui", daemon=True).start()
    log.info("Labelling page listening on :%d (Home Assistant sidebar: Prusa Watch)", port)
    return server
