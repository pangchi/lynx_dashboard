# BriskHeat LYNX – Real-Time Dashboard

A Python-based web dashboard for the BriskHeat LYNX® Temperature Control System.  
Monitors all zones in real-time over Modbus TCP, logs data to PostgreSQL, and provides
a history viewer, energy analytics, and full configuration via the browser.

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
9. [Change History](#change-history)

---

## File Structure

```
lynx/
├── app.py                  Entry point — run this
├── lynx_dashboard.py       Flask app, WebSocket scanner, settings & API routes
├── lynx_history.py         Flask blueprint — /history, /energy, and API routes
├── lynx_db_logger.py       Background PostgreSQL logger with auto-purge
├── lynx_reader.py          Modbus communication class (original project)
├── lynx_set_all.py         CLI tool — set all zones to the same setpoint
├── test_connection.py      Connectivity test for Modbus and PostgreSQL
├── config.ini              All configuration (the only file you normally edit)
└── templates/
    ├── dashboard.html      Live dashboard page
    ├── history.html        History viewer page
    ├── energy.html         Hourly energy consumption page
    └── settings.html       Settings page
```

---

## Quick Start

### 1. Edit `config.ini`

Set at minimum the OI Gateway IP and database password:

```ini
[modbus]
host = 192.168.200.20   ; ← your OI Gateway IP

[database]
password = secret       ; ← your DB password
```

### 2. Test connectivity (recommended on first run)

```bash
python test_connection.py
```

Verifies Modbus and PostgreSQL connections. Auto-creates the database, user, table,
and indexes if they don't exist yet.

### 3. Run

```bash
python app.py
```

Missing Python packages are installed automatically on first run:
`flask`, `flask-socketio`, `pymodbus`, `eventlet`, `psycopg2-binary`

---

## Configuration Reference

`config.ini` is the single source of truth. Changes via the Settings page are written
back to this file automatically.

```ini
[modbus]
host          = 192.168.200.20  ; OI Gateway IP address
port          = 502             ; Modbus TCP port
timeout       = 4.0             ; Connection timeout in seconds
line_voltage  = 240             ; Line voltage (V) — used to calculate Power (W = V × A)
scan_interval = 8               ; Seconds between Modbus polls (minimum 4)
lines         = 1,2,3,4         ; Lines to scan (comma-separated)

[database]
host            = localhost     ; PostgreSQL host
port            = 5432          ; PostgreSQL port
name            = lynx          ; Database name
user            = lynx_user     ; Application DB user
password        = secret        ; Application DB password
log_interval    = 60            ; Seconds between DB writes (minimum 10)
purge_threshold = 80            ; Trigger auto-purge when disk >= this % full
purge_keep_pct  = 60            ; Keep newest this % of rows after purge
admin_user      = postgres      ; Superuser for first-run DB/user creation
admin_password  =               ; Leave blank for peer/trust auth (common on Linux)

[flask]
secret_key = BriskHeat2025      ; Flask session secret
port       = 5000               ; Web server port

[dashboard]
ip      = 127.0.0.1             ; Dashboard IP used by lynx_set_all.py
timeout = 8                     ; HTTP timeout for lynx_set_all.py
```

---

## Web Pages

| URL | Description |
|---|---|
| `http://HOST:5000/` | Live dashboard — real-time zone table, WebSocket auto-refresh |
| `http://HOST:5000/history` | History viewer — trend charts, zone selector, CSV download |
| `http://HOST:5000/energy` | Hourly energy — stacked bar chart, kWh table, CSV download |
| `http://HOST:5000/settings` | Settings — configure all parameters without restarting |

### Live Dashboard
- Real-time zone table updated every `scan_interval` seconds via WebSocket
- Columns: Line, Zone, SP (°C), PV (°C), Output (%), Current (A), Status
- Inline setpoint control per zone
- Links to History, Energy, and Settings pages

### History Viewer
- **Date/time pickers** — start and end range (defaults to last 24 h)
- **Line / Zone filters** — narrow query to specific zones
- **Zone selector chips** — toggle individual zones on/off across all charts
- **Trend charts** (in order): PV Temperature → Power (W) → Current (A) → Output % → Setpoint
- **Total Energy card** — accumulated kWh for the queried window (trapezoidal integration)
- **Summary stats** — row count, average / max / min PV
- **Data table** — last 500 rows shown inline
- **CSV download** — full dataset pivoted by timestamp, one column group per zone
- **Local timezone** — all chart timestamps in the browser's local timezone (Luxon adapter)

### Hourly Energy Page
- **Stacked bar chart** — one bar per hour, stacked by zone
- **Total kWh label** on top of each bar
- **Interactive selection** — click legend item, bar segment, or zone chip to toggle a zone
- **Zone selector chips** — same pattern as history page
- **Summary stat cards** — total hours, total kWh, peak hour, avg kWh/h, line voltage
- **Data table** — one row per hour, per-zone columns, row totals, footer grand totals
- **CSV download** — pivoted by hour, one column per zone, Total kWh column
- Hours displayed in browser local timezone

### Settings Page
- OI Gateway host, port, timeout
- Scan interval and DB log interval
- Line voltage (used for power calculation)
- **Test Connection** — verifies Modbus without saving
- **Save & Apply** — validates, applies live, persists to `config.ini`

---

## REST API

### `GET /api/status`
Returns the latest zone snapshot from the live scanner.
```json
{
  "zones": [
    {"line": 1, "zone": 1, "pv": 85.2, "setpoint": 90.0,
     "output_percent": 42, "current": 1.23, "status": "HEATING"}
  ],
  "updated": 1736123456.789
}
```

### `POST /api/setpoint`
Set one zone or batch-update many.

Single:
```json
{ "line": 1, "zone": 2, "sp": 95.0 }
```
Batch:
```json
{ "updates": [{"line": 1, "zone": 1, "sp": 90.0}, {"line": 1, "zone": 2, "sp": 95.0}] }
```

### `GET /api/history`
Query historical data. All parameters optional.

| Parameter | Default | Description |
|---|---|---|
| `start` | 24 h ago | `YYYY-MM-DD HH:MM` or ISO-8601 UTC |
| `end` | now | Same format |
| `line` | all | Filter by line number |
| `zone` | all | Filter by zone number |
| `limit` | 10 000 | Max rows (hard cap 50 000) |

Response includes computed `power_w` field (`current_a × line_voltage`).

### `GET /api/history/csv`
Same parameters. CSV pivoted by timestamp — one row per scan, one column group per zone:
```
Time, L1-Z1 PV, L1-Z1 SP, L1-Z1 W, L1-Z1 Out%, L1-Z1 A, L1-Z1 Status, L1-Z2 PV, ...
```

### `GET /api/energy/hourly`
Hourly kWh per zone, aggregated using SQL trapezoidal integration.
Same query parameters as `/api/history` (start, end, line, zone).

```json
{
  "voltage": 240,
  "rows": [
    {"hour": "2025-01-15T08:00:00+00:00", "line": 1, "zone": 1, "kwh": 0.342},
    ...
  ]
}
```

### `GET /api/energy/hourly/csv`
Same parameters. CSV pivoted by hour — one column per zone, plus a Total kWh column:
```
Hour, L1-Z1 kWh, L1-Z2 kWh, ..., Total kWh
2025-01-15T08:00:00+00:00, 0.3420, 0.2910, ..., 0.6330
```

### `GET /api/settings`
Returns current live settings:
```json
{
  "oi_host": "192.168.200.20", "oi_port": 502, "oi_timeout": 4.0,
  "scan_interval": 8, "db_interval": 60, "line_voltage": 240
}
```

### `POST /api/settings`
Update and persist all settings without restarting:
```json
{
  "oi_host": "192.168.200.20", "oi_port": 502, "oi_timeout": 4.0,
  "scan_interval": 8, "db_interval": 60, "line_voltage": 240
}
```

### `POST /api/settings/test`
Test a Modbus connection without saving. Returns zone count on success:
```json
{ "oi_host": "192.168.200.20", "oi_port": 502, "oi_timeout": 4.0 }
```

---

## CLI Tools

### `lynx_set_all.py` — Set all zones to one temperature

```bash
python lynx_set_all.py 75
python lynx_set_all.py --temp 70
python lynx_set_all.py 80 --config /path/to/config.ini
```

Reads `[dashboard]` from `config.ini`, calls `/api/status` to discover active zones,
then batch-posts all setpoints via `/api/setpoint`.

### `test_connection.py` — Verify connectivity

```bash
python test_connection.py
```

Runs in order:
1. Connects to Modbus OI Gateway — reports success and zone count
2. Connects as PostgreSQL admin — creates database and user if missing
3. Grants `ALL ON SCHEMA public` (required on PostgreSQL 15+)
4. Connects as app user — reports PostgreSQL version and table existence

---

## Database

### Automatic Setup

Everything is created automatically on first run. No manual SQL required.

Bootstrap sequence (runs at every startup, all steps idempotent):
1. Connect to `postgres` maintenance DB as admin
2. `CREATE USER lynx_user` if not exists
3. `CREATE DATABASE lynx OWNER lynx_user` if not exists
4. `GRANT ALL PRIVILEGES ON DATABASE lynx TO lynx_user`
5. Connect to `lynx` DB as admin → `GRANT ALL ON SCHEMA public TO lynx_user`
6. Connect as `lynx_user` → create table + indexes + run migrations

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

> **Note:** `power_w` is **not stored** — it is computed on read as `current_a × line_voltage`.
> Changing `line_voltage` in Settings instantly affects all historical queries.

### Timestamps

- Stored as `TIMESTAMPTZ` (UTC with timezone offset)
- API returns ISO-8601 strings: `2025-01-15T08:00:00+00:00`
- Browser converts to local time for chart labels and table display

### Power Calculation

```
power_w  = current_a × line_voltage        (W, per zone, per reading)
energy   = Σ (avg_power × Δt_seconds / 3600)  (kWh, trapezoidal integration)
```

The hourly energy API performs trapezoidal integration entirely in SQL using a `LAG()`
window function:
```sql
(power_w + prev_power_w) / 2.0 * EXTRACT(EPOCH FROM (ts - prev_ts)) / 3600.0
```

### Auto-Purge

When disk usage on the application's mount point reaches `purge_threshold` (default 80%),
the logger deletes the oldest rows keeping the newest `purge_keep_pct`% (default 60%).
Runs after every DB write cycle. Configurable in `config.ini`.

### Migrations

Run automatically at startup via `ALTER TABLE ... ADD/DROP COLUMN IF EXISTS`:
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
| `NO TC` | Grey | Thermocouple fault or no sensor connected |

---

## Change History

### v1.0 — Initial Release
- Original `app.py` with live Modbus dashboard
- Real-time WebSocket zone table, updates every 8 s
- Per-zone setpoint control (single and batch via `/api/setpoint`)
- `lynx_reader.py` Modbus TCP communication

### v2.0 — PostgreSQL Logging + History Viewer
- Added `lynx_db_logger.py` — background thread logs zone readings every 60 s
- Added `lynx_history.py` — Flask blueprint
- `/history` page with date/time range picker, PV / SP / Output% trend charts, data table
- `/api/history` JSON endpoint and `/api/history/csv` download
- PostgreSQL `TIMESTAMPTZ` storage — all times in UTC with timezone info
- Browser date pickers use local time; API receives UTC ISO-8601

### v2.1 — Config File
- All hardcoded settings extracted to `config.ini` (`configparser` INI format)
- Sections: `[modbus]`, `[database]`, `[flask]`, `[dashboard]`
- `lynx_set_all.py` migrated from `config.json` to `config.ini`
- `app.py` reduced to a 7-line launcher: `from lynx_dashboard import run`

### v2.2 — Database Auto-Creation
- `ensure_db_exists()` in `lynx_db_logger.py` creates DB, user, and grants on first run
- Admin credentials (`admin_user` / `admin_password`) in `[database]` config
- PostgreSQL 15+ fix: `GRANT ALL ON SCHEMA public` issued via a separate connection to the target DB (not the maintenance DB)
- Blank `admin_password` omits the kwarg entirely → peer/trust auth support
- `test_connection.py` runs bootstrap before testing app-user connection

### v2.3 — Schema Creation Fix
- Fixed `psycopg2` single-statement limitation: DDL split into individual `execute()` calls
- Previously a single `execute()` with multiple `;`-separated statements only ran the first
- Table and indexes now reliably created on first connect

### v2.4 — Settings Page
- `/settings` web page with `GET /api/settings` and `POST /api/settings`
- Configurable live (no restart): OI Gateway host, port, timeout, scan interval, DB log interval
- `POST /api/settings/test` — test Modbus without saving
- Settings persisted back to `config.ini` on save
- `scan_interval` added to `[modbus]` section (default 8 s)
- Scanner reads globals under `settings_lock` each cycle; reconnects Modbus if host/port/timeout changed

### v2.5 — Auto-Purge
- `LynxDBLogger._check_and_purge()` checks `shutil.disk_usage()` after every write
- Mount point resolved from `os.path.abspath(__file__)` of `lynx_dashboard.py`
- Purges oldest rows when disk ≥ `purge_threshold`% (default 80%)
- Keeps newest `purge_keep_pct`% of rows (default 60%)
- `purge_threshold`, `purge_keep_pct` added to `[database]` in `config.ini`

### v2.6 — History Page Improvements
- CSV first column renamed from `ts` to `Time`
- CSV pivoted by timestamp: one row per scan, one column group per zone
- Zone selector panel: colour-coded chips toggle individual zones across all charts and table
- Chart order changed to: PV → Output% → Setpoint
- HTML moved to `templates/` directory (`render_template` instead of `render_template_string`)
- Chart.js built-in legend replaced by zone chips

### v2.7 — Current (A) Trend Chart
- Current (A) trend chart added to history page (inserted before Output%)
- Chart order: PV → Current (A) → Output% → Setpoint

### v2.8 — Power & Energy
- `line_voltage` setting added to `[modbus]` config and Settings page
- `power_w` computed as `current_a × line_voltage` at query time — never stored in DB
- Migration: `DROP COLUMN IF EXISTS power_w` cleans up old stored column if present
- Power (W) trend chart added to history page (between PV and Current)
- Total Energy (kWh) stat card on history page — trapezoidal integration over query window
- `power_w` (W) column added to history CSV per zone group
- `line_voltage` change in Settings updates `lynx_history._line_voltage` live

### v2.9 — Local Timezone in Charts
- Switched Chart.js time adapter: `chartjs-adapter-date-fns` → `chartjs-adapter-luxon`
- Browser timezone auto-detected: `Intl.DateTimeFormat().resolvedOptions().timeZone`
- All chart x-axis ticks and tooltips now render in browser local time
- API continues to return UTC ISO-8601; conversion is client-side only

### v3.0 — Hourly Energy Page
- New `/energy` page — hourly kWh aggregated per zone
- `GET /api/energy/hourly` — SQL trapezoidal integration using `LAG()` window function
- `GET /api/energy/hourly/csv` — pivoted CSV, one column per zone, Total kWh column
- Stacked bar chart (Chart.js) with total kWh label on top of each bar (`chartjs-plugin-datalabels`)
- Interactive zone selection: click legend item, bar segment, or zone chip to toggle
- Zone selector chips (same pattern as history page)
- Summary stat cards: total hours, total kWh, peak hour kWh, avg kWh/h, line voltage
- Data table: per-zone kWh per hour, row totals, footer grand totals
- Hours displayed in browser local timezone via Luxon
- Energy link added to dashboard and history page headers
