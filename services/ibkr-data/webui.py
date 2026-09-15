#!/usr/bin/env python3
"""Local web page for logging the IBKR gateway in.

    .venv/bin/python services/ibkr-data/webui.py    # then open the URL it prints

Binds to 127.0.0.1 only. The page asks for the IBKR username and password
(saved to deploy/.env, owner-only, gitignored), starts the gateway, and asks
for the six digit authenticator code at the moment the gateway wants it.
Stdlib only on the server side; the terminal flow in gateway.py stays the
authority for the underlying mechanics, which this imports.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway as gw  # noqa: E402  (env_value, write_env, compose, logs, ...)

HOST, PORT = "127.0.0.1", 8642
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
# Fresh per process. Embedded in the page and required on every POST, so a
# page served by a rebound DNS name or a cross-site form cannot drive this
# server even from the same machine.
CSRF_TOKEN = secrets.token_urlsafe(32)

STEPS = ["credentials", "starting the container", "waiting for the gateway",
         "authenticator code", "sending the code", "opening the API"]

_lock = threading.Lock()
_state: dict = {
    "phase": "idle",           # idle needs_creds starting waiting need_code
                               # typing opening logged_in failed
    "message": "",
    "done": [],                # step names completed
    "active": None,            # step name in progress
    "deadline": None,          # unix ts the code window closes
    "accounts": [],
}
_code_event = threading.Event()
_code_value: list[str] = []
_worker: threading.Thread | None = None


def set_state(**kw) -> None:
    with _lock:
        if "phase" in kw and kw["phase"] != _state.get("phase"):
            _state["phase_ts"] = time.time()
        _state.update(kw)


def get_state() -> dict:
    with _lock:
        out = dict(_state)
    out["creds_present"] = bool(gw.env_value("TWS_USERID")
                                and gw.env_value("TWS_PASSWORD"))
    out["totp_present"] = bool(gw.env_value("TOTP_SECRET"))
    out["now"] = time.time()
    return out


def step_done(name: str, nxt: str | None) -> None:
    with _lock:
        if name not in _state["done"]:
            _state["done"].append(name)
        _state["active"] = nxt


def login_worker() -> None:
    try:
        step_done("credentials", "starting the container")
        set_state(phase="starting", message="")

        age = gw.throttle_age()
        if age is not None and age < gw.THROTTLE_COOLDOWN:
            wait = int((gw.THROTTLE_COOLDOWN - age) // 60) + 1
            set_state(phase="failed", active=None,
                      message=f"IBKR throttled this login recently. Wait about "
                              f"{wait} minutes, then press start again.")
            return

        started_at = time.time() - 1
        if "lacuna-ib-gateway" in gw.compose("ps"):
            gw.compose("restart")
        else:
            gw.compose("up", "-d")
        step_done("starting the container", "waiting for the gateway")
        set_state(phase="waiting",
                  message="The gateway program is booting and logging in to "
                          "IBKR with your saved details. This usually takes "
                          "around 40 seconds.")

        need_code, limit = False, time.time() + 180
        while time.time() < limit:
            time.sleep(2)
            current = gw.logs(200, since=started_at)
            if gw.THROTTLED in current:
                gw.compose("stop")
                set_state(phase="failed", active=None,
                          message="IBKR refused: too many failed attempts. "
                                  "Wait 15 minutes.")
                return
            if gw.LOGGED_IN.search(current):
                break
            if gw.DIALOG in current:
                need_code = True
                break
        else:
            set_state(phase="failed", active=None,
                      message="The gateway never reached the login dialog. "
                              "Press start to try again.")
            return
        step_done("waiting for the gateway", None)

        if need_code:
            code = None
            secret = gw.env_value("TOTP_SECRET")
            if secret:
                try:
                    import pyotp

                    left = 30 - (int(time.time()) % 30)
                    if left < 8:
                        time.sleep(left + 1)
                    code = pyotp.TOTP(secret.replace(" ", "")).now()
                    set_state(phase="typing", active="sending the code",
                              message="code generated from the stored secret")
                    step_done("authenticator code", "sending the code")
                except Exception:
                    # A mistyped secret must degrade to the normal prompt,
                    # never kill the login.
                    gw.write_env({"TOTP_SECRET": ""})
                    set_state(message="The stored TOTP secret was not valid "
                                      "and has been removed. Type the code "
                                      "from your authenticator app instead.")
            if code is None:
                set_state(phase="need_code", active="authenticator code",
                          deadline=time.time() + gw.CODE_WINDOW)
                _code_event.clear()
                if not _code_event.wait(timeout=gw.CODE_WINDOW - 30):
                    set_state(phase="failed", active=None, deadline=None,
                              message="No code arrived before the window "
                                      "closed. Press start to try again.")
                    return
                code = _code_value.pop()
                step_done("authenticator code", "sending the code")
                set_state(phase="typing", deadline=None,
                          message="Typing the code into the gateway's login "
                                  "screen for you.")
            gw.type_code(code)
            step_done("sending the code", "opening the API")
        else:
            step_done("authenticator code", None)
            step_done("sending the code", "opening the API")
            set_state(message="Session restored from a saved token, no code "
                              "needed.")

        set_state(phase="opening",
                  message="Code sent. IBKR is checking it and opening the "
                          "data connection. This is the slow part: it can "
                          "take a minute or two of nothing visibly happening, "
                          "which is normal. The page checks every few seconds "
                          "and will turn green by itself.")
        limit = time.time() + 150
        while time.time() < limit:
            time.sleep(3)
            accounts = gw.api_session()
            if accounts:
                step_done("opening the API", None)
                set_state(phase="logged_in", accounts=accounts, message="")
                return
            if gw.THROTTLED in gw.logs(60, since=started_at):
                break
        set_state(phase="failed", active=None,
                  message="The code went in but no session appeared. It was "
                          "probably stale or mistyped. Press start to try "
                          "again with a fresh one.")
    except Exception as exc:  # surface anything rather than dying silently
        set_state(phase="failed", active=None, message=f"unexpected: {exc}")
    finally:
        # IBC's separate Re-login dialog handler ignores the 2FA retry
        # setting. Failed UI attempts must not leave that loop running.
        with _lock:
            failed = _state["phase"] == "failed"
        if failed:
            try:
                gw.compose("stop")
            except Exception:
                with _lock:
                    _state["message"] += " Gateway cleanup failed; stop the container manually."


def start_login() -> str | None:
    global _worker
    if not (gw.env_value("TWS_USERID") and gw.env_value("TWS_PASSWORD")):
        set_state(phase="needs_creds", done=[], active="credentials",
                  accounts=[], message="")
        return "credentials first"
    if _worker and _worker.is_alive():
        return "already running"
    set_state(phase="starting", done=["credentials"],
              active="starting the container", accounts=[], message="",
              deadline=None)
    _worker = threading.Thread(target=login_worker, daemon=True)
    _worker.start()
    return None


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IBKR gateway</title><style>
:root{color-scheme:dark}
body{margin:0;background:#0d1117;color:#e6edf3;
  font:15px/1.5 system-ui,-apple-system,sans-serif;
  display:flex;justify-content:center;padding:40px 16px}
.card{width:100%;max-width:460px}
h1{font-size:19px;margin:0 0 2px}
.sub{color:#8b949e;font-size:13px;margin-bottom:22px}
.panel{background:#161b22;border:1px solid #30363d;border-radius:10px;
  padding:18px 20px;margin-bottom:14px}
.step{display:flex;gap:10px;align-items:center;padding:5px 0}
.dot{width:10px;height:10px;border-radius:50%;flex:none;
  border:2px solid #30363d}
.done .dot{background:#3fb950;border-color:#3fb950}
.fail .dot{background:#f85149;border-color:#f85149}
.active .dot{border-color:#58a6ff;animation:pulse 1s infinite}
@keyframes pulse{50%{box-shadow:0 0 0 5px rgba(88,166,255,.25)}}
.step span{color:#8b949e}.done span{color:#e6edf3}
.active span{color:#58a6ff;font-weight:600}
label{display:block;font-size:13px;color:#8b949e;margin:10px 0 4px}
input{width:100%;box-sizing:border-box;background:#0d1117;color:#e6edf3;
  border:1px solid #30363d;border-radius:8px;padding:10px 12px;font-size:15px}
input:focus{outline:none;border-color:#58a6ff}
input.code{font-size:30px;letter-spacing:12px;text-align:center;
  font-family:ui-monospace,monospace}
button{width:100%;margin-top:14px;background:#238636;color:#fff;border:0;
  border-radius:8px;padding:11px;font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#2ea043}
button.quiet{background:#21262d;color:#c9d1d9}
.msg{border-radius:8px;padding:10px 14px;font-size:14px;margin-bottom:14px}
.msg.err{background:#3d1d20;border:1px solid #f85149;color:#ffa198}
.msg.ok{background:#1b2f23;border:1px solid #3fb950;color:#7ee2a8}
.msg.info{background:#182437;border:1px solid #58a6ff;color:#a5c9ff}
.count{color:#8b949e;font-size:13px;text-align:center;margin-top:8px}
.hint{color:#8b949e;font-size:12.5px;margin-top:10px}
.mono{font-family:ui-monospace,monospace;background:#0d1117;
  border:1px solid #30363d;border-radius:6px;padding:2px 6px;font-size:12.5px}
.dim{color:#8b949e;font-size:12.5px;font-weight:400}
</style></head><body><div class="card">
<h1>IBKR gateway</h1><div class="sub">read-only market data session</div>
<div id="app">loading…</div>
</div><script>
const el=q=>document.getElementById(q);
const TOKEN='__CSRF__';
let S=null,lastSig='',cerr='';
async function post(u,b){const r=await fetch(u,{method:'POST',
  headers:{'Content-Type':'application/json','X-CSRF-Token':TOKEN},
  body:JSON.stringify(b||{})});
  let j={};try{j=await r.json();}catch(e){}
  cerr=r.ok?'':(j.error||'request failed');
  lastSig='';tick();}
function steps(){
  const names=["credentials","starting the container","waiting for the gateway",
    "authenticator code","sending the code","opening the API"];
  return '<div class="panel">'+names.map(n=>{
    let cls='',extra='';
    if(S.done.includes(n))cls='done';
    else if(S.active===n){
      cls=S.phase==='failed'?'fail':'active';
      if(cls==='active')extra=' <span id="elapsed" class="dim"></span>';
    }
    return `<div class="step ${cls}"><div class="dot"></div><span>${n}${extra}</span></div>`;
  }).join('')+'</div>';}
function render(){
  // A re-render rebuilds the inputs, so carry typed values across it.
  const keep={};['u','p','t','c'].forEach(k=>{const n=el(k);if(n)keep[k]=n.value;});
  let h='';
  if(cerr)h+=`<div class="msg err">${cerr}</div>`;
  if(S.message)h+=`<div class="msg ${S.phase==='failed'?'err':'info'}">${S.message}</div>`;
  if(S.phase==='logged_in'){
    h+=`<div class="msg ok">Logged in. Account <b>${S.accounts.join(', ')}</b> is
      serving on <span class="mono">127.0.0.1:4001</span>.</div>`+steps()+
      `<div class="hint">Pull something:<br><span class="mono">
      .venv/bin/python services/ibkr-data/check_connection.py AAPL MSFT</span></div>`;
  }else if(S.phase==='idle'||S.phase==='needs_creds'||
           (S.phase==='failed'&&!S.creds_present)){
    if(!S.creds_present){
      h+=`<div class="panel"><b>IBKR credentials</b>
      <div class="hint">Saved to deploy/.env on this machine only, owner-readable,
      ignored by git. The password is your IBKR website login.</div>
      <label>username</label><input id="u" autocomplete="username">
      <label>password</label><input id="p" type="password">
      <label>TOTP secret <span style="color:#586069">(optional, usually left
      empty. NOT the 6 digit code: the page asks for that later, once the
      gateway is up. This is the long base32 key behind the authenticator QR,
      for people who never want to be asked for codes.)</span></label>
      <input id="t" type="password" placeholder="leave empty">
      <button onclick="post('/creds',{u:el('u').value,p:el('p').value,
        t:el('t').value})">save and log in</button></div>`;
    }else{
      h+=steps()+`<button onclick="post('/start')">start login</button>
      <button class="quiet" onclick="post('/creds_clear')">change credentials</button>`;
    }
  }else if(S.phase==='need_code'){
    const left=Math.max(0,Math.floor(S.deadline-S.now));
    h+=steps()+`<div class="panel"><b>authenticator code</b>
    <div class="hint">Open the authenticator app and type a fresh 6 digit code.</div>
    <input id="c" class="code" maxlength="6" inputmode="numeric" autofocus
      oninput="if(this.value.length===6)post('/code',{c:this.value})">
    <div class="count">window closes in ${Math.floor(left/60)}m ${left%60}s</div></div>`;
  }else if(S.phase==='failed'){
    h+=steps()+`<button onclick="post('/start')">start again</button>`;
  }else{
    h+=steps();
  }
  el('app').innerHTML=h;
  Object.entries(keep).forEach(([k,v])=>{const n=el(k);if(n&&v)n.value=v;});
  const c=el('c');if(c)c.focus();}
function countdown(){
  const n=document.querySelector('.count');
  if(n&&S&&S.deadline){
    const left=Math.max(0,Math.floor(S.deadline-S.now));
    n.textContent=`window closes in ${Math.floor(left/60)}m ${left%60}s`;}
  const a=el('elapsed');
  if(a&&S&&S.phase_ts){
    a.textContent=`· ${Math.max(0,Math.floor(S.now-S.phase_ts))}s`;}}
async function tick(){
  try{S=await (await fetch('/state')).json();}catch(e){return;}
  // Re-render only when the state really changed. innerHTML replacement
  // destroys the inputs, so rendering on every poll wipes whatever the
  // user is halfway through typing.
  const sig=JSON.stringify([S.phase,S.done,S.active,S.message,S.accounts,
    S.creds_present,S.totp_present]);
  if(sig!==lastSig){lastSig=sig;render();}
  countdown();}
setInterval(tick,1500);tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    def _host_ok(self) -> bool:
        """DNS rebinding guard: a page from evil.example resolved to 127.0.0.1
        arrives with its own Host header, so an exact allowlist stops it."""
        if self.headers.get("Host", "") in ALLOWED_HOSTS:
            return True
        self._json({"error": "bad host"}, 421)
        return False

    def _json(self, obj, status=200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        if self.path == "/state":
            self._json(get_state())
            return
        body = PAGE.replace("__CSRF__", CSRF_TOKEN).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._host_ok():
            return
        if self.headers.get("X-CSRF-Token", "") != CSRF_TOKEN:
            self._json({"error": "bad token"}, 403)
            return
        data = self._body()
        if self.path == "/creds":
            user = (data.get("u") or "").strip()
            password = (data.get("p") or "").strip()
            secret = (data.get("t") or "").strip().replace(" ", "")
            if not user or not password:
                self._json({"error": "both username and password are needed"},
                           400)
                return
            values = {"TWS_USERID": user, "TWS_PASSWORD": password}
            if secret:
                import base64

                try:
                    base64.b32decode(secret.upper() + "=" * (-len(secret) % 8),
                                     casefold=True)
                except Exception:
                    self._json({"error":
                                "That does not look like a TOTP secret (a long "
                                "base32 key: letters A-Z and digits 2-7). If "
                                "you meant the 6 digit login code, leave this "
                                "field empty; the page asks for the code later, "
                                "after the gateway is up."}, 400)
                    return
                values["TOTP_SECRET"] = secret
            gw.write_env(values)
            start_login()
            self._json({"ok": True})
        elif self.path == "/creds_clear":
            gw.write_env({"TWS_USERID": "", "TWS_PASSWORD": ""})
            set_state(phase="needs_creds", done=[], active="credentials",
                      message="", accounts=[])
            self._json({"ok": True})
        elif self.path == "/start":
            err = start_login()
            self._json({"ok": err is None, "error": err})
        elif self.path == "/code":
            code = (data.get("c") or "").strip()
            if not (code.isdigit() and len(code) == 6):
                self._json({"error": "six digits"}, 400)
                return
            _code_value.append(code)
            _code_event.set()
            self._json({"ok": True})
        else:
            self._json({"error": "unknown"}, 404)


def session_watchdog() -> None:
    """Flip a stale logged_in back to idle when the session dies underneath
    us. The gateway restarts itself nightly and IBKR resets tokens weekly,
    so a page left open overnight would otherwise show green forever."""
    while True:
        time.sleep(60)
        with _lock:
            phase = _state["phase"]
        if phase == "logged_in" and gw.api_session() is None:
            set_state(phase="idle", done=["credentials"], active=None,
                      accounts=[],
                      message="The gateway session has ended (nightly restart "
                              "or the weekly Sunday reset). Press start login "
                              "to bring it back.")


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=session_watchdog, daemon=True).start()
    print(f"IBKR gateway login page:  http://{HOST}:{PORT}")
    accounts = gw.api_session()
    if accounts:
        set_state(phase="logged_in", done=list(STEPS), accounts=accounts)
    elif gw.env_value("TWS_USERID") and gw.env_value("TWS_PASSWORD"):
        set_state(phase="idle", done=["credentials"], active=None)
    else:
        set_state(phase="needs_creds", active="credentials")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
