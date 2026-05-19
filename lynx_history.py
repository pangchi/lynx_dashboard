#!/usr/bin/env python3
"""
lynx_history.py
---------------
Flask blueprint that adds three routes to lynx_dashboard:

  GET  /history              → trend viewer web page
  GET  /api/history?...      → JSON data for charts
  GET  /api/history/csv?...  → CSV download

Query-string parameters (all optional):
  start   "YYYY-MM-DD HH:MM" or ISO-8601   default: 24 h ago
  end     same format                       default: now
  line    int                               filter by line number
  zone    int                               filter by zone number
  limit   int (max 50 000)                  default: 10 000

Mount in lynx_dashboard.py:
----------------------------
from lynx_history import history_bp, init_history_db

app.register_blueprint(history_bp)
init_history_db(host=..., port=..., dbname=..., user=..., password=...)
"""

import io
import csv
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, request, jsonify, Response, render_template

log = logging.getLogger("lynx_history")

history_bp = Blueprint("history", __name__, template_folder="templates")

# ── connection state ──────────────────────────────────────────────────────────
# Stored at module level; init_history_db() populates before first request.

_db_cfg  = {}   # filled by init_history_db()
_conn    = None  # persistent connection; replaced on error


# Each statement must be executed separately – psycopg2 only runs
# one statement per execute() call.
_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS lynx_zone_log (
        id          BIGSERIAL PRIMARY KEY,
        ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        line        SMALLINT    NOT NULL,
        zone        SMALLINT    NOT NULL,
        pv          REAL,
        setpoint    REAL,
        output_pct  REAL,
        current_a   REAL,
        status      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lynx_ts ON lynx_zone_log (ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_lynx_line_zone ON lynx_zone_log (line, zone, ts DESC)",
    # Migration: drop stored power_w column (now computed on read)
    "ALTER TABLE lynx_zone_log DROP COLUMN IF EXISTS power_w",
]


def _ensure_schema(conn):
    """Create table, indexes, and run migrations with autocommit to avoid
    'cannot run inside a transaction block' errors on DDL statements."""
    conn.autocommit = True
    with conn.cursor() as cur:
        for stmt in _DDL_STATEMENTS:
            try:
                cur.execute(stmt)
            except Exception as e:
                log.warning("DDL step skipped (%s): %s",
                            stmt.strip()[:60], e)
    conn.autocommit = False


_line_voltage = 240.0   # set via init_history_db(); used in SELECT


def init_history_db(host="localhost", port=5432, dbname="lynx",
                    user="postgres", password="", line_voltage=240.0):
    """Call once at startup – registers credentials and ensures the table exists."""
    global _db_cfg, _conn, _line_voltage
    _db_cfg = dict(host=host, port=port, dbname=dbname,
                   user=user, password=password)
    _line_voltage = line_voltage
    _conn = None

    try:
        c = _new_conn()
        _ensure_schema(c)
        c.close()
        log.info("History DB ready → %s@%s:%s/%s", user, host, port, dbname)
    except Exception as exc:
        log.error("History DB connection failed: %s", exc)
        log.error("Check [database] section in config.ini "
                  "(host=%s port=%s name=%s user=%s)", host, port, dbname, user)


def _new_conn():
    """Open a brand-new psycopg2 connection using the stored config.
    Omits the password kwarg when blank so PostgreSQL can use peer/trust auth."""
    try:
        import psycopg2
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip",
                               "install", "--quiet", "psycopg2-binary"])
        import psycopg2
    if not _db_cfg:
        raise RuntimeError("init_history_db() was never called")
    kwargs = {k: v for k, v in _db_cfg.items() if k != "password" or v}
    return psycopg2.connect(**kwargs)


def _get_conn():
    """Return a live connection, reconnecting transparently on error."""
    global _conn
    try:
        if _conn is None or _conn.closed:
            raise Exception("no connection")
        # Quick liveness check
        _conn.cursor().execute("SELECT 1")
    except Exception:
        try:
            _conn = _new_conn()
        except Exception as exc:
            log.error("Cannot connect to history DB: %s", exc)
            raise
    return _conn


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_dt(s, default):
    """
    Parse a datetime string from the browser.
    The JS side sends ISO-8601 strings with a Z suffix (UTC),
    converted from the user's local time via toISOString().
    Falls back to treating bare strings as UTC.
    """
    if not s:
        return default
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Cannot parse datetime: {s!r}")


def _query(start, end, line, zone, limit):
    """Return list of dicts from lynx_zone_log."""
    import psycopg2.extras

    sql = """
        SELECT ts, line, zone, pv, setpoint, output_pct, current_a,
               current_a * %(voltage)s AS power_w, status
        FROM   lynx_zone_log
        WHERE  ts BETWEEN %(start)s AND %(end)s
    """
    params = {"start": start, "end": end, "voltage": _line_voltage}
    if line:
        sql += " AND line = %(line)s"
        params["line"] = int(line)
    if zone:
        sql += " AND zone = %(zone)s"
        params["zone"] = int(zone)
    sql += " ORDER BY ts ASC LIMIT %(limit)s"
    params["limit"] = min(int(limit or 10000), 50000)

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        # Connection may be stale; force reconnect on next call
        global _conn
        _conn = None
        raise


