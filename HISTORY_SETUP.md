# PostgreSQL Logging & History Viewer – Setup Guide

Three files are added to the project:

| File | Purpose |
|---|---|
| `lynx_db_logger.py` | Background thread; inserts zone readings to PostgreSQL every 60 s |
| `lynx_history.py` | Flask blueprint; adds `/history` page + `/api/history` + `/api/history/csv` |
| `lynx_dashboard.py` | Updated original – wires the two modules in, adds "📈 History" link |

---

## 1. PostgreSQL setup

```sql
-- Run once as a superuser
CREATE DATABASE lynx;
CREATE USER lynx_user WITH PASSWORD 'secret';
GRANT ALL PRIVILEGES ON DATABASE lynx TO lynx_user;
-- The table is created automatically on first run.
```

---

## 2. Edit config.ini

All settings live in `config.ini` — no need to touch Python files.

```ini
[modbus]
host    = 192.168.200.20   ; OI Gateway IP
port    = 502
timeout = 4.0
lines   = 1,2,3,4          ; comma-separated lines to scan

[database]
host         = localhost
port         = 5432
name         = lynx
user         = lynx_user
password     = secret
log_interval = 60          ; seconds between DB writes

[flask]
secret_key = BriskHeat2025
port       = 5000
```

---

## 3. Run (unchanged command)

```bash
python3 lynx_dashboard.py
```

`psycopg2-binary` is auto-installed on first run if missing.

---

## 4. New URLs

| URL | Description |
|---|---|
| `http://HOST:5000/` | Live dashboard (unchanged) |
| `http://HOST:5000/history` | Trend viewer with date/time selector |
| `http://HOST:5000/api/history?start=2025-01-01&end=2025-01-02` | JSON data |
| `http://HOST:5000/api/history/csv?start=...&end=...` | CSV download |

### Query parameters

| Param | Default | Description |
|---|---|---|
| `start` | 24 h ago | `YYYY-MM-DD HH:MM` or ISO-8601 |
| `end` | now | same format |
| `line` | all | filter by line number |
| `zone` | all | filter by zone number |
| `limit` | 10 000 | max rows (hard cap 50 000) |

---

## 5. Database schema (auto-created)

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

Indexed on `ts DESC` and `(line, zone, ts DESC)` for fast range queries.

---

## History page features

- **Date/time pickers** – start & end (defaults to last 24 h)
- **Line / Zone filters** – narrow to a specific zone
- **3 charts** – PV trend, Setpoint trend, Output % trend (Chart.js, time-axis)
- **Summary stats** – row count, avg/max/min PV
- **Data table** – last 500 rows shown inline
- **CSV download** – full dataset streamed to browser
