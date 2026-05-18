#!/usr/bin/env python3
"""
BriskHeat LYNX – Real-Time Dashboard + PostgreSQL History
=========================================================
Changes vs. original:
  • Logs zone readings to PostgreSQL every 60 s  (lynx_db_logger.py)
  • Adds /history web page with trend charts      (lynx_history.py)
  • Adds /api/history and /api/history/csv routes (lynx_history.py)
"""

# ========= AUTO-INSTALL DEPENDENCIES =========
import subprocess, sys

def install(p):
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", p])

missing = []
for pkg, imp in [
    ("flask",         "flask"),
    ("flask-socketio","flask_socketio"),
    ("pymodbus",      "pymodbus"),
    ("eventlet",      "eventlet"),
    ("psycopg2-binary","psycopg2"),
]:
    try:
        __import__(imp)
    except ImportError:
        missing.append(pkg)

if missing:
    print("Installing missing packages:", ", ".join(missing))
    for p in missing:
        install(p)
    print("All installed! Starting dashboard...\n")

# ========= IMPORTS =========
from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO, emit
import threading, time, configparser, os
from datetime import datetime

from lynx_reader import LynxTemperatureSystem
from lynx_db_logger import LynxDBLogger
from lynx_history  import history_bp, init_history_db

# ========= CONFIG – loaded from config.ini =========
_CFG_FILE = os.path.join(os.path.dirname(__file__), "config.ini")
cfg = configparser.ConfigParser()
if not cfg.read(_CFG_FILE):
    raise FileNotFoundError(f"config.ini not found at {_CFG_FILE}")

OI_HOST         = cfg.get    ("modbus",   "host")
OI_PORT         = cfg.getint ("modbus",   "port")
MODBUS_TIMEOUT  = cfg.getfloat("modbus",  "timeout")
SCAN_INTERVAL   = cfg.getint ("modbus",   "scan_interval", fallback=8)
_LINES          = tuple(int(x) for x in cfg.get("modbus", "lines").split(","))

DB_HOST         = cfg.get    ("database", "host")
DB_PORT         = cfg.getint ("database", "port")
DB_NAME         = cfg.get    ("database", "name")
DB_USER         = cfg.get    ("database", "user")
DB_PASSWORD     = cfg.get    ("database", "password")
DB_LOG_INTERVAL = cfg.getint ("database", "log_interval")
DB_ADMIN_USER   = cfg.get    ("database", "admin_user",      fallback="postgres")
DB_ADMIN_PASS   = cfg.get    ("database", "admin_password",  fallback="")
DB_PURGE_THRESH = cfg.getfloat("database", "purge_threshold", fallback=80.0)
DB_PURGE_KEEP   = cfg.getfloat("database", "purge_keep_pct",  fallback=60.0)

FLASK_SECRET    = cfg.get    ("flask",    "secret_key")
FLASK_PORT      = cfg.getint ("flask",    "port")

# ========= SETUP =========
app = Flask(__name__)
app.config['SECRET_KEY'] = FLASK_SECRET
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Register history blueprint (adds /history, /api/history, /api/history/csv)
app.register_blueprint(history_bp)
init_history_db(
    host=DB_HOST, port=DB_PORT,
    dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
)

# DB logger (submit() is non-blocking – safe to call from scanner thread)
db_logger = LynxDBLogger(
    host=DB_HOST, port=DB_PORT,
    dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
    interval_sec=DB_LOG_INTERVAL,
    admin_user=DB_ADMIN_USER, admin_password=DB_ADMIN_PASS,
    purge_threshold=DB_PURGE_THRESH,
    purge_keep_pct=DB_PURGE_KEEP,
    mount_path=os.path.dirname(os.path.abspath(__file__)),
)
db_logger.start()

system = LynxTemperatureSystem(
    host=OI_HOST, port=OI_PORT,
    timeout=MODBUS_TIMEOUT, lines=_LINES
)
latest_data  = []
last_update  = 0
data_lock    = threading.Lock()
settings_lock   = threading.Lock()
scanner_running = False


