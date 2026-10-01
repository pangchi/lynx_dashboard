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
    # --break-system-packages is needed on Bookworm outside a venv; harmless to omit inside one
    cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
    if not hasattr(sys, "real_prefix") and not (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix):
        cmd.append("--break-system-packages")
    cmd.append(p)
    subprocess.check_call(cmd)

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
import threading, time, configparser, os, logging, traceback as _tb
from datetime import datetime

from lynx_reader import LynxTemperatureSystem
from lynx_db_logger import LynxDBLogger
import lynx_history
from lynx_history  import history_bp, init_history_db
from lynx_schedule import schedule_bp, start_scheduler, update_scheduler_tz

# ========= CONFIG – loaded from config.ini =========
# APP_DIR may be set by app.py before import to ensure purge monitors
# the partition where app.py lives, not necessarily lynx_dashboard.py.
APP_DIR  = os.path.dirname(os.path.abspath(__file__))
_CFG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
cfg = configparser.ConfigParser()
if not cfg.read(_CFG_FILE):
    raise FileNotFoundError(f"config.ini not found at {_CFG_FILE}")

OI_HOST         = cfg.get    ("modbus",   "host")
OI_PORT         = cfg.getint ("modbus",   "port")
MODBUS_TIMEOUT  = cfg.getfloat("modbus",  "timeout")
SCAN_INTERVAL   = cfg.getint ("modbus",   "scan_interval", fallback=8)
LINE_VOLTAGE    = cfg.getfloat("modbus",   "line_voltage",  fallback=240.0)
MODBUS_MODE     = cfg.get    ("modbus",   "mode",          fallback="tcp").lower()
DEVICE_TYPE     = cfg.get    ("modbus",   "device_type",   fallback="lynx").lower()
C2_MAX_UNITS    = cfg.getint ("modbus",   "c2_max_units",  fallback=128)
C2_PASSWORD     = cfg.get    ("modbus",   "c2_password",   fallback="briskheat")
C2_DUMP_SECS    = cfg.getint ("modbus",   "c2_dump_secs",  fallback=3)
NOMINAL_POWER_W = cfg.getfloat("modbus",  "nominal_power_w", fallback=0.0)
_LINES          = tuple(int(x) for x in cfg.get("modbus", "lines").split(","))

SERIAL_PORT     = cfg.get    ("serial",   "port",          fallback="/dev/ttyUSB0")
SERIAL_BAUD     = cfg.getint ("serial",   "baudrate",      fallback=9600)
SERIAL_BYTESIZE = cfg.getint ("serial",   "bytesize",      fallback=8)
SERIAL_PARITY   = cfg.get    ("serial",   "parity",        fallback="N")
SERIAL_STOPBITS = cfg.getint ("serial",   "stopbits",      fallback=1)

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
API_KEY         = cfg.get    ("flask",    "api_key",    fallback="")

# ========= SETUP =========
app = Flask(__name__)
app.config['SECRET_KEY'] = FLASK_SECRET
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ── API key authentication ────────────────────────────────────────────────────
@app.before_request
def _require_api_key():
    """Protect all /api/* routes with an optional API key.
    If api_key is blank in config.ini, auth is disabled.
    Requests from the web pages (browser session / Referer) pass through.
    Key can be supplied via:
      - Header:       X-API-Key: <key>
      - Query param:  ?api_key=<key>
    """
    if not API_KEY:
        return   # auth disabled
    if not request.path.startswith("/api/"):
        return   # only protect API routes
    # Allow requests that originated from the dashboard itself
    # (SocketIO, inline setpoint buttons, settings page JS)
    referer = request.headers.get("Referer", "")
    if referer and request.host in referer:
        return
    provided = (request.headers.get("X-API-Key") or
                request.args.get("api_key") or
                request.json.get("api_key") if request.is_json else None)
    if provided != API_KEY:
        return jsonify({"error": "Unauthorized — invalid or missing API key",
                        "hint":  "Pass X-API-Key header or ?api_key= query param"}), 401


