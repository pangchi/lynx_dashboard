# BriskHeat LYNX – Real-Time Dashboard

A Python-based web dashboard for the BriskHeat LYNX® Temperature Control System.
Monitors all zones in real-time over Modbus TCP, logs data to PostgreSQL, and provides
history viewing, energy analytics, setpoint scheduling, and full browser-based configuration.

---

## Table of Contents

1. [File Structure](#file-structure)
2. [Quick Start](#quick-start)
3. [Configuration Reference](#configuration-reference)
4. [Web Pages](#web-pages)
5. [REST API](#rest-api)
6. [CLI Tools](#cli-tools)
7. [Database](#database)
8. [Zone Status Values](#zone-status-values)
9. [Troubleshooting](#troubleshooting)
10. [Change History](#change-history)

---

## File Structure

```
lynx/
├── app.py                  Entry point — run this
├── lynx_dashboard.py       Flask app, WebSocket scanner, settings & API routes
├── lynx_history.py         Flask blueprint — /history, /energy, and API routes
├── lynx_db_logger.py       Background PostgreSQL logger with auto-purge
├── lynx_schedule.py        Flask blueprint — /schedule page and scheduler thread
├── lynx_reader.py          Modbus TCP/RTU + Centipede 2 LUI text protocol reader
├── c2_scan.py              Offline Centipede 2 register scanner (troubleshooting tool)
├── lynx_set_all.py         CLI tool — set all zones to the same setpoint
├── test_connection.py      Connectivity test for Modbus and PostgreSQL
├── config.ini.example      Configuration template — copy to config.ini and edit
├── config.ini              Your local configuration (not in version control)
├── schedule.json           Saved schedules (auto-created on first schedule save)
├── install.sh              Automated Raspberry Pi installer
├── uninstall.sh            Clean removal script
├── run.sh                  Manual launcher (activates venv, then runs app.py)
└── templates/
    ├── dashboard.html      Live dashboard page
    ├── history.html        History viewer page
    ├── energy.html         Hourly / daily energy consumption page
    ├── schedule.html       Setpoint schedule management page
    └── settings.html       Settings page
```

---

## Quick Start

### Option A — Automated install (Raspberry Pi)

Copy all project files to the Pi, then:

```bash
chmod +x install.sh
./install.sh
```

The script will:
- Install system dependencies (`python3`, `postgresql`, `libpq-dev`)
- Create a Python virtual environment with all required packages
- Create the PostgreSQL database and user with a random password
- Write `config.ini` with detected timezone and entered OI Gateway IP
- Install and enable a `systemd` service (`lynx-dashboard`) that starts on boot
- Open port 5000 in `ufw` if the firewall is active
- Run `test_connection.py` and optionally start the service

After install the dashboard is available at `http://<pi-ip>:5000`.

**Service commands:**
```bash
sudo systemctl status  lynx-dashboard
sudo systemctl restart lynx-dashboard
sudo journalctl -u     lynx-dashboard -f
```

**Manual start (with venv):**
```bash
./run.sh
```

**To uninstall:**
```bash
chmod +x uninstall.sh
./uninstall.sh
```

---

### Option B — Manual setup

#### 1. Copy and edit `config.ini`

```bash
cp config.ini.example config.ini
```

```ini
[modbus]
host = 192.168.200.20      ; ← your OI Gateway IP

[database]
password = secret          ; ← your DB password

[dashboard]
timezone = Asia/Singapore  ; ← your IANA timezone (for schedule firing)
```

#### 2. Test connectivity

```bash
python test_connection.py
```

Auto-creates the PostgreSQL database, user, table, and indexes if they don't exist.

#### 3. Run

```bash
python app.py
```

Missing packages (`flask`, `flask-socketio`, `pymodbus`, `eventlet`, `psycopg2-binary`)
are auto-installed on first run.

#### 4. Manual systemd service

Create `/etc/systemd/system/lynx-dashboard.service`:

```ini
[Unit]
Description=BriskHeat LYNX Dashboard
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/lynx
ExecStart=/home/pi/lynx/venv/bin/python /home/pi/lynx/app.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1
Environment=VIRTUAL_ENV=/home/pi/lynx/venv
Environment=PATH=/home/pi/lynx/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/home/pi
Environment=LYNX_APP_DIR=/home/pi/lynx

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable lynx-dashboard
sudo systemctl start  lynx-dashboard
```

---

## Configuration Reference

All settings live in `config.ini` (copied from `config.ini.example` on first setup). Changes via the Settings page are written back automatically.

```ini
[modbus]
host          = 192.168.200.20  ; OI Gateway IP address
port          = 502             ; Modbus TCP port
timeout       = 4.0             ; Connection timeout (seconds)
line_voltage  = 240             ; Line voltage (V) — for power calculation (W = V × A)
scan_interval = 8               ; Seconds between Modbus polls (minimum 4)
lines         = 1,2,3,4         ; Lines to scan (comma-separated)

[database]
host            = localhost     ; PostgreSQL host
port            = 5432          ; PostgreSQL port
name            = lynx          ; Database name
user            = lynx_user     ; Application DB user
password        = secret        ; Application DB password
log_interval    = 60            ; Seconds between DB writes (minimum 10)
purge_threshold = 80            ; Trigger auto-purge when disk >= this % full (50–99)
purge_keep_pct  = 60            ; After purge, keep newest this % of rows (10–90)
admin_user      = postgres      ; Superuser for first-run DB creation
admin_password  =               ; Leave blank for peer/trust auth (Linux default)

[flask]
secret_key = BriskHeat2025
port       = 5000

[dashboard]
ip       = 127.0.0.1            ; Used by lynx_set_all.py
timeout  = 8                    ; HTTP timeout for lynx_set_all.py
timezone = UTC                  ; IANA timezone for schedule firing
```

---

## Web Pages

| URL | Description |
|---|---|
| `http://HOST:5000/` | Live dashboard — real-time zone table, WebSocket |
| `http://HOST:5000/history` | History viewer — trend charts, zone selector, CSV |
| `http://HOST:5000/energy` | Energy — hourly/daily kWh charts, CSV |
| `http://HOST:5000/schedule` | Schedule — setpoint schedule CRUD |
| `http://HOST:5000/settings` | Settings — configure all parameters live |

### Live Dashboard
- Real-time zone table (Line, Zone, SP, PV, Output%, Current, Status)
- WebSocket updates every `scan_interval` seconds
- Per-zone inline setpoint control
- Links to all pages in the header

### History Viewer
- Date/time range picker (defaults to last 24 h)
- Line / Zone filter dropdowns
- Zone selector chips — toggle individual zones on/off across all charts
- Trend charts in order: PV Temperature → Power (W) → Current (A) → Output % → Setpoint
- **Power (W) chart** has a **Σ Sum** toggle — collapses all active zones into one total line
- **Current (A) chart** has a **Σ Sum** toggle — same, shows total system current
- Total Energy (kWh) stat card — trapezoidal integration over the query window
- Data table — last 500 rows inline
- CSV download — pivoted by timestamp, one column group per zone
- All chart timestamps in browser local timezone (Luxon adapter)

### Energy Page
- **Hourly / Daily toggle** — switch between hour and day buckets
- Hourly defaults to last 24 h; Daily defaults to last 30 days
- Stacked bar chart — one bar per bucket, stacked by zone
- Total kWh label on top of each bar
- Interactive zone selection — click legend, bar segment, or chip
- Stat cards: total buckets, total kWh, peak bucket, avg/bucket, line voltage
- Data table with per-zone kWh and row/column totals
- CSV download — pivoted by bucket, Total kWh column
- Hour/day buckets in browser local timezone

### Schedule Page
- Add, edit, delete setpoint schedules
- Per-schedule: name, setpoint (°C), time (HH:MM), weekdays, target zones
- Day quick-select: Weekdays / Weekend / Every day buttons
- Zone multi-select showing live PV readings
- Leave zones unchecked to apply to all active zones at fire time
- Enable/disable toggle per schedule
- Schedules stored in `schedule.json`
- Scheduler checks every 30 s, fires within one check cycle of the configured minute

### Settings Page
- **Connection Mode** — TCP (IP gateway) or RTU (serial)
- TCP: host, port, timeout; RTU: serial port, baud rate, parity
- **Device Type** — LYNX OI Gateway or Centipede 2 (RS-232)
- Centipede 2 fields: OI password, max units to scan, dump duration (s)
- Scan interval and DB log interval
- Line voltage (V) and Nominal Heater Power (W) for Centipede 2 energy calculation
- Purge threshold (%) and purge keep (%)
- API Key for external REST access
- **Test Connection** — verify connection without saving
- **Save & Apply** — validates, applies live, persists to `config.ini`

---

## REST API

### `GET /api/status`
Latest zone snapshot.
```json
{
  "zones": [{"line":1,"zone":1,"pv":85.2,"setpoint":90.0,
             "output_percent":42,"current":1.23,"status":"HEATING"}],
  "updated": 1736123456.789
}
```

### `POST /api/setpoint`
Single: `{"line":1,"zone":2,"sp":95.0}`
Batch: `{"updates":[{"line":1,"zone":1,"sp":90.0},...]}`

### `GET /api/history`
| Param | Default | Description |
|---|---|---|
| `start` | 24 h ago | `YYYY-MM-DD HH:MM` or ISO-8601 UTC |
| `end` | now | same |
| `line` | all | filter by line |
| `zone` | all | filter by zone |
| `limit` | 10 000 | max rows (cap 50 000) |

Response includes computed `power_w` = `current_a × line_voltage`.

### `GET /api/history/csv`
Same params. Pivoted CSV — one row per scan, one column group per zone:
```
Time, L1-Z1 PV, L1-Z1 SP, L1-Z1 W, L1-Z1 Out%, L1-Z1 A, L1-Z1 Status, ...
```

### `GET /api/energy/hourly`
Hourly kWh per zone (SQL trapezoidal integration).
Params: `start`, `end`, `line`, `zone`, `tz` (IANA timezone).
```json
{"voltage":240,"rows":[{"hour":"2025-01-15T08:00:00+00:00","line":1,"zone":1,"kwh":0.342}]}
```

### `GET /api/energy/hourly/csv`
Same params. Pivoted by hour, Total kWh column.

### `GET /api/energy/daily`
Daily kWh per zone. Same params as hourly.
```json
{"voltage":240,"rows":[{"bucket":"2025-01-15T00:00:00+00:00","line":1,"zone":1,"kwh":7.824}]}
```

### `GET /api/energy/daily/csv`
Same params. Pivoted by day, Total kWh column.

### `GET /api/settings`
```json
{
  "oi_host":"192.168.200.20","oi_port":502,"oi_timeout":4.0,
  "scan_interval":8,"db_interval":60,"line_voltage":240,
  "purge_threshold":80,"purge_keep_pct":60
}
```

### `POST /api/settings`
Updates all settings live and persists to `config.ini`. Same fields as GET.

### `POST /api/settings/test`
Test Modbus connection without saving.
```json
{"oi_host":"192.168.200.20","oi_port":502,"oi_timeout":4.0}
```

### `GET /api/schedule`
Returns all schedule entries as a JSON array.

### `POST /api/schedule`
Create (omit `id`) or update (include `id`) a schedule entry.
```json
{
  "name": "Night setback",
  "setpoint": 60.0,
  "time": "22:00",
  "days": [0,1,2,3,4],
  "zones": [{"line":1,"zone":1}],
  "enabled": true
}
```
`days`: 0=Mon … 6=Sun. `zones`: empty = all active zones at fire time.

### `DELETE /api/schedule/<id>`
Delete a schedule entry.

### `POST /api/schedule/<id>/toggle`
Enable or disable a schedule entry.

---

## CLI Tools

### `lynx_set_all.py` — Set all zones to one temperature

```bash
python lynx_set_all.py 75
python lynx_set_all.py --temp 70
python lynx_set_all.py 80 --config /path/to/config.ini
```

### `test_connection.py` — Verify connectivity

```bash
python test_connection.py
```

Runs in order:
1. Modbus OI Gateway — connect and report zone count
2. PostgreSQL admin — create DB, user, schema grants if missing; sync password
3. PostgreSQL app user — verify credentials, report table existence

---

## Database

### Automatic Setup

Everything is created automatically on first run. Bootstrap (every startup, all steps idempotent):

1. Admin connects to `postgres` maintenance DB
2. `CREATE USER lynx_user` if missing; sync password from `config.ini`
3. `CREATE DATABASE lynx OWNER lynx_user` if missing
4. `GRANT ALL PRIVILEGES ON DATABASE lynx`
5. Admin connects to `lynx` DB → `GRANT ALL ON SCHEMA public` (required on PG 15+)
6. App user connects → CREATE TABLE + indexes + migrations (autocommit)

### Schema

```sql
CREATE TABLE lynx_zone_log (
    id          BIGSERIAL    PRIMARY KEY,
    ts          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    line        SMALLINT     NOT NULL,
    zone        SMALLINT     NOT NULL,
    pv          REAL,          -- Process Value (°C)
    setpoint    REAL,          -- Setpoint (°C)
    output_pct  REAL,          -- Heater output (%)
    current_a   REAL,          -- Measured current (A)
    status      TEXT           -- OK / HEATING / OVER TEMP / NO TC
);
CREATE INDEX idx_lynx_ts        ON lynx_zone_log (ts DESC);
CREATE INDEX idx_lynx_line_zone ON lynx_zone_log (line, zone, ts DESC);
```

> **`power_w` is not stored** — computed on read as `current_a × line_voltage`.
> Changing voltage in Settings recalculates all historical queries instantly.

### Timestamps

Stored as `TIMESTAMPTZ` (UTC). API returns ISO-8601 with UTC offset.
Browser converts to local time for chart labels and table display.
Energy buckets truncated in browser's IANA timezone (`?tz=` param).

### Power & Energy Calculation

```
power_w  = current_a × line_voltage                          (W)
energy   = Σ (avg_power × Δt_seconds) / 3_600_000           (kWh)
```

SQL uses `LAG()` window function. Segments with Δt > 600 s excluded to avoid
inflating kWh across restarts or logging gaps. LAG applied inside a CTE filtered
to the query window to prevent cross-boundary contamination.

### Auto-Purge

- Runs after every DB write cycle
- Monitors the partition containing `app.py` (found via `os.path.ismount()` walk)
- Startup log: `monitoring partition: /home, 34.2% used of 32.0GB`
- Purges oldest rows when usage ≥ `purge_threshold` (default 80%)
- Keeps newest `purge_keep_pct`% (default 60%)
- Both values configurable live in Settings page

### Migrations

Run at startup with `autocommit=True`:
- `DROP COLUMN IF EXISTS power_w` — removes stored power column (now computed on read)

### Manual Setup (if preferred)

```sql
CREATE DATABASE lynx;
CREATE USER lynx_user WITH PASSWORD 'secret';
GRANT ALL PRIVILEGES ON DATABASE lynx TO lynx_user;
\c lynx
GRANT ALL ON SCHEMA public TO lynx_user;
```

---

## Zone Status Values

| Status | Colour | Meaning |
|---|---|---|
| `OK` | Green | Zone is at setpoint |
| `HEATING` | Yellow | Actively heating toward setpoint |
| `OVER TEMP` | Red | Temperature exceeded setpoint |
| `NO TC` | Grey | Thermocouple fault or no sensor |

---

## Troubleshooting

Always check the journal first:
```bash
sudo journalctl -u lynx-dashboard -n 50 --no-pager
```

| Error | Fix |
|---|---|
| `RuntimeError: The Werkzeug web server is not designed to run in production` | `allow_unsafe_werkzeug=True` in `socketio.run()` — update `lynx_dashboard.py` |
| `FileNotFoundError: config.ini not found` | Update `lynx_dashboard.py` — uses `abspath(__file__)` for config path |
| `ModuleNotFoundError: No module named 'flask'` | Venv not used — check `ExecStart` points to `venv/bin/python` |
| `password authentication failed for user "lynx_user"` | Run `test_connection.py` — syncs password from `config.ini` to PostgreSQL |
| `permission denied for schema public` | PostgreSQL 15+ issue — run `test_connection.py` to re-run schema grant |
| `cannot run inside a transaction block` | DDL now runs with `autocommit=True` — update `lynx_db_logger.py` |
| `fe_sendauth: no password supplied` | Blank password — `password` kwarg omitted; uses peer/trust auth |

### `run.sh` works but service fails

1. Check `LYNX_APP_DIR` is in the service: `sudo systemctl cat lynx-dashboard`
2. `ExecStart` must point to `venv/bin/python`
3. Redeploy: copy all `.py` files, then `sudo systemctl restart lynx-dashboard`

### DB not logging data

- Check `sudo journalctl -u lynx-dashboard -f` for `DB write failed`
- Run `python test_connection.py`
- Scanner starts at `app.py` launch — no browser visit needed
- Writer waits up to one interval for a snapshot before skipping

---

## Change History

### v1.0 — Initial Release
- Live Modbus dashboard with real-time WebSocket zone table
- Per-zone setpoint control — single and batch via `/api/setpoint`
- `lynx_reader.py` Modbus TCP communication

### v2.0 — PostgreSQL Logging + History Viewer
- `lynx_db_logger.py` — background thread, logs zone readings every 60 s
- `lynx_history.py` Flask blueprint — `/history` page
- `/api/history` JSON and `/api/history/csv` download
- History page: date/time picker, PV/SP/Output% trend charts, data table
- Timestamps stored as `TIMESTAMPTZ` (UTC)

### v2.1 — Config File
- All settings extracted to `config.ini`
- `lynx_set_all.py` migrated from `config.json` → `config.ini`
- `app.py` reduced to a thin launcher

### v2.2 — Database Auto-Creation
- `ensure_db_exists()` creates DB, user, and grants on first run
- PostgreSQL 15+ schema grant fix
- Blank `admin_password` → peer/trust auth support

### v2.3 — Schema Creation Fix
- Fixed `psycopg2` single-statement limitation — DDL split into individual `execute()` calls

### v2.4 — Settings Page
- `/settings` page with live GET/POST `/api/settings`
- Configurable without restart: host, port, timeout, scan interval, DB log interval
- `POST /api/settings/test` — Modbus test without saving

### v2.5 — Auto-Purge
- Disk usage check after every write cycle
- Purges oldest rows when disk ≥ `purge_threshold`%
- Keeps newest `purge_keep_pct`%

### v2.6 — History Improvements + Templates
- CSV pivoted by timestamp: one row per scan, one column group per zone
- Zone selector chips
- HTML moved to `templates/` directory

### v2.7 — Current (A) Trend Chart
- Current (A) trend added to history page before Output%

### v2.8 — Power & Energy
- `line_voltage` in config and Settings page
- `power_w` computed on read (`current_a × line_voltage`) — not stored
- Power (W) trend chart and Total Energy (kWh) stat card on history page

### v2.9 — Local Timezone in Charts
- Switched to `chartjs-adapter-luxon`
- Chart x-axis and tooltips in browser local time

### v3.0 — Hourly Energy Page
- `/energy` page with stacked bar chart, zone chips, stat cards, data table
- `/api/energy/hourly` and `/api/energy/hourly/csv`
- SQL trapezoidal integration using `LAG()` window function
- `chartjs-plugin-datalabels` for total kWh labels on bars

### v3.1 — kWh Bug Fixes
- **Fixed: unit error** — divisor 3,600 → 3,600,000 (was Wh not kWh)
- **Fixed: LAG() crossing query boundary** — filter into CTE first
- **Fixed: gap inflation** — segments with Δt > 600 s excluded

### v3.2 — Energy Page: Hourly / Daily Toggle
- Hourly/Daily toggle; defaults 24 h / 30 days respectively
- `/api/energy/daily` and `/api/energy/daily/csv`

### v3.3 — Energy Timezone Fix
- Browser sends `?tz=<IANA>` — SQL `date_trunc()` uses browser timezone
- Correct local-hour bucket boundaries (previously UTC only)

### v3.4 — Setpoint Schedule
- `lynx_schedule.py` Flask blueprint + background scheduler thread
- `/schedule` page: add/edit/delete with modal UI
- Per-schedule: name, setpoint, time, weekdays, target zones
- Enable/disable toggle; stored in `schedule.json`
- REST: `GET/POST /api/schedule`, `DELETE`, `POST .../toggle`

### v3.5 — Auto-Purge Improvements
- `_get_mount_point()` finds true partition via `os.path.ismount()`
- Startup log confirms monitored partition and usage
- Purge threshold and keep % configurable in Settings page

### v3.6 — DB Write Fix + Logging
- **Fixed: scanner not starting until browser connects**
- **Fixed: DB writes skipping on empty queue** — writer waits for snapshot
- **Fixed: DDL in transaction block** — autocommit for DDL connections
- `logging.basicConfig(INFO)` in `app.py`

### v3.7 — Password Auth Fix
- Blank password omits `password` kwarg → peer/trust auth
- `test_connection.py` shows masked password

### v3.8 — Raspberry Pi Installer
- `install.sh` — full automated installer
- `uninstall.sh` — clean removal
- `run.sh` — manual venv launcher
- `--break-system-packages` for Bookworm (Python 3.11+)

### v3.9 — Real lynx_reader.py
- Correct register map: `REGISTERS_PER_ZONE=24`, `ZONES_PER_LINE=32`
- Separate holding (SP) and input (PV/Output/Current) registers
- Scaling: temperature ×100, current ×1000
- `device_id=` kwarg; `0x8000`/`0xFFFF` sentinels handled

### v4.0 — Systemd Startup Fixes
- **Fixed: Werkzeug production error** — `allow_unsafe_werkzeug=True`
- **Fixed: config.ini not found** — `abspath(__file__)` for all path resolution
- `app.py` sets `sys.path`, `os.chdir`, `LYNX_APP_DIR` env var
- `VIRTUAL_ENV`, `HOME`, `LYNX_APP_DIR` in systemd service environment

### v4.1 — Power & Current Sum Toggle
- **Power (W) Trend** chart: **Σ Sum** toggle button collapses all active zones
  into one total line showing combined system power at each timestamp
- **Current (A) Trend** chart: same toggle for total system current
- Toggle resets automatically on each new query
- Chart title updates to indicate sum mode is active

### v4.2 — Serial RTU & Settings Improvements
- Settings page: **TCP / RTU (Serial)** mode toggle
- RTU fields: Serial Port, Baud Rate (dropdown), Parity
- Device Type dropdown: **BriskHeat LYNX (OI Gateway)** / **BriskHeat Centipede 2 (RS-232)**
- Selecting Centipede 2 locks baud to 19200, hides TCP fields, shows C2-specific fields
- Card heading updates dynamically per device type
- Serial mode shown in dashboard footer: `/dev/ttyUSB0 @ 19200 baud`
- Test Connection error message shows serial port instead of IP in RTU mode
- `modbus_mode`, `device_type`, `serial_port`, `serial_baud`, `serial_parity` all saved to `config.ini`
- **Fixed: `device_type` never persisted** — `cfg.set("modbus", "device_type", ...)` was missing
- **Fixed: `modbus_mode` read from button CSS class** — replaced with hidden input as single source of truth
- `install.sh` adds service user to `dialout` group for serial port access
- Systemd service file sets `Group=dialout` and `SupplementaryGroups=dialout`

### v4.3 — Centipede 2 Support (C2MOD-OI-7)
- Full support for BriskHeat Centipede 2 temperature modules via C2MOD-OI-7 OI gateway
- Communication via **LUI text protocol** over RS-232 (not Modbus RTU — OI does not expose
  zone data via Modbus registers; zone data only accessible via text protocol)
- **Login sequence** (confirmed by live device):
  - Send CR only (not CRLF) — OEM shell intercepts the port first
  - Two CR pokes to wake the shell
  - Password sent character-by-character at 50ms intervals + CR
  - `Dump:N` command streams all active zones for N seconds
  - Duplicate readings deduplicated — only the last reading per zone is kept
  - `Bye` sent to exit cleanly
- Zones streamed as: `Z#### ST#### S####.#C H####.#C L####.#C PV####.#C DC###% HTOK`
- Zone numbers mapped to dashboard lines: zones 1–16 = Line 1, 17–32 = Line 2, etc.
- 73 active zones (Z0001–Z0073) reading correctly at 110°C setpoint
- New Settings fields for Centipede 2: **Max Units to Scan**, **OI Password**, **Dump Duration (s)**
- `c2_max_units`, `c2_password`, `c2_dump_secs` in `config.ini` and Settings page
- `c2_scan.py` — offline register scanner tool for troubleshooting
- `lynx_reader.py` CLI: `--password`, `--dump-secs`, `--debug` flags added
- **Fixed: duplicate `_read_all_centipede2` and `_read_lynx_line`** — Python uses last definition;
  stale duplicates silently overrode the correct implementations
- **Fixed: `property 'connected' has no setter`** — pymodbus 3.13 made `connected` read-only;
  replaced `self.client.connected = False` with `self.client.close()`
- **Fixed: pymodbus kwarg detection** — `_slave_kwarg()` now inspects method signature at runtime
  to select `device_id=`, `slave=`, or `unit=` per pymodbus version

### v4.4 — Energy Page Centipede 2 Support
- Energy and history `power_w` calculation now supports three modes:
  1. **LYNX** — `current_a × line_voltage` (exact watts, unchanged)
  2. **Centipede 2 with nominal wattage** — `output_pct / 100 × nominal_power_w`
  3. **Centipede 2, no wattage set** — `output_pct` used as relative proxy (0–100 units)
- **Nominal Heater Power (W)** field added to Settings page and `config.ini`
- `nominal_power_w` passed to history blueprint; all SQL queries use it via `%(nominal)s`
- **Fixed: inline `;` comments in `config.ini`** — `configparser.getint()` failed on values
  with trailing comments; all inline comments removed from `config.ini.example`
