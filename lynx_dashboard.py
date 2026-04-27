#!/usr/bin/env python3
"""
BriskHeat LYNX – The One Everyone Loves
Real-Time Dashboard + WebSocket + Full API + Auto-Install + Debug Mode
Your favorite version, now perfect
"""

# ========= AUTO-INSTALL DEPENDENCIES =========
import subprocess, sys
def install(p):
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", p])

missing = []
for pkg, imp in [("flask", "flask"), ("flask-socketio", "flask_socketio"), ("pymodbus", "pymodbus"), ("eventlet", "eventlet")]:
    try: __import__(imp)
    except: missing.append(pkg)

if missing:
    print("Installing missing packages:", ", ".join(missing))
    for p in missing: install(p)
    print("All installed! Starting dashboard...\n")

# ========= IMPORTS =========
from flask import Flask, render_template_string, request, jsonify, abort
from flask_socketio import SocketIO, emit
import threading, time
from datetime import datetime

# Your reader class (must be in same folder)
from lynx_reader import LynxTemperatureSystem

# ========= CONFIG – CHANGE ONLY THIS =========
OI_HOST = "192.168.200.20"      # ←←← YOUR OI GATEWAY IP
OI_PORT = 502
MODBUS_TIMEOUT = 4.0

# ========= SETUP =========
app = Flask(__name__)
app.config['SECRET_KEY'] = 'BriskHeat2025'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')  # eventlet = best performance

system = LynxTemperatureSystem(host=OI_HOST, port=OI_PORT, timeout=MODBUS_TIMEOUT, lines=(1,2,3,4))

latest_data = []
last_update = 0
data_lock = threading.Lock()
scanner_running = False

def background_scanner():
    global latest_data, last_update, scanner_running
    scanner_running = True
    print(f"Live scanner started → polling {OI_HOST} every 8s")

    # Silence the huge print table from read_all_zones()
    import builtins
    orig_print = builtins.print
    builtins.print = lambda *a, **k: None

    while scanner_running:
        try:
            start = time.time()
            data = system.read_all_zones()  # auto-connects

            with data_lock:
                latest_data = data
                last_update = time.time()

            elapsed = time.time() - start
            socketio.emit('update', {
                'zones': data,
                'human_time': datetime.now().strftime("%H:%M:%S"),
                'count': len(data),
                'scan_time': round(elapsed, 2)
            }, namespace='/live')

            print(f"Sent {len(data)} zones ({elapsed:.2f}s)")

        except Exception as e:
            print(f"Scanner ERROR: {e}")
            socketio.emit('error', {'message': str(e)}, namespace='/live')
        time.sleep(8)

    builtins.print = orig_print

# ========= WEBSOCKET EVENTS =========
@socketio.on('connect', namespace='/live')
def on_connect():
    global scanner_running
    if not scanner_running:
        threading.Thread(target=background_scanner, daemon=True).start()
    emit('connected', {'message': 'Live feed active'})

# ========= DASHBOARD =========
HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>BriskHeat LYNX – Live Monitor</title>
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
  body{font-family:Arial,sans-serif;margin:20px;background:#f8f9fa;color:#333}
  h1{color:#2c3e50;margin-bottom:5px}
  .header{display:flex;justify-content:space-between;align-items:center;background:#3498db;color:white;padding:15px 25px;border-radius:8px}
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
  <div><span id="status">Connecting...</span><span id="spin" class="spinner"></span> <span id="time">Never</span></div>
</div>

<table id="t"><thead><tr>
  <th>Line</th><th>Zone</th><th>SP (°C)</th><th>PV (°C)</th><th>Out (%)</th><th>A</th><th>Status</th><th>New SP</th><th>Set</th>
</tr></thead><tbody><tr><td colspan="9">Waiting for first scan...</td></tr></tbody></table>
<div class="footer">OI Gateway: {{host}}:{{port}} • Updates every ~8s</div>

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
    return render_template_string(HTML, host=OI_HOST, port=OI_PORT)

# ========= API =========
@app.route("/api/status")
def status():
    with data_lock:
        return jsonify({"zones": latest_data, "updated": last_update})

@app.route("/api/setpoint", methods=["POST"])
def set_sp():
    try:
        j = request.get_json() or request.form
        updates = j.get("updates")  # Array: [{"line":1, "zone":1, "sp":25.0}, ...] for batch
        single_sp = j.get("sp")     # Fallback for single: {"line":1, "zone":1, "sp":25.0}
        
        if updates:
            # Batch mode
            if not isinstance(updates, list):
                raise ValueError("updates must be a list")
            results = []
            for upd in updates:
                line, zone, sp = int(upd["line"]), int(upd["zone"]), float(upd["sp"])
                result = _perform_setpoint_write(line, zone, sp)
                results.append(result)
            return jsonify({"success": all(r["success"] for r in results), "details": results})
        
        elif single_sp:
            # Single mode (backward compat)
            line, zone = int(j["line"]), int(j["zone"])
            sp = float(single_sp)
            result = _perform_setpoint_write(line, zone, sp)
            return jsonify(result)
        
        else:
            raise ValueError("Provide 'updates' array or single 'line/zone/sp'")
    
    except Exception as e:
        print(f"Setpoint ERROR: {e}")
        return jsonify({"error": str(e)}), 500

def _perform_setpoint_write(line, zone, sp):
    """Internal helper for one setpoint write"""
    scaled = int(round(sp * 100))
  
    addr = system._calc_base_address(line, zone)
    
    print(f"WRITE: L{line}-Z{zone} | SP:{sp} | Addr:{addr} | DevID:{system.unit_id} | Val:{scaled}")

    resp = system.client.write_register(addr, scaled, device_id=system.unit_id)
    if resp.isError():
        err = str(resp)
        if hasattr(resp, "exception_code"):
            err += f" (code {resp.exception_code})"
        raise Exception(err)
    
    return {"success": True, "line": line, "zone": zone, "setpoint_c": round(sp, 1)}

# ========= START =========
if __name__ == "__main__":
    print("\n" + "="*60)
    print("   BriskHeat LYNX – Ultimate Real-Time Dashboard")
    print("="*60)
    print(f"   Open → http://YOUR_PC_IP:5000")
    print(f"   API   → http://YOUR_PC_IP:5000/api/status")
    print("   Works on phones, tablets, any browser")
    print("="*60 + "\n")
    socketio.run(app, host="0.0.0.0", port=5000)
