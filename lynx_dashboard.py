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
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import threading, time, configparser, os
from datetime import datetime

from lynx_reader import LynxTemperatureSystem
from lynx_db_logger import LynxDBLogger
import lynx_history
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
LINE_VOLTAGE    = cfg.getfloat("modbus",   "line_voltage",  fallback=240.0)
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
    dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
    line_voltage=LINE_VOLTAGE
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


@app.route("/")
def index():
    return render_template('dashboard.html', host=OI_HOST, port=OI_PORT, interval=DB_LOG_INTERVAL)


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


@app.route("/settings")
def settings_page():
    return render_template('settings.html')


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    with settings_lock:
        return jsonify({
            "oi_host":       OI_HOST,
            "oi_port":       OI_PORT,
            "oi_timeout":    MODBUS_TIMEOUT,
            "scan_interval": SCAN_INTERVAL,
            "db_interval":   DB_LOG_INTERVAL,
            "line_voltage":  LINE_VOLTAGE,
        })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    global OI_HOST, OI_PORT, MODBUS_TIMEOUT, SCAN_INTERVAL, DB_LOG_INTERVAL, LINE_VOLTAGE
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

        db_interval  = int(j["db_interval"])
        line_voltage = float(j["line_voltage"])
        if db_interval < 10:   raise ValueError("DB interval must be >= 10 s")
        if line_voltage <= 0:  raise ValueError("Line voltage must be > 0")

        with settings_lock:
            OI_HOST        = host
            OI_PORT        = port
            MODBUS_TIMEOUT = timeout
            SCAN_INTERVAL  = interval
            DB_LOG_INTERVAL          = db_interval
            LINE_VOLTAGE             = line_voltage
            lynx_history._line_voltage = line_voltage
            db_logger._interval      = db_interval

        cfg.read(_CFG_FILE)
        cfg.set("modbus",    "host",          host)
        cfg.set("modbus",    "port",          str(port))
        cfg.set("modbus",    "timeout",       str(timeout))
        cfg.set("modbus",    "scan_interval", str(interval))
        cfg.set("database",  "log_interval",  str(db_interval))
        cfg.set("modbus",    "line_voltage",  str(line_voltage))
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
