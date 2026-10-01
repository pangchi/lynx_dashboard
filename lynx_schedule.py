#!/usr/bin/env python3
"""
lynx_schedule.py
----------------
Setpoint scheduling for BriskHeat LYNX Dashboard.

Schedules are stored in schedule.json next to this file.
A background thread checks every 60 s and fires any due entries.

Each schedule entry:
{
  "id":        "uuid4 string",
  "name":      "Night setback",
  "enabled":   true,
  "zones":     [{"line":1,"zone":1}, ...],   // empty = ALL zones
  "setpoint":  60.0,
  "days":      [0,1,2,3,4],                 // 0=Mon … 6=Sun
  "time":      "22:00"                       // HH:MM local time
}

Mount in lynx_dashboard.py:
----------------------------
from lynx_schedule import schedule_bp, start_scheduler
app.register_blueprint(schedule_bp)
start_scheduler(get_zones_fn, set_sp_fn)
"""

import json
import os
import uuid
import logging
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, render_template

log = logging.getLogger("lynx_schedule")

schedule_bp = Blueprint("schedule", __name__, template_folder="templates")

_SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "schedule.json")
_lock          = threading.Lock()
_get_zones     = None   # injected by start_scheduler
_set_sp        = None   # injected by start_scheduler
_tz_name       = "UTC"  # updated by settings


# ── persistence ───────────────────────────────────────────────────────────────

def _load():
    if not os.path.exists(_SCHEDULE_FILE):
        return []
    try:
        with open(_SCHEDULE_FILE) as f:
            return json.load(f)
    except Exception as e:
        log.error("Failed to load schedule: %s", e)
        return []


def _save(entries):
    try:
        with open(_SCHEDULE_FILE, "w") as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        log.error("Failed to save schedule: %s", e)


# ── API ───────────────────────────────────────────────────────────────────────

@schedule_bp.route("/api/schedule", methods=["GET"])
def api_schedule_get():
    with _lock:
        return jsonify(_load())


@schedule_bp.route("/api/schedule", methods=["POST"])
def api_schedule_post():
    """Create or update a schedule entry. Omit 'id' to create new."""
    try:
        j = request.get_json()
        _validate(j)
        with _lock:
            entries = _load()
            if j.get("id"):
                entries = [j if e["id"] == j["id"] else e for e in entries]
                if not any(e["id"] == j["id"] for e in entries):
                    return jsonify({"error": "id not found"}), 404
            else:
                j["id"] = str(uuid.uuid4())
                entries.append(j)
            _save(entries)
        return jsonify({"success": True, "id": j["id"]})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@schedule_bp.route("/api/schedule/<entry_id>", methods=["DELETE"])
def api_schedule_delete(entry_id):
    with _lock:
        entries = _load()
        new     = [e for e in entries if e["id"] != entry_id]
        if len(new) == len(entries):
            return jsonify({"error": "not found"}), 404
        _save(new)
    return jsonify({"success": True})


@schedule_bp.route("/api/schedule/<entry_id>/toggle", methods=["POST"])
def api_schedule_toggle(entry_id):
    with _lock:
        entries = _load()
        for e in entries:
            if e["id"] == entry_id:
                e["enabled"] = not e.get("enabled", True)
                _save(entries)
                return jsonify({"success": True, "enabled": e["enabled"]})
    return jsonify({"error": "not found"}), 404


def _validate(j):
    if not isinstance(j.get("setpoint"), (int, float)):
        raise ValueError("setpoint must be a number")
    if not isinstance(j.get("days"), list) or not j["days"]:
        raise ValueError("days must be a non-empty list (0=Mon … 6=Sun)")
    t = j.get("time_utc") or j.get("time")
    if not t or len(t) != 5:
        raise ValueError("time_utc must be HH:MM (24h)")
    for d in j["days"]:
        if d not in range(7):
            raise ValueError("days values must be 0–6")


# ── scheduler thread ──────────────────────────────────────────────────────────

def start_scheduler(get_zones_fn, set_sp_fn, tz_name="UTC"):
    """
    Call once at startup.
    get_zones_fn() → list of zone dicts (from latest_data)
    set_sp_fn(line, zone, sp) → None (calls _perform_setpoint_write)
    tz_name → IANA timezone string for local time comparison
    """
    global _get_zones, _set_sp, _tz_name
    _get_zones = get_zones_fn
    _set_sp    = set_sp_fn
    _tz_name   = tz_name
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="LynxScheduler")
    t.start()
    log.info("Scheduler started (timezone: %s)", tz_name)


def update_scheduler_tz(tz_name):
    global _tz_name
    _tz_name = tz_name


_last_fired = {}   # entry_id -> "YYYY-MM-DD HH:MM" of last fire


def _scheduler_loop():
    while True:
        try:
            _check_schedules()
        except Exception as e:
            log.error("Scheduler error: %s", e)
        time.sleep(30)


def _check_schedules():
    # Compare in UTC — time is stored as UTC HH:MM in schedule.json
    now_utc  = datetime.now(timezone.utc)

    # For day-of-week we still use local time so Mon-Fri makes sense to the user
    try:
        import zoneinfo
        tz_local = zoneinfo.ZoneInfo(_tz_name)
    except Exception:
        tz_local = timezone.utc

    now_local   = datetime.now(tz_local)
    now_day     = now_local.weekday()       # 0=Mon, local weekday
    now_hhmm    = now_utc.strftime("%H:%M") # compare UTC time to stored UTC time

    with _lock:
        entries = _load()

    for entry in entries:
        if not entry.get("enabled", True):
            continue

        # day_offset = how many days the UTC time is ahead of local time
        # e.g. local Mon 23:00 UTC+8 → UTC Sun 15:00 → day_offset = -1
        # Browser computes this and stores it alongside time_utc.
        day_offset = entry.get("day_offset", 0)

        # The local days the user picked; adjust for UTC day boundary
        local_days = entry.get("days", [])
        utc_days   = [(d + day_offset) % 7 for d in local_days]

        if now_utc.weekday() not in utc_days:
            continue

        stored = entry.get("time_utc") or entry.get("time")
        if stored != now_hhmm:
            continue

        fire_key = f"{now_utc.strftime('%Y-%m-%d')} {now_hhmm}"
        if _last_fired.get(entry["id"]) == fire_key:
            continue   # already fired this minute

        _fire(entry)
        _last_fired[entry["id"]] = fire_key


def _fire(entry):
    sp    = float(entry["setpoint"])
    name  = entry.get("name", entry["id"])
    zones = entry.get("zones", [])

    if not zones:
        # Apply to all currently active zones
        if _get_zones:
            zones = [{"line": z["line"], "zone": z["zone"]}
                     for z in (_get_zones() or [])]

    if not zones:
        log.warning("Schedule '%s': no zones to apply (scanner may not have data yet)", name)
        return

    success, failed = 0, 0
    for z in zones:
        try:
            _set_sp(int(z["line"]), int(z["zone"]), sp)
            success += 1
        except Exception as e:
            log.error("Schedule '%s': failed L%s-Z%s: %s",
                      name, z["line"], z["zone"], e)
            failed += 1

    log.info("Schedule '%s' fired → SP=%.1f°C | %d ok, %d failed",
             name, sp, success, failed)


# ── page ──────────────────────────────────────────────────────────────────────

@schedule_bp.route("/schedule")
def schedule_page():
    return render_template("schedule.html", tz_name=_tz_name)