@app.route("/api/settings/detect_baud", methods=["POST"])
def api_detect_baud():
    """Auto-detect baud rate on the configured serial port."""
    from lynx_reader import auto_detect_baud
    j = request.get_json(force=True, silent=True) or {}
    port    = j.get("serial_port", SERIAL_PORT)
    dtype   = j.get("device_type", DEVICE_TYPE)
    timeout = float(j.get("timeout", 1.5))
    baud = auto_detect_baud(port, dtype, timeout)
    if baud:
        return jsonify({"success": True,  "baud": baud})
    return jsonify({"success": False, "error": f"No response on {port}"}), 200


# Register history blueprint (adds /history, /api/history, /api/history/csv)
app.register_blueprint(history_bp)
app.register_blueprint(schedule_bp)
init_history_db(
    host=DB_HOST, port=DB_PORT,
    dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
    line_voltage=LINE_VOLTAGE,
    nominal_power_w=NOMINAL_POWER_W
)

# DB logger (submit() is non-blocking – safe to call from scanner thread)
db_logger = LynxDBLogger(
    host=DB_HOST, port=DB_PORT,
    dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
    interval_sec=DB_LOG_INTERVAL,
    admin_user=DB_ADMIN_USER, admin_password=DB_ADMIN_PASS,
    purge_threshold=DB_PURGE_THRESH,
    purge_keep_pct=DB_PURGE_KEEP,
    mount_path=APP_DIR,
)
db_logger.start()