def background_scanner():
    global latest_data, last_update, scanner_running
    scanner_running = True

    import builtins
    orig_print = builtins.print
    builtins.print = lambda *a, **k: None

    while scanner_running:
        with settings_lock:
            host     = OI_HOST
            port     = OI_PORT
            timeout  = MODBUS_TIMEOUT
            interval = SCAN_INTERVAL

        # Reconnect if host/port/timeout changed
        if (system.host != host or system.port != port
                or system.timeout != timeout):
            try: system.close()
            except Exception: pass
            system.__init__(host=host, port=port,
                            timeout=timeout, lines=_LINES)

        try:
            start = time.time()
            data  = system.read_all_zones()

            with data_lock:
                latest_data = data
                last_update = time.time()

            elapsed = time.time() - start

            socketio.emit('update', {
                'zones':      data,
                'human_time': datetime.now().strftime("%H:%M:%S"),
                'count':      len(data),
                'scan_time':  round(elapsed, 2),
            }, namespace='/live')

            db_logger.submit(data)

        except Exception as e:
            orig_print(f"Scanner ERROR: {e}")
            socketio.emit('error', {'message': str(e)}, namespace='/live')

        time.sleep(interval)

    builtins.print = orig_print


# ========= WEBSOCKET EVENTS =========
@socketio.on('connect', namespace='/live')
def on_connect():
    global scanner_running
    if not scanner_running:
        threading.Thread(target=background_scanner, daemon=True).start()
    emit('connected', {'message': 'Live feed active'})


