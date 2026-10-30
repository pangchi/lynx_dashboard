# Changelog

All notable changes to **BriskHeat LYNX Dashboard** are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [4.5.1] – 2026-10-19

### Changed
- **Dashboard** redesigned as a compact layout for Raspberry Pi 7" touchscreen (800×480)
  - Slim 36 px nav bar replaces tall blue header banner
  - Base font reduced to 13 px; body padding/margins cut ~60 %
  - Table cell padding 4–5 px (was 10–12 px); SP input 62 px wide
  - Buttons trimmed; all pages reachable without scrolling

### Added
- **🎛 Operator** nav link on the dashboard
- **Orange override banner** on dashboard — appears whenever an operator override is active, with live countdown and a "Manage" link to `/operator`
- **Toast notifications** replace `alert()` for setpoint feedback (green = success, red = error)
- **Live dot indicator** replaces spinner once WebSocket connection is established
- Override banner polls `/api/override/status` every 30 s

---

## [4.5] – 2026-10

### Added
- **`lynx_operator.py`** — Flask blueprint providing the `/operator` timed-override interface
  - `POST /api/override/start` — saves current setpoints, writes new SP to all zones, starts countdown timer
  - `POST /api/override/cancel` — immediately restores saved setpoints
  - `GET /api/override/status` — returns `{active, setpoint, end_utc, remaining_s, remaining_str}`
  - Background watchdog thread auto-cancels when the timer expires (checks every 30 s)
  - `override_active()` exported for schedule suppression
- **`templates/operator.html`** — Operator page
  - Orange active-override banner with live countdown
  - Status card (4 stat boxes: SP, end time, remaining, zones affected)
  - Cancel button and start form with quick-pick duration buttons (1 h 2 h 4 h 6 h 8 h 12 h)
  - Activity log panel; 15 s polling

### Changed
- **`lynx_dashboard.py`** — registers `operator_bp`; wires `_op_set_all`, `_op_get_zones`, `_op_restore` callbacks into `init_operator()`
- **`lynx_schedule.py`** — `_fire()` calls `_override_active()` guard before applying scheduled setpoints; suppressed events are logged at INFO level; deferred import avoids circular dependency

---

## [4.4] – 2026

### Added
- **Centipede 2 energy/power calculation** — three automatic modes:
  1. LYNX: `current_a × line_voltage` (exact watts, unchanged)
  2. Centipede 2 with nominal wattage: `output_pct / 100 × nominal_power_w`
  3. Centipede 2, no wattage set: `output_pct` used as relative proxy
- **Nominal Heater Power (W)** field in Settings page and `config.ini`

### Fixed
- Inline `;` comments in `config.ini` caused `configparser.getint()` to fail — removed all inline comments from `config.ini.example`

---

## [4.3] – 2026

### Added
- **Full Centipede 2 support** (C2MOD-OI-7 OI gateway, LUI text protocol over RS-232)
  - Login sequence: CR-only pokes, character-by-character password, `Dump:N` command
  - Duplicate readings per zone deduplicated (last wins)
  - Zone-to-line mapping: zones 1–16 = Line 1, 17–32 = Line 2, etc.
  - 73 active zones confirmed working at 110 °C setpoint
- New Settings fields: **Max Units to Scan**, **OI Password**, **Dump Duration (s)**
- `c2_max_units`, `c2_password`, `c2_dump_secs` in `config.ini` and Settings page
- `c2_scan.py` — offline register scanner for troubleshooting
- `lynx_reader.py` CLI flags: `--password`, `--dump-secs`, `--debug`

### Fixed
- Duplicate `_read_all_centipede2` / `_read_lynx_line` definitions — Python silently used the last one, overriding the correct implementation
- `property 'connected' has no setter` — pymodbus 3.13 made `connected` read-only; replaced with `client.close()`
- `_slave_kwarg()` now inspects method signature at runtime to select `device_id=`, `slave=`, or `unit=` per pymodbus version

---

## [4.2] – 2026

### Added
- Settings page: **TCP / RTU (Serial)** mode toggle
- RTU fields: Serial Port, Baud Rate (dropdown), Parity
- **Device Type** dropdown: BriskHeat LYNX (OI Gateway) / BriskHeat Centipede 2 (RS-232)
- Selecting Centipede 2 locks baud to 19200, hides TCP fields, shows C2-specific fields
- Serial mode shown in dashboard footer
- `install.sh` adds service user to `dialout` group; systemd service sets `Group=dialout`

### Fixed
- `device_type` never persisted to `config.ini` — `cfg.set()` call was missing
- `modbus_mode` was read from button CSS class — replaced with hidden input as single source of truth

---

## [4.1] – 2026

### Added
- **Power (W) Trend** chart — **Σ Sum** toggle collapses all active zones into one total line
- **Current (A) Trend** chart — same Σ Sum toggle for total system current
- Toggle resets automatically on each new query; chart title updates to indicate sum mode

---

## [4.0] – 2026

### Fixed
- Werkzeug production error — `allow_unsafe_werkzeug=True` added to `socketio.run()`
- `config.ini` not found when run as systemd service — all paths now use `abspath(__file__)`
- `app.py` sets `sys.path`, `os.chdir()`, and `LYNX_APP_DIR` environment variable
- `VIRTUAL_ENV`, `HOME`, `LYNX_APP_DIR` added to systemd service `[Service]` environment block