system = LynxTemperatureSystem(
    host=OI_HOST, port=OI_PORT,
    use_serial=(MODBUS_MODE == "rtu"),
    serial_port=SERIAL_PORT, baudrate=SERIAL_BAUD,
    bytesize=SERIAL_BYTESIZE, parity=SERIAL_PARITY, stopbits=SERIAL_STOPBITS,
    device_type=DEVICE_TYPE,
    c2_max_units=C2_MAX_UNITS,
    c2_password=C2_PASSWORD,
    c2_dump_secs=C2_DUMP_SECS,
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
        use_rtu = (MODBUS_MODE == "rtu")
        if (system.host != host or system.port != port
                or system.timeout != timeout
                or getattr(system, "use_serial", False) != use_rtu
                or getattr(system, "serial_port", "") != SERIAL_PORT):
            try: system.close()
            except Exception: pass
            system.__init__(
                host=host, port=port,
                use_serial=use_rtu,
                serial_port=SERIAL_PORT, baudrate=SERIAL_BAUD,
                bytesize=SERIAL_BYTESIZE, parity=SERIAL_PARITY, stopbits=SERIAL_STOPBITS,
                timeout=timeout, lines=_LINES
            )

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
    emit('connected', {'message': 'Live feed active'})


# ========= DASHBOARD HTML =========


@app.route("/")
def index():
    try:
        return render_template('dashboard.html',
            host=OI_HOST, port=OI_PORT,
            modbus_mode=MODBUS_MODE,
            serial_port=SERIAL_PORT, serial_baud=SERIAL_BAUD,
            interval=DB_LOG_INTERVAL)
    except Exception as e:
        import traceback
        logging.error("dashboard.html render failed:\n%s", _tb.format_exc())
        raise


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
            "scan_interval":  SCAN_INTERVAL,
            "db_interval":    DB_LOG_INTERVAL,
            "line_voltage":   LINE_VOLTAGE,
            "nominal_power_w": NOMINAL_POWER_W,
            "purge_threshold": DB_PURGE_THRESH,
            "purge_keep_pct":  DB_PURGE_KEEP,
            "api_key":         API_KEY,
            "modbus_mode":    MODBUS_MODE,
            "serial_port":    SERIAL_PORT,
            "serial_baud":    SERIAL_BAUD,
            "serial_bytesize": SERIAL_BYTESIZE,
            "serial_parity":  SERIAL_PARITY,
            "serial_stopbits": SERIAL_STOPBITS,
            "device_type":    DEVICE_TYPE,
            "c2_max_units":   C2_MAX_UNITS,
            "c2_password":    C2_PASSWORD,
            "c2_dump_secs":   C2_DUMP_SECS,
        })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    global OI_HOST, OI_PORT, MODBUS_TIMEOUT, SCAN_INTERVAL, DB_LOG_INTERVAL, LINE_VOLTAGE, NOMINAL_POWER_W, DB_PURGE_THRESH, DB_PURGE_KEEP, MODBUS_MODE, SERIAL_PORT, SERIAL_BAUD, SERIAL_BYTESIZE, SERIAL_PARITY, SERIAL_STOPBITS, DEVICE_TYPE, C2_MAX_UNITS, C2_PASSWORD, C2_DUMP_SECS, API_KEY
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
        purge_threshold = float(j["purge_threshold"])
        purge_keep_pct  = float(j["purge_keep_pct"])
        nominal_power_w = float(j.get("nominal_power_w", NOMINAL_POWER_W))
        if line_voltage <= 0:       raise ValueError("Line voltage must be > 0")
        api_key_new     = str(j.get("api_key", "")).strip()
        if not 50 <= purge_threshold <= 99: raise ValueError("Purge threshold must be 50–99%")
        if not 10 <= purge_keep_pct  <= 90: raise ValueError("Purge keep % must be 10–90%")

        modbus_mode     = str(j.get("modbus_mode", "tcp")).lower()
        serial_port_val = str(j.get("serial_port",    SERIAL_PORT))
        serial_baud_val = int(j.get("serial_baud",    SERIAL_BAUD))
        serial_bsz_val  = int(j.get("serial_bytesize",SERIAL_BYTESIZE))
        serial_par_val  = str(j.get("serial_parity",  SERIAL_PARITY)).upper()
        serial_stp_val  = int(j.get("serial_stopbits",SERIAL_STOPBITS))
        device_type     = str(j.get("device_type", DEVICE_TYPE)).lower().strip()
        c2_max_units    = int(j.get("c2_max_units", C2_MAX_UNITS))
        c2_password     = str(j.get("c2_password",  C2_PASSWORD)).strip()
        c2_dump_secs    = int(j.get("c2_dump_secs", C2_DUMP_SECS))
        if modbus_mode not in ("tcp","rtu"):
            raise ValueError("modbus_mode must be tcp or rtu")
        if serial_par_val not in ("N","E","O"):
            raise ValueError("parity must be N, E or O")
        if device_type not in ("lynx","centipede2"):
            raise ValueError("device_type must be lynx or centipede2")

        with settings_lock:
            OI_HOST        = host
            OI_PORT        = port
            MODBUS_TIMEOUT = timeout
            SCAN_INTERVAL  = interval
            DB_LOG_INTERVAL          = db_interval
            LINE_VOLTAGE             = line_voltage
            lynx_history._line_voltage = line_voltage
            DB_PURGE_THRESH              = purge_threshold
            DB_PURGE_KEEP                = purge_keep_pct
            API_KEY                      = api_key_new
            DEVICE_TYPE                  = device_type
            C2_MAX_UNITS                 = c2_max_units
            C2_PASSWORD                  = c2_password
            C2_DUMP_SECS                 = c2_dump_secs
            MODBUS_MODE                  = modbus_mode
            SERIAL_PORT                  = serial_port_val
            SERIAL_BAUD                  = serial_baud_val
            SERIAL_BYTESIZE              = serial_bsz_val
            SERIAL_PARITY                = serial_par_val
            SERIAL_STOPBITS              = serial_stp_val
            db_logger._interval          = db_interval
            db_logger._purge_threshold   = purge_threshold
            db_logger._purge_keep_pct    = purge_keep_pct

        cfg.read(_CFG_FILE)
        cfg.set("modbus",    "host",          host)
        cfg.set("modbus",    "port",          str(port))
        cfg.set("modbus",    "timeout",       str(timeout))
        cfg.set("modbus",    "scan_interval", str(interval))
        cfg.set("database",  "log_interval",    str(db_interval))
        cfg.set("database",  "purge_threshold", str(purge_threshold))
        cfg.set("database",  "purge_keep_pct",  str(purge_keep_pct))
        cfg.set("flask",     "api_key",         api_key_new)
        cfg.set("modbus",    "line_voltage",  str(line_voltage))
        cfg.set("modbus",    "mode",          modbus_mode)
        cfg.set("modbus",    "device_type",   device_type)
        cfg.set("modbus",    "c2_max_units",  str(c2_max_units))
        cfg.set("modbus",    "c2_password",   c2_password)
        cfg.set("modbus",    "c2_dump_secs",  str(c2_dump_secs))
        cfg.set("serial",    "port",          serial_port_val)
        cfg.set("serial",    "baudrate",      str(serial_baud_val))
        cfg.set("serial",    "bytesize",      str(serial_bsz_val))
        cfg.set("serial",    "parity",        serial_par_val)
        cfg.set("serial",    "stopbits",      str(serial_stp_val))
        update_scheduler_tz(cfg.get("dashboard", "timezone", fallback="UTC"))
        with open(_CFG_FILE, "w") as f:
            cfg.write(f)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/settings/test", methods=["POST"])