# ========= DASHBOARD HTML =========
HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>BriskHeat LYNX – Live Monitor</title>
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
body{font-family:Arial,sans-serif;margin:20px;background:#f8f9fa;color:#333}
h1{color:#2c3e50;margin-bottom:5px}
.header{display:flex;justify-content:space-between;align-items:center;background:#3498db;color:white;padding:15px 25px;border-radius:8px}
.header-links a{color:#d6eaf8;text-decoration:none;margin-left:18px;font-size:.9rem}
.header-links a:hover{color:#fff}
table{width:100%;border-collapse:collapse;margin-top:20px;background:white;box-shadow:0 4px 20px rgba(0,0,0,.1);border-radius:8px;overflow:hidden}
th{background:#2c3e50;color:white;padding:12px}
td{padding:10px;text-align:center}
tr:nth-child(even){background:#f8f9fa}
.ok{background:#d4edda}.heating{background:#fff3cd}.over{background:#f8d7da;color:#721c24}.notc{background:#e2e3e5;color:#6c757d}
input[type=number]{width:80px;padding:6px;border:1px solid #ccc;border-radius:4px}
button{padding:6px 12px;background:#28a745;color:white;border:none;border-radius:4px;cursor:pointer}
button:hover{background:#218838}
.footer{margin-top:20px;text-align:center;color:#666;font-size:0.9em}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid #f3f3f3;border-top:2px solid #3498db;border-radius:50%;animation:s 1s linear infinite;margin-left:8px}
@keyframes s{to{transform:rotate(360deg)}}
</style></head><body>
<div class="header">
  <h1>BriskHeat LYNX – Real-Time Dashboard</h1>
  <div style="display:flex;align-items:center;gap:16px">
    <div><span id="status">Connecting...</span><span id="spin" class="spinner"></span> <span id="time">Never</span></div>
    <div class="header-links">
      <a href="/history">📈 History</a>
      <a href="/settings">⚙ Settings</a>
    </div>
  </div>
</div>
<table id="t"><thead><tr>
<th>Line</th><th>Zone</th><th>SP (°C)</th><th>PV (°C)</th><th>Out (%)</th><th>A</th><th>Status</th><th>New SP</th><th>Set</th>
</tr></thead><tbody><tr><td colspan="9">Waiting for first scan...</td></tr></tbody></table>
<div class="footer">OI Gateway: {{host}}:{{port}} • Updates every ~8s • DB logging every {{interval}}s</div>
<script>
const socket=io('/live'), tb=document.querySelector('#t tbody'), st=document.getElementById('status'), tm=document.getElementById('time'), sp=document.getElementById('spin');
socket.on('connected',()=>{st.textContent='LIVE';st.style.color='limegreen';sp.style.display='none'});
socket.on('update',d=>{
  tm.textContent=d.human_time;
  tb.innerHTML='';
  if(d.zones.length===0){tb.innerHTML='<tr><td colspan="9">No active zones</td></tr>';return;}
  d.zones.forEach(z=>{
    const r=document.createElement('tr');
    r.className = z.status==='OK'?'ok':z.status==='HEATING'?'heating':z.status.includes('OVER')?'over':'notc';
    r.innerHTML=`<td>${z.line}</td><td>${z.zone}</td><td>${z.setpoint?.toFixed(1)||'--'}</td>
    <td>${z.pv?.toFixed(1)||'--'}</td><td>${z.output_percent??'--'}</td><td>${z.current?.toFixed(2)||'--'}</td>
    <td><b>${z.status}</b></td>
    <td><input type="number" step="0.1" value="${z.setpoint?.toFixed(1)||''}" style="width:70px"></td>
    <td><button onclick="set(this,${z.line},${z.zone})">Set</button></td>`;
    tb.appendChild(r);
  });
});
function set(b,l,z){
  const v=b.closest('tr').querySelector('input').value;
  if(!v)return alert("Enter setpoint");
  fetch('/api/setpoint',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({line:l,zone:z,sp:+v})
  }).then(r=>r.json()).then(d=>d.success&&alert('Setpoint → '+d.setpoint_c+'°C'));
}
</script></body></html>"""


@app.route("/")
def index():
    return render_template_string(
        HTML, host=OI_HOST, port=OI_PORT, interval=DB_LOG_INTERVAL
    )


# ========= API =========
@app.route("/api/status")
def status():
    with data_lock:
        return jsonify({"zones": latest_data, "updated": last_update})


@app.route("/api/setpoint", methods=["POST"])
def set_sp():
    try:
        j       = request.get_json() or request.form
        updates = j.get("updates")
        single_sp = j.get("sp")

        if updates:
            if not isinstance(updates, list):
                raise ValueError("updates must be a list")
            results = [_perform_setpoint_write(
                int(u["line"]), int(u["zone"]), float(u["sp"])
            ) for u in updates]
            return jsonify({"success": all(r["success"] for r in results),
                            "details": results})

        elif single_sp is not None:
            line, zone = int(j["line"]), int(j["zone"])
            return jsonify(_perform_setpoint_write(line, zone, float(single_sp)))

        else:
            raise ValueError("Provide 'updates' array or single 'line/zone/sp'")

    except Exception as e:
        print(f"Setpoint ERROR: {e}")
        return jsonify({"error": str(e)}), 500


def _perform_setpoint_write(line, zone, sp):
    scaled = int(round(sp * 100))
    addr   = system._calc_base_address(line, zone)
    print(f"WRITE: L{line}-Z{zone} | SP:{sp} | Addr:{addr} | Val:{scaled}")
    resp   = system.client.write_register(addr, scaled, device_id=system.unit_id)
    if resp.isError():
        err = str(resp)
        if hasattr(resp, "exception_code"):
            err += f" (code {resp.exception_code})"
        raise Exception(err)
    return {"success": True, "line": line, "zone": zone, "setpoint_c": round(sp, 1)}


# ========= SETTINGS PAGE =========
SETTINGS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>LYNX – Settings</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:Arial,sans-serif;background:#f0f2f5;color:#333}
  .header{background:#2c3e50;color:#fff;padding:14px 24px;display:flex;align-items:center;justify-content:space-between}
  .header h1{font-size:1.2rem;font-weight:700}
  .header a{color:#aed6f1;text-decoration:none;font-size:.85rem}
  .header a:hover{color:#fff}
  .card{background:#fff;border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,.08);
        padding:28px 32px;margin:24px auto;max-width:520px}
  .card h2{font-size:1rem;font-weight:700;color:#2c3e50;margin-bottom:20px;
           padding-bottom:10px;border-bottom:2px solid #f0f2f5}
  .field{margin-bottom:16px}
  .field label{display:block;font-size:.8rem;font-weight:600;color:#555;
               text-transform:uppercase;letter-spacing:.04em;margin-bottom:5px}
  .field input{width:100%;padding:9px 12px;border:1px solid #ccc;border-radius:5px;
               font-size:.95rem;background:#fafafa;transition:border .15s}
  .field input:focus{outline:none;border-color:#3498db;background:#fff}
  .field .hint{font-size:.75rem;color:#999;margin-top:4px}
  .actions{display:flex;gap:12px;margin-top:24px}
  .btn{flex:1;padding:10px;border:none;border-radius:5px;cursor:pointer;
       font-size:.95rem;font-weight:600}
  .btn-primary{background:#3498db;color:#fff}.btn-primary:hover{background:#2980b9}
  .btn-test{background:#8e44ad;color:#fff}.btn-test:hover{background:#7d3c98}
  .msg{padding:10px 14px;border-radius:5px;margin-top:16px;font-size:.9rem;display:none}
  .msg-ok{background:#d4edda;color:#155724}
  .msg-err{background:#f8d7da;color:#721c24}
  .msg-info{background:#d1ecf1;color:#0c5460}
</style></head><body>
<div class="header">
  <h1>⚙ LYNX Settings</h1>
  <a href="/">← Live Dashboard</a>
</div>
<div class="card">
  <h2>Modbus / OI Gateway</h2>
  <div class="field">
    <label>Host (IP Address)</label>
    <input type="text" id="oi_host" placeholder="192.168.200.20">
  </div>
  <div class="field">
    <label>Port</label>
    <input type="number" id="oi_port" min="1" max="65535" placeholder="502">
  </div>
  <div class="field">
    <label>Timeout (seconds)</label>
    <input type="number" id="oi_timeout" min="0.5" max="30" step="0.5" placeholder="4.0">
  </div>
  <div class="field">
    <label>Scan Interval (seconds)</label>
    <input type="number" id="scan_interval" min="4" max="3600" placeholder="8">
    <div class="hint">How often the scanner polls the OI Gateway (minimum 4 s)</div>
  </div>
  <div class="field">
    <label>DB Log Interval (seconds)</label>
    <input type="number" id="db_interval" min="10" max="86400" placeholder="60">
    <div class="hint">How often zone data is saved to PostgreSQL (minimum 10 s)</div>
  </div>
  <div class="actions">
    <button class="btn btn-test" onclick="testConn()">🔌 Test Connection</button>
    <button class="btn btn-primary" onclick="saveSettings()">💾 Save & Apply</button>
  </div>
  <div class="msg" id="msg"></div>
</div>
<script>
fetch("/api/settings").then(r=>r.json()).then(d=>{
  document.getElementById("oi_host").value       = d.oi_host;
  document.getElementById("oi_port").value       = d.oi_port;
  document.getElementById("oi_timeout").value    = d.oi_timeout;
  document.getElementById("scan_interval").value = d.scan_interval;
  document.getElementById("db_interval").value    = d.db_interval;
});
function showMsg(text, type) {
  const el = document.getElementById("msg");
  el.textContent = text;
  el.className = "msg msg-" + type;
  el.style.display = "block";
  if (type === "ok") setTimeout(() => el.style.display="none", 3000);
}
function getValues() {
  return {
    oi_host:       document.getElementById("oi_host").value.trim(),
    oi_port:       parseInt(document.getElementById("oi_port").value),
    oi_timeout:    parseFloat(document.getElementById("oi_timeout").value),
    scan_interval: parseInt(document.getElementById("scan_interval").value),
    db_interval:   parseInt(document.getElementById("db_interval").value),
  };
}
async function testConn() {
  showMsg("Testing connection...", "info");
  try {
    const r = await fetch("/api/settings/test", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify(getValues())
    });
    const d = await r.json();
    if (d.success) showMsg("✓ Connected! Found " + d.zone_count + " zone(s).", "ok");
    else showMsg("✗ " + d.error, "err");
  } catch(e) { showMsg("✗ " + e.message, "err"); }
}
async function saveSettings() {
  showMsg("Saving...", "info");
  try {
    const r = await fetch("/api/settings", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify(getValues())
    });
    const d = await r.json();
    if (d.success) showMsg("✓ Settings saved and applied.", "ok");
    else showMsg("✗ " + d.error, "err");
  } catch(e) { showMsg("✗ " + e.message, "err"); }
}
</script></body></html>"""


@app.route("/settings")
def settings_page():
    return render_template_string(SETTINGS_HTML)


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    with settings_lock:
        return jsonify({
            "oi_host":       OI_HOST,
            "oi_port":       OI_PORT,
            "oi_timeout":    MODBUS_TIMEOUT,
            "scan_interval": SCAN_INTERVAL,
            "db_interval":   DB_LOG_INTERVAL,
        })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    global OI_HOST, OI_PORT, MODBUS_TIMEOUT, SCAN_INTERVAL, DB_LOG_INTERVAL
    try:
        j = request.get_json()
        host     = str(j["oi_host"]).strip()
        port     = int(j["oi_port"])
        timeout  = float(j["oi_timeout"])
        interval = int(j["scan_interval"])

        if not host:             raise ValueError("Host cannot be empty")
        if not 1 <= port <= 65535: raise ValueError("Port must be 1–65535")
        if timeout < 0.5:        raise ValueError("Timeout must be >= 0.5 s")
        if interval < 4:         raise ValueError("Scan interval must be >= 4 s")

        db_interval = int(j["db_interval"])
        if db_interval < 10: raise ValueError("DB interval must be >= 10 s")

        with settings_lock:
            OI_HOST        = host
            OI_PORT        = port
            MODBUS_TIMEOUT = timeout
            SCAN_INTERVAL  = interval
            DB_LOG_INTERVAL = db_interval
            db_logger._interval = db_interval   # takes effect on next write cycle

        cfg.read(_CFG_FILE)
        cfg.set("modbus",    "host",          host)
        cfg.set("modbus",    "port",          str(port))
        cfg.set("modbus",    "timeout",       str(timeout))
        cfg.set("modbus",    "scan_interval", str(interval))
        cfg.set("database",  "log_interval",  str(db_interval))
        with open(_CFG_FILE, "w") as f:
            cfg.write(f)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/settings/test", methods=["POST"])
def api_settings_test():
    try:
        j = request.get_json()
        test_sys = LynxTemperatureSystem(
            host=str(j["oi_host"]).strip(),
            port=int(j["oi_port"]),
            timeout=float(j["oi_timeout"]),
            lines=_LINES
        )
        if not test_sys.connect():
            return jsonify({"success": False,
                            "error": f"Cannot connect to {j['oi_host']}:{j['oi_port']}"}), 200
        data = test_sys.read_all_zones()
        test_sys.close()
        return jsonify({"success": True, "zone_count": len(data)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 200


# ========= START =========
def run():
    print("\n" + "=" * 60)
    print("  BriskHeat LYNX – Dashboard + PostgreSQL Logging")
    print("=" * 60)
    print(f"  Config     → {_CFG_FILE}")
    print(f"  Dashboard  → http://YOUR_PC_IP:{FLASK_PORT}")
    print(f"  History    → http://YOUR_PC_IP:{FLASK_PORT}/history")
    print(f"  API status → http://YOUR_PC_IP:{FLASK_PORT}/api/status")
    print(f"  DB logging → {DB_HOST}/{DB_NAME} every {DB_LOG_INTERVAL}s")
    print("=" * 60 + "\n")
    socketio.run(app, host="0.0.0.0", port=FLASK_PORT)

if __name__ == "__main__":
    run()