---

## [3.9] – 2025

### Changed
- `lynx_reader.py` rewritten with correct LYNX register map
  - `REGISTERS_PER_ZONE = 24`, `ZONES_PER_LINE = 32`
  - Separate holding registers (SP) and input registers (PV / Output% / Current)
  - Temperature scaling ×100, current ×1000
  - `0x8000` / `0xFFFF` sentinel values handled correctly

---

## [3.8] – 2025

### Added
- `install.sh` — automated Raspberry Pi installer (dependencies, venv, PostgreSQL, systemd service, ufw)
- `uninstall.sh` — clean removal script
- `run.sh` — manual venv launcher
- `--break-system-packages` flag for Bookworm (Python 3.11+)

---

## [3.7] – 2025

### Fixed
- Blank `admin_password` omits `password` kwarg → peer/trust authentication (Linux default)
- `test_connection.py` shows masked password in output

---

## [3.6] – 2025

### Fixed
- Scanner not starting until a browser connected — now starts immediately at `app.py` launch
- DB writes skipping when queue was empty — writer now waits up to one interval for a snapshot
- DDL running inside a transaction block — all DDL connections now use `autocommit=True`

### Added
- `logging.basicConfig(INFO)` in `app.py`

---

## [3.5] – 2025

### Changed
- Auto-purge: `_get_mount_point()` finds the true filesystem partition via `os.path.ismount()`
- Startup log confirms monitored partition path and current usage percentage
- Purge threshold and keep % now configurable live via the Settings page

---

## [3.4] – 2025

### Added
- `lynx_schedule.py` Flask blueprint — `/schedule` page and background scheduler thread
- Per-schedule: name, setpoint, time (HH:MM), weekdays (0=Mon–6=Sun), target zones
- Enable/disable toggle per schedule; schedules stored in `schedule.json`
- REST API: `GET/POST /api/schedule`, `DELETE /api/schedule/<id>`, `POST .../toggle`
- Scheduler checks every 30 s; fires within one check cycle of the configured minute

---

## [3.3] – 2025

### Fixed
- Energy page used UTC-only bucket boundaries — browser now sends `?tz=<IANA>` and SQL `date_trunc()` uses the correct local timezone

---

## [3.2] – 2025

### Added
- Hourly / Daily toggle on the Energy page (defaults: 24 h / 30 days)
- `/api/energy/daily` and `/api/energy/daily/csv`

---

## [3.1] – 2025

### Fixed
- kWh unit error — divisor was 3,600 (giving Wh); corrected to 3,600,000
- `LAG()` window function could cross the query time boundary — filtered into a CTE first
- Large Δt segments (> 600 s) inflated kWh across restarts/logging gaps — now excluded

---

## [3.0] – 2025

### Added
- `/energy` page — stacked bar chart, zone chips, stat cards, data table
- `/api/energy/hourly` and `/api/energy/hourly/csv`
- SQL trapezoidal integration using `LAG()` window function
- `chartjs-plugin-datalabels` for total kWh labels on bars

---

## [2.9] – 2025

### Changed
- Chart x-axis and tooltips converted to browser local time using `chartjs-adapter-luxon`

---

## [2.8] – 2025

### Added
- `line_voltage` in `config.ini` and Settings page
- `power_w` computed on read as `current_a × line_voltage` — not stored in DB
- Power (W) trend chart on history page
- Total Energy (kWh) stat card on history page

---

## [2.7] – 2025

### Added
- Current (A) trend chart on history page (shown before Output%)

---

## [2.6] – 2025

### Changed
- History CSV now pivoted by timestamp: one row per scan, one column group per zone
- Zone selector chips on history page (toggle individual zones across all charts)
- All HTML pages moved to `templates/` directory

---

## [2.5] – 2025

### Added
- Auto-purge: monitors disk usage after every DB write cycle
- Purges oldest rows when disk usage ≥ `purge_threshold`% (default 80%)
- Keeps newest `purge_keep_pct`% of rows (default 60%)

---

## [2.4] – 2025

### Added
- `/settings` page — live GET/POST via `/api/settings`
- Configurable without restart: OI host/port/timeout, scan interval, DB log interval
- `POST /api/settings/test` — test Modbus connection without saving

---

## [2.3] – 2025

### Fixed
- `psycopg2` single-statement limitation — DDL split into individual `execute()` calls

---

## [2.2] – 2025

### Added
- `ensure_db_exists()` — creates database, user, and grants on first run
- PostgreSQL 15+ schema grant fix (`GRANT ALL ON SCHEMA public`)
- Blank `admin_password` → peer/trust authentication

---

## [2.1] – 2025

### Changed
- All settings extracted to `config.ini` (was `config.json`)
- `lynx_set_all.py` migrated to `config.ini`
- `app.py` reduced to a thin launcher

---

## [2.0] – 2025

### Added
- `lynx_db_logger.py` — background thread logging zone readings every 60 s to PostgreSQL
- `lynx_history.py` Flask blueprint — `/history` page, `/api/history` JSON + CSV
- History page: date/time picker, PV/SP/Output% trend charts, data table
- Timestamps stored as `TIMESTAMPTZ` (UTC)

---

## [1.0] – 2025

### Added
- Live Modbus dashboard with real-time WebSocket zone table
- Per-zone setpoint control — single and batch via `/api/setpoint`
- `lynx_reader.py` Modbus TCP communication
