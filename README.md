# BriskHeat LYNX – Real-Time Dashboard

A Python-based web dashboard for the BriskHeat LYNX® Temperature Control System.  
Monitors all zones in real-time over Modbus, logs data to PostgreSQL, and provides a history viewer with trend charts and CSV export.

---

## Files

| File | Purpose |
|---|---|
| `app.py` | Entry point — run this to start the dashboard |
| `lynx_dashboard.py` | Main Flask app, WebSocket scanner, settings & API routes |
| `lynx_history.py` | Flask blueprint — history viewer page and `/api/history` routes |
| `lynx_db_logger.py` | Background PostgreSQL logger with auto-purge |
| `lynx_reader.py` | Modbus communication class (from original project) |
| `lynx_set_all.py` | CLI tool to set all zones to the same setpoint |
| `test_connection.py` | Connectivity test for Modbus and PostgreSQL |
| `config.ini` | All configuration — the only file you normally need to edit |

---

## Quick Start

### 1. Edit `config.ini`

```ini
[modbus]
host          = 192.168.200.20   ; OI Gateway IP address
port          = 502
timeout       = 4.0
scan_interval = 8                ; seconds between Modbus polls
lines         = 1,2,3,4

[database]
host           = localhost
port           = 5432
name           = lynx
user           = lynx_user
password       = secret
log_interval   = 60              ; seconds between DB writes
purge_threshold = 80             ; purge oldest rows when disk >= 80% full
purge_keep_pct  = 60             ; keep newest 60% of rows after purge
admin_user     = postgres        ; superuser for first-run DB creation
admin_password =                 ; leave blank for peer/trust auth

[flask]
secret_key = BriskHeat2025
port       = 5000

[dashboard]
ip      = 127.0.0.1              ; used by lynx_set_all.py
timeout = 8
```

### 2. Test connectivity (optional but recommended)

```bash
python test_connection.py
```

This verifies the Modbus gateway and PostgreSQL connection, and auto-creates the database and user if they don't exist yet.

### 3. Run

```bash
python app.py
```

Missing Python packages (`flask`, `flask-socketio`, `pymodbus`, `eventlet`, `psycopg2-binary`) are installed automatically on first run.

---

## Web Pages

| URL | Description |
|---|---|
| `http://HOST:5000/` | Live dashboard — real-time zone table, WebSocket updates |
| `http://HOST:5000/history` | History viewer — trend charts, date/time filter, CSV download |
| `http://HOST:5000/settings` | Settings — configure host, port, timeout, scan & DB intervals |

---

## REST API

### GET `/api/status`
Returns the latest zone snapshot.
```json
{
  "zones": [{"line": 1, "zone": 1, "pv": 85.2, "setpoint": 90.0, "status": "HEATING", ...}],
  "updated": 1736123456.789
}
```

### POST `/api/setpoint`
Set a single zone setpoint:
```json
{ "line": 1, "zone": 2, "sp": 95.0 }
```
Batch update:
```json
{ "updates": [{"line": 1, "zone": 1, "sp": 90.0}, {"line": 1, "zone": 2, "sp": 95.0}] }
```

### GET `/api/history`
Query historical data. All parameters optional.

| Parameter | Default | Description |
|---|---|---|
| `start` | 24 h ago | `YYYY-MM-DD HH:MM` or ISO-8601 |
| `end` | now | same format |
| `line` | all | filter by line number |
| `zone` | all | filter by zone number |
| `limit` | 10 000 | max rows returned (hard cap 50 000) |

```
GET /api/history?start=2025-01-15 08:00&end=2025-01-15 18:00&line=1
```

### GET `/api/history/csv`
Same parameters as `/api/history`. Returns a CSV file download.

Rows are **pivoted by timestamp** — one row per scan, one column group per zone:

```
Time,                    L1-Z1 PV, L1-Z1 SP, L1-Z1 Out%, L1-Z1 A, L1-Z1 Status, L1-Z2 PV, ...
2025-01-15T08:00:00+00,  85.2,     90.0,     42.0,       1.23,    HEATING,      91.0,     ...
2025-01-15T08:01:00+00,  86.1,     90.0,     44.0,       1.25,    HEATING,      91.2,     ...
```

Zones are auto-discovered from the returned data and sorted by line/zone number. Missing readings for a zone at a given timestamp are left blank.

### GET `/api/settings`
Returns current live settings.

### POST `/api/settings`
Update settings without restarting. Changes are applied immediately and persisted to `config.ini`.
```json
{
  "oi_host": "192.168.200.20",
  "oi_port": 502,
  "oi_timeout": 4.0,
  "scan_interval": 8,
  "db_interval": 60
}
```

### POST `/api/settings/test`
Test a Modbus connection without saving. Returns zone count on success.
```json
{ "oi_host": "192.168.200.20", "oi_port": 502, "oi_timeout": 4.0 }
```

---

## CLI Tools

### `lynx_set_all.py` — Set all zones to the same temperature

```bash
python lynx_set_all.py 75
python lynx_set_all.py --temp 70
python lynx_set_all.py 80 --config /path/to/config.ini
```

Connects to the dashboard API (configured via `[dashboard]` in `config.ini`) and batch-sets every active zone to the given setpoint.

### `test_connection.py` — Verify connectivity

```bash
python test_connection.py
```

Checks both the Modbus OI Gateway and PostgreSQL. On first run it will:
- Create the PostgreSQL user if missing
- Create the database if missing
- Grant schema privileges (required on PostgreSQL 15+)

---

## PostgreSQL Notes

### First run
The database, user, table, and indexes are all created automatically. No manual SQL required.

### PostgreSQL 15+
PostgreSQL 15 revoked `CREATE` on the `public` schema by default. The bootstrap step handles this automatically with:
```sql
GRANT ALL ON SCHEMA public TO lynx_user;
```

### Manual setup (if preferred)
```sql
CREATE DATABASE lynx;
CREATE USER lynx_user WITH PASSWORD 'secret';
GRANT ALL PRIVILEGES ON DATABASE lynx TO lynx_user;
\c lynx
GRANT ALL ON SCHEMA public TO lynx_user;
```

### Schema
```sql
CREATE TABLE lynx_zone_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    line        SMALLINT    NOT NULL,
    zone        SMALLINT    NOT NULL,
    pv          REAL,
    setpoint    REAL,
    output_pct  REAL,
    current_a   REAL,
    status      TEXT
);
```

### Auto-purge
When disk usage on the application's mount point exceeds `purge_threshold` (default 80%), the oldest rows are deleted automatically, keeping the newest `purge_keep_pct` percent (default 60%). Configure in `config.ini` under `[database]`.

---

## Zone Status Values

| Status | Meaning |
|---|---|
| `OK` | At setpoint |
| `HEATING` | Actively heating toward setpoint |
| `OVER TEMP` | Temperature exceeded setpoint |
| `NO TC` | Thermocouple fault / no sensor |