def api_settings_test():
    try:
        j        = request.get_json()
        use_rtu  = str(j.get("modbus_mode", "tcp")).lower() == "rtu"
        s_port   = str(j.get("serial_port",  SERIAL_PORT))
        s_baud   = int(j.get("serial_baud",  SERIAL_BAUD))
        dtype    = str(j.get("device_type",  DEVICE_TYPE)).lower().strip()

        via = (f"{s_port} @ {s_baud} baud" if use_rtu
               else f"{j.get('oi_host','')}:{j.get('oi_port', 502)}")

        # For RTU: the scanner already holds the serial port open exclusively.
        # Opening a second connection will fail with EAGAIN (resource busy).
        # Instead, briefly pause the scanner, close its client, run the test,
        # then reconnect and resume.
        if use_rtu:
            with settings_lock:
                try:
                    system.close()
                except Exception:
                    pass

        try:
            # Use a short timeout for the test — just enough to confirm comms
            test_timeout = min(float(j.get("oi_timeout", MODBUS_TIMEOUT)), 2.0)

            test_sys = LynxTemperatureSystem(
                host=str(j.get("oi_host", "")).strip(),
                port=int(j.get("oi_port", 502)),
                use_serial=use_rtu,
                serial_port=s_port,
                baudrate=s_baud,
                bytesize=int(j.get("serial_bytesize", SERIAL_BYTESIZE)),
                parity=str(j.get("serial_parity", SERIAL_PARITY)).upper(),
                stopbits=int(j.get("serial_stopbits", SERIAL_STOPBITS)),
                device_type=dtype,
                timeout=test_timeout,
                lines=(1,),
            )

            try:
                connected = test_sys.connect()
            except Exception as ce:
                return jsonify({"success": False,
                                "error": f"Cannot open {via}: {ce}"}), 200

            if not connected:
                return jsonify({"success": False,
                                "error": f"Cannot open {via} — port not found or in use"}), 200

            # For Centipede 2: just probe unit IDs 1–4 to confirm any response.
            # Full zone scan takes too long (32 zones × timeout per line).
            if dtype == "centipede2" and use_rtu:
                from lynx_reader import _slave_kwarg
                responded = 0
                for uid in range(1, 5):
                    try:
                        r = test_sys.client.read_input_registers(
                            address=0, count=1, **_slave_kwarg(uid)
                        )
                        if r and not r.isError():
                            responded += 1
                    except Exception:
                        pass
                return jsonify({"success": True,
                                "zone_count": responded,
                                "note": f"{responded} unit(s) responded in quick probe (units 1–4)"}), 200

            try:
                data = test_sys.read_all_zones()
            except Exception as read_err:
                return jsonify({"success": False,
                                "error": f"Port opened but no Modbus response ({via}): {read_err}"}), 200

            return jsonify({"success": True, "zone_count": len(data)})

        finally:
            # Always close test client and let the scanner reconnect on next cycle
            try: test_sys.close()
            except Exception: pass

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

    # Start scanner immediately — do not wait for a browser to connect
    threading.Thread(target=background_scanner, daemon=True).start()

    # Start setpoint scheduler
    def _get_zones():
        with data_lock:
            return list(latest_data)
    start_scheduler(_get_zones, _perform_setpoint_write,
                    tz_name=cfg.get("dashboard", "timezone", fallback="UTC"))

    socketio.run(app, host="0.0.0.0", port=FLASK_PORT, allow_unsafe_werkzeug=True)

if __name__ == "__main__":
    run()