# ── API: JSON ─────────────────────────────────────────────────────────────────

@history_bp.route("/api/history")
def api_history():
    try:
        now   = datetime.now(timezone.utc)
        start = _parse_dt(request.args.get("start"), now - timedelta(hours=24))
        end   = _parse_dt(request.args.get("end"),   now)
        rows  = _query(start, end,
                       request.args.get("line"),
                       request.args.get("zone"),
                       request.args.get("limit", 10000))
        for r in rows:
            r["ts"] = r["ts"].isoformat()
        return jsonify({"count": len(rows), "rows": rows})
    except Exception as exc:
        log.error("api_history: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ── CSV pivot helper ─────────────────────────────────────────────────────────

def _pivot_to_csv(rows):
    """
    Pivot zone rows so each unique timestamp is one CSV row.

    Input (multiple rows per timestamp):
        ts                      | line | zone | pv   | setpoint | output_pct | current_a | status
        2025-01-15T08:00:00+00  |  1   |  1   | 85.2 | 90.0     | 42.0       | 1.23      | HEATING
        2025-01-15T08:00:00+00  |  1   |  2   | 91.0 | 90.0     |  0.0       | 0.00      | OK

    Output (one row per timestamp, one column-group per zone):
        Time                    | L1-Z1 PV | L1-Z1 SP | L1-Z1 Out% | L1-Z1 A | L1-Z1 Status | L1-Z2 PV | ...
        2025-01-15T08:00:00+00  | 85.2     | 90.0     | 42.0       | 1.23    | HEATING      | 91.0     | ...
    """
    if not rows:
        return "Time\r\n"

    # Collect ordered unique zone keys and timestamps
    zones_seen = {}   # (line, zone) -> insertion-order index
    times_seen = {}   # ts_str -> insertion-order index
    for r in rows:
        ts_str = r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else str(r["ts"])
        key = (r["line"], r["zone"])
        if ts_str not in times_seen:
            times_seen[ts_str] = len(times_seen)
        if key not in zones_seen:
            zones_seen[key] = len(zones_seen)

    zone_keys   = sorted(zones_seen, key=zones_seen.get)
    time_keys   = sorted(times_seen, key=times_seen.get)

    # Build lookup: (ts_str, line, zone) -> row dict
    lookup = {}
    for r in rows:
        ts_str = r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else str(r["ts"])
        lookup[(ts_str, r["line"], r["zone"])] = r

    # Write CSV
    buf = io.StringIO()
    writer = csv.writer(buf)

    # Header row
    header = ["Time"]
    for (line, zone) in zone_keys:
        prefix = f"L{line}-Z{zone}"
        header += [f"{prefix} PV", f"{prefix} SP",
                   f"{prefix} W",
                   f"{prefix} Out%", f"{prefix} A", f"{prefix} Status"]
    writer.writerow(header)

    # Data rows
    for ts_str in time_keys:
        row = [ts_str]
        for (line, zone) in zone_keys:
            r = lookup.get((ts_str, line, zone))
            if r:
                row += [
                    r["pv"]         if r["pv"]         is not None else "",
                    r["setpoint"]   if r["setpoint"]   is not None else "",
                    r["power_w"]    if r["power_w"]    is not None else "",
                    r["output_pct"] if r["output_pct"] is not None else "",
                    r["current_a"]  if r["current_a"]  is not None else "",
                    r["status"]     or "",
                ]
            else:
                row += ["", "", "", "", "", ""]
        writer.writerow(row)

    return buf.getvalue()


# ── API: CSV download ─────────────────────────────────────────────────────────

@history_bp.route("/api/history/csv")
def api_history_csv():
    try:
        now   = datetime.now(timezone.utc)
        start = _parse_dt(request.args.get("start"), now - timedelta(hours=24))
        end   = _parse_dt(request.args.get("end"),   now)
        rows  = _query(start, end,
                       request.args.get("line"),
                       request.args.get("zone"),
                       request.args.get("limit", 50000))

        buf = io.StringIO()
        buf.write(_pivot_to_csv(rows))

        fname = (f"lynx_{start.strftime('%Y%m%d_%H%M')}"
                 f"_{end.strftime('%Y%m%d_%H%M')}.csv")
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'}
        )
    except Exception as exc:
        log.error("api_history_csv: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ── Web page ──────────────────────────────────────────────────────────────────


@history_bp.route("/history")
def history_page():
    return render_template('history.html')


# ── Hourly energy API ─────────────────────────────────────────────────────────

@history_bp.route("/api/energy/hourly")
def api_energy_hourly():
    """
    Return hourly kWh aggregated per zone using trapezoidal integration in SQL.

    Query params: start, end, line, zone  (same as /api/history)

    Response:
    {
      "voltage": 240,
      "rows": [
        {"hour": "2025-01-15T08:00:00+00:00", "line": 1, "zone": 1, "kwh": 0.342},
        ...
      ]
    }
    """
    try:
        now   = datetime.now(timezone.utc)
        start = _parse_dt(request.args.get("start"), now - timedelta(hours=24))
        end   = _parse_dt(request.args.get("end"),   now)

        # Trapezoidal kWh in pure SQL:
        #   For each consecutive pair of readings within the same hour bucket,
        #   energy = avg_power_W * delta_seconds / 3600
        # We use a window LAG() to get the previous reading's values.
        sql = """
        WITH lagged AS (
            SELECT
                date_trunc('hour', ts)           AS hour,
                line, zone,
                ts,
                current_a * %(voltage)s          AS power_w,
                LAG(ts)          OVER w           AS prev_ts,
                LAG(current_a * %(voltage)s) OVER w AS prev_power_w
            FROM lynx_zone_log
            WHERE ts BETWEEN %(start)s AND %(end)s
              {line_filter}
              {zone_filter}
            WINDOW w AS (PARTITION BY line, zone ORDER BY ts)
        ),
        trapezoid AS (
            SELECT
                hour, line, zone,
                CASE
                    WHEN prev_ts IS NOT NULL
                     AND date_trunc('hour', ts) = date_trunc('hour', prev_ts)
                    THEN (power_w + prev_power_w) / 2.0
                         * EXTRACT(EPOCH FROM (ts - prev_ts)) / 3600.0
                    ELSE 0
                END AS kwh_segment
            FROM lagged
        )
        SELECT
            hour,
            line,
            zone,
            ROUND(CAST(SUM(kwh_segment) AS NUMERIC), 4) AS kwh
        FROM trapezoid
        GROUP BY hour, line, zone
        ORDER BY hour ASC, line ASC, zone ASC
        """

        params = {"start": start, "end": end, "voltage": _line_voltage}

        line_filter = ""
        zone_filter = ""
        if request.args.get("line"):
            line_filter = "AND line = %(line)s"
            params["line"] = int(request.args.get("line"))
        if request.args.get("zone"):
            zone_filter = "AND zone = %(zone)s"
            params["zone"] = int(request.args.get("zone"))

        sql = sql.format(line_filter=line_filter, zone_filter=zone_filter)

        import psycopg2.extras
        conn = _get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
        except Exception:
            global _conn
            _conn = None
            raise

        for r in rows:
            r["hour"] = r["hour"].isoformat()
            r["kwh"]  = float(r["kwh"])

        return jsonify({"voltage": _line_voltage, "rows": rows})

    except Exception as exc:
        log.error("api_energy_hourly: %s", exc)
        return jsonify({"error": str(exc)}), 500


@history_bp.route("/api/energy/hourly/csv")
def api_energy_hourly_csv():
    """CSV download of hourly energy data, pivoted by hour with one column per zone."""
    try:
        now   = datetime.now(timezone.utc)
        start = _parse_dt(request.args.get("start"), now - timedelta(hours=24))
        end   = _parse_dt(request.args.get("end"),   now)

        # Re-use the JSON endpoint logic by calling directly
        from flask import current_app
        with current_app.test_request_context(
            "/api/energy/hourly?" + request.query_string.decode()
        ):
            pass

        # Fetch data directly
        resp = api_energy_hourly()
        data = resp.get_json()
        if "error" in data:
            return jsonify(data), 500

        rows = data["rows"]
        if not rows:
            return Response("Hour\r\n", mimetype="text/csv",
                            headers={"Content-Disposition": 'attachment; filename="energy.csv"'})

        # Pivot: hours as rows, zones as columns
        zone_keys = sorted({(r["line"], r["zone"]) for r in rows},
                           key=lambda x: (x[0], x[1]))
        hours     = sorted({r["hour"] for r in rows})
        lookup    = {(r["hour"], r["line"], r["zone"]): r["kwh"] for r in rows}

        buf    = io.StringIO()
        writer = csv.writer(buf)

        header = ["Hour"] + [f"L{l}-Z{z} kWh" for l, z in zone_keys]
        total_cols = [f"L{l}-Z{z} kWh" for l, z in zone_keys]
        header.append("Total kWh")
        writer.writerow(header)

        for hour in hours:
            row = [hour]
            total = 0.0
            for l, z in zone_keys:
                val = lookup.get((hour, l, z), "")
                row.append(val)
                if val != "":
                    total += float(val)
            row.append(round(total, 4))
            writer.writerow(row)

        fname = (f"energy_{start.strftime('%Y%m%d_%H%M')}"
                 f"_{end.strftime('%Y%m%d_%H%M')}.csv")
        return Response(
            buf.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'}
        )
    except Exception as exc:
        log.error("api_energy_hourly_csv: %s", exc)
        return jsonify({"error": str(exc)}), 500


@history_bp.route("/energy")
def energy_page():
    return render_template("energy.html")
