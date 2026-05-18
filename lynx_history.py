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

from flask import Blueprint, request, jsonify, Response, render_template_string

log = logging.getLogger("lynx_history")

history_bp = Blueprint("history", __name__)

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
]


def _ensure_schema(conn):
    """Create table and indexes if they don't exist. Executes each DDL separately."""
    with conn.cursor() as cur:
        for stmt in _DDL_STATEMENTS:
            cur.execute(stmt)
    conn.commit()


def init_history_db(host="localhost", port=5432, dbname="lynx",
                    user="postgres", password=""):
    """Call once at startup – registers credentials and ensures the table exists."""
    global _db_cfg, _conn
    _db_cfg = dict(host=host, port=port, dbname=dbname,
                   user=user, password=password)
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
        SELECT ts, line, zone, pv, setpoint, output_pct, current_a, status
        FROM   lynx_zone_log
        WHERE  ts BETWEEN %(start)s AND %(end)s
    """
    params = {"start": start, "end": end}
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
                    r["output_pct"] if r["output_pct"] is not None else "",
                    r["current_a"]  if r["current_a"]  is not None else "",
                    r["status"]     or "",
                ]
            else:
                row += ["", "", "", "", ""]
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

HISTORY_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>LYNX – History Viewer</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:Arial,sans-serif;background:#f0f2f5;color:#333;min-height:100vh}

  .header{background:#2c3e50;color:#fff;padding:14px 24px;display:flex;
          align-items:center;justify-content:space-between}
  .header h1{font-size:1.2rem;font-weight:700}
  .header a{color:#aed6f1;text-decoration:none;font-size:.85rem}
  .header a:hover{color:#fff}

  .controls{background:#fff;border-bottom:1px solid #ddd;padding:14px 24px;
            display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
  .ctrl-group{display:flex;flex-direction:column;gap:4px}
  .ctrl-group label{font-size:.75rem;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.04em}
  .ctrl-group input,.ctrl-group select{
      padding:7px 10px;border:1px solid #ccc;border-radius:5px;font-size:.9rem;background:#fafafa}
  .ctrl-group input:focus,.ctrl-group select:focus{outline:none;border-color:#3498db;background:#fff}

  .btn{padding:8px 18px;border:none;border-radius:5px;cursor:pointer;font-size:.9rem;font-weight:600}
  .btn-primary{background:#3498db;color:#fff}.btn-primary:hover{background:#2980b9}
  .btn-success{background:#27ae60;color:#fff}.btn-success:hover{background:#219150}

  .stats{display:flex;gap:16px;padding:14px 24px;flex-wrap:wrap}
  .stat-card{background:#fff;border-radius:8px;padding:12px 20px;
             box-shadow:0 1px 4px rgba(0,0,0,.08);min-width:120px;text-align:center}
  .stat-card .val{font-size:1.6rem;font-weight:700;color:#2c3e50}
  .stat-card .lbl{font-size:.75rem;color:#888;margin-top:2px}

  .chart-wrap{background:#fff;margin:0 24px 24px;border-radius:8px;
              box-shadow:0 1px 6px rgba(0,0,0,.08);padding:20px}
  .chart-wrap h2{font-size:1rem;font-weight:600;color:#555;margin-bottom:14px}
  canvas{max-height:380px}

  .table-wrap{margin:0 24px 32px;background:#fff;border-radius:8px;
              box-shadow:0 1px 6px rgba(0,0,0,.08);overflow:auto}
  table{width:100%;border-collapse:collapse}
  th{background:#2c3e50;color:#fff;padding:10px 12px;text-align:left;font-size:.82rem}
  td{padding:8px 12px;font-size:.85rem;border-bottom:1px solid #f0f0f0}
  tr:hover td{background:#f7faff}

  .badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75rem;font-weight:600}
  .badge-ok{background:#d4edda;color:#155724}
  .badge-heat{background:#fff3cd;color:#856404}
  .badge-over{background:#f8d7da;color:#721c24}
  .badge-notc{background:#e2e3e5;color:#495057}

  .msg{padding:10px 16px;border-radius:5px;margin:12px 24px;font-size:.9rem}
  .msg-info{color:#0c5460;background:#d1ecf1}
  .msg-err{color:#721c24;background:#f8d7da}
  #loading{display:none;text-align:center;padding:40px;color:#888}
</style>
</head>
<body>

<div class="header">
  <h1>📈 BriskHeat LYNX – History Viewer</h1>
  <a href="/">← Live Dashboard</a>
</div>

<div class="controls">
  <div class="ctrl-group">
    <label>Start</label>
    <input type="datetime-local" id="startDt">
  </div>
  <div class="ctrl-group">
    <label>End</label>
    <input type="datetime-local" id="endDt">
  </div>
  <div class="ctrl-group">
    <label>Line</label>
    <select id="filterLine">
      <option value="">All</option>
      <option>1</option><option>2</option><option>3</option><option>4</option>
    </select>
  </div>
  <div class="ctrl-group">
    <label>Zone</label>
    <input type="number" id="filterZone" placeholder="All" min="1" max="64" style="width:80px">
  </div>
  <div class="ctrl-group">
    <label>Max rows</label>
    <select id="limitSel">
      <option value="1000">1 000</option>
      <option value="5000">5 000</option>
      <option value="10000" selected>10 000</option>
      <option value="50000">50 000</option>
    </select>
  </div>
  <button class="btn btn-primary" onclick="loadData()">🔍 Query</button>
  <button class="btn btn-success" onclick="downloadCSV()">⬇ Download CSV</button>
</div>

<div id="msgArea"></div>
<div id="loading">Loading…</div>

<div class="stats" id="statsRow" style="display:none">
  <div class="stat-card"><div class="val" id="scRows">–</div><div class="lbl">Rows returned</div></div>
  <div class="stat-card"><div class="val" id="scAvgPV">–</div><div class="lbl">Avg PV (°C)</div></div>
  <div class="stat-card"><div class="val" id="scMaxPV">–</div><div class="lbl">Max PV (°C)</div></div>
  <div class="stat-card"><div class="val" id="scMinPV">–</div><div class="lbl">Min PV (°C)</div></div>
</div>

<div class="chart-wrap" id="chartWrap" style="display:none">
  <h2>PV Temperature Trend</h2>
  <canvas id="pvChart"></canvas>
</div>
<div class="chart-wrap" id="spChartWrap" style="display:none">
  <h2>Setpoint Trend</h2>
  <canvas id="spChart"></canvas>
</div>
<div class="chart-wrap" id="outChartWrap" style="display:none">
  <h2>Output % Trend</h2>
  <canvas id="outChart"></canvas>
</div>

<div class="table-wrap" id="tableWrap" style="display:none">
  <table>
    <thead><tr>
      <th>Timestamp</th><th>Line</th><th>Zone</th>
      <th>PV (°C)</th><th>SP (°C)</th><th>Out %</th>
      <th>Current (A)</th><th>Status</th>
    </tr></thead>
    <tbody id="tBody"></tbody>
  </table>
</div>

<script>
(function() {
  // datetime-local input needs local time (YYYY-MM-DDTHH:MM)
  const fmt = d => {
    const pad = n => String(n).padStart(2,"0");
    return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate())+
           "T"+pad(d.getHours())+":"+pad(d.getMinutes());
  };
  const now = new Date(), ago = new Date(now - 86400000);
  document.getElementById("endDt").value   = fmt(now);
  document.getElementById("startDt").value = fmt(ago);
})();

let pvChart, spChart, outChart;
const PALETTE = ["#3498db","#e74c3c","#2ecc71","#f39c12","#9b59b6",
                 "#1abc9c","#e67e22","#34495e","#16a085","#c0392b"];

async function loadData() {
  setMsg("","");
  document.getElementById("loading").style.display = "block";
  ["statsRow","chartWrap","spChartWrap","outChartWrap","tableWrap"]
    .forEach(id => document.getElementById(id).style.display = "none");

  try {
    const res  = await fetch("/api/history?" + buildParams());
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    if (!data.rows?.length) { setMsg("No data found for this range.","info"); return; }
    renderStats(data.rows);
    renderCharts(data.rows);
    renderTable(data.rows);
  } catch(e) {
    setMsg("Error: " + e.message, "err");
  } finally {
    document.getElementById("loading").style.display = "none";
  }
}

function buildParams() {
  // datetime-local gives local time; convert to UTC ISO string for the API
  const toUTC = val => val ? new Date(val).toISOString() : "";
  const p = new URLSearchParams();
  p.set("start", toUTC(document.getElementById("startDt").value));
  p.set("end",   toUTC(document.getElementById("endDt").value));
  const line = document.getElementById("filterLine").value;
  const zone = document.getElementById("filterZone").value;
  if (line) p.set("line", line);
  if (zone) p.set("zone", zone);
  p.set("limit", document.getElementById("limitSel").value);
  return p.toString();
}

function downloadCSV() { window.location = "/api/history/csv?" + buildParams(); }

function renderStats(rows) {
  const pvs = rows.map(r=>r.pv).filter(v=>v!==null);
  document.getElementById("scRows").textContent  = rows.length.toLocaleString();
  document.getElementById("scAvgPV").textContent = pvs.length?(pvs.reduce((a,b)=>a+b,0)/pvs.length).toFixed(1):"–";
  document.getElementById("scMaxPV").textContent = pvs.length?Math.max(...pvs).toFixed(1):"–";
  document.getElementById("scMinPV").textContent = pvs.length?Math.min(...pvs).toFixed(1):"–";
  document.getElementById("statsRow").style.display = "flex";
}

function groupByZone(rows) {
  const map={};
  for(const r of rows){const k=`L${r.line}-Z${r.zone}`;(map[k]=map[k]||[]).push(r);}
  return map;
}
function makeDatasets(grouped,field){
  return Object.entries(grouped).map(([key,rows],i)=>({
    label:key, data:rows.map(r=>({x:r.ts,y:r[field]})),
    borderColor:PALETTE[i%PALETTE.length],
    backgroundColor:PALETTE[i%PALETTE.length]+"22",
    pointRadius:rows.length>500?0:2, borderWidth:1.5, tension:0.2
  }));
}
const CHART_OPTS = yLabel =>({
  responsive:true, animation:false,
  interaction:{mode:"index",intersect:false},
  plugins:{legend:{position:"top",labels:{boxWidth:12,font:{size:11}}},
    tooltip:{callbacks:{title:i=>new Date(i[0].parsed.x).toLocaleString()}}},
  scales:{
    x:{type:"time",time:{tooltipFormat:"yyyy-MM-dd HH:mm:ss"},ticks:{maxTicksLimit:10,font:{size:10}}},
    y:{title:{display:true,text:yLabel,font:{size:11}},ticks:{font:{size:10}}}
  }
});
function buildChart(id,datasets,yLabel){
  return new Chart(document.getElementById(id).getContext("2d"),
    {type:"line",data:{datasets},options:CHART_OPTS(yLabel)});
}
function renderCharts(rows){
  const g=groupByZone(rows);
  if(pvChart)  pvChart.destroy();
  if(spChart)  spChart.destroy();
  if(outChart) outChart.destroy();
  pvChart  = buildChart("pvChart",  makeDatasets(g,"pv"),        "PV (°C)");
  spChart  = buildChart("spChart",  makeDatasets(g,"setpoint"),  "SP (°C)");
  outChart = buildChart("outChart", makeDatasets(g,"output_pct"),"Output %");
  ["chartWrap","spChartWrap","outChartWrap"].forEach(id=>
    document.getElementById(id).style.display="block");
}

function statusBadge(s){
  if(!s)return"";
  const cls=s==="OK"?"badge-ok":s==="HEATING"?"badge-heat":s.includes("OVER")?"badge-over":"badge-notc";
  return `<span class="badge ${cls}">${s}</span>`;
}
function renderTable(rows){
  const tbody=document.getElementById("tBody");
  const shown=rows.slice(-500);
  tbody.innerHTML=shown.map(r=>`<tr>
    <td>${new Date(r.ts).toLocaleString()}</td>
    <td>${r.line}</td><td>${r.zone}</td>
    <td>${r.pv?.toFixed(1)??"–"}</td>
    <td>${r.setpoint?.toFixed(1)??"–"}</td>
    <td>${r.output_pct?.toFixed(1)??"–"}</td>
    <td>${r.current_a?.toFixed(2)??"–"}</td>
    <td>${statusBadge(r.status)}</td>
  </tr>`).join("");
  if(rows.length>500){
    const note=document.createElement("tr");
    note.innerHTML=`<td colspan="8" style="text-align:center;color:#888;padding:8px">
      Showing last 500 of ${rows.length.toLocaleString()} rows. Download CSV for full dataset.</td>`;
    tbody.appendChild(note);
  }
  document.getElementById("tableWrap").style.display="block";
}

function setMsg(text,type){
  document.getElementById("msgArea").innerHTML=
    text?`<div class="msg msg-${type}">${text}</div>`:"";
}
</script>
</body>
</html>
"""

@history_bp.route("/history")
def history_page():
    return render_template_string(HISTORY_HTML)
