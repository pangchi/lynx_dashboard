#!/usr/bin/env python3
"""
lynx_operator.py — Operator Interface Blueprint
================================================
Provides /operator page with:
  - Timed override: run all zones at a chosen setpoint for N hours
  - Cancel override: restore previous setpoints / hand back to schedule
  - Live status banner showing active override countdown
  - API endpoints for programmatic control

Override lifecycle
------------------
1. Operator sets SP + duration → POST /api/override/start
   - Saves current setpoints from latest_data as _saved_setpoints
   - Writes new SP to all zones via _set_all_fn callback
   - Records override_end time
2. Background watchdog thread checks every 30 s:
   - If override_end passed → auto-cancel (restore saved setpoints)
3. Operator can cancel early → POST /api/override/cancel
   - Restores _saved_setpoints immediately
4. GET /api/override/status → current state for UI polling

Wiring in lynx_dashboard.py
----------------------------
    from lynx_operator import operator_bp, init_operator, override_active
    app.register_blueprint(operator_bp)

    # After system and data structures are created:
    def _op_set_all(sp):
        with data_lock:
            zones = list(latest_data)
        updates = [{"line": z["line"], "zone": z["zone"], "sp": sp}
                   for z in zones if z.get("line") and z.get("zone")]
        for u in updates:
            try:
                _perform_setpoint_write(int(u["line"]), int(u["zone"]), float(u["sp"]))
            except Exception as exc:
                log.warning("operator set_all L%s-Z%s: %s", u["line"], u["zone"], exc)

    def _op_get_zones():
        with data_lock:
            return list(latest_data)

    def _op_restore(updates):
        for u in updates:
            try:
                _perform_setpoint_write(int(u["line"]), int(u["zone"]), float(u["sp"]))
            except Exception as exc:
                log.warning("operator restore L%s-Z%s: %s", u["line"], u["zone"], exc)

    init_operator(_op_set_all, _op_get_zones, _op_restore)

Schedule suppression in lynx_schedule.py _fire():
---------------------------------------------------
    from lynx_operator import override_active
    def _fire(entry):
        if override_active():
            log.info("Schedule '%s' suppressed — operator override active", entry.get("name"))
            return
        ...
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from flask import Blueprint, jsonify, render_template, request

log = logging.getLogger("LynxOperator")

# ── shared state ──────────────────────────────────────────────────────────────
_lock              = threading.Lock()
_override_active   = False
_override_end_utc: Optional[float] = None   # epoch seconds
_override_sp: Optional[float]      = None
_saved_setpoints:  list            = []      # [{line, zone, sp}, ...]

# Injected by init_operator()
_set_all_fn   = None   # callable(sp: float) → None
_get_zones_fn = None   # callable() → list[dict]
_restore_fn   = None   # callable(updates: list) → None


def init_operator(set_all_fn, get_zones_fn, restore_fn):
    """
    Call once from lynx_dashboard.py after app creation.

    set_all_fn(sp)        – write sp to every active zone
    get_zones_fn()        – return list of current zone dicts (latest_data snapshot)
    restore_fn(updates)   – write a list of {line, zone, sp} back to hardware
    """
    global _set_all_fn, _get_zones_fn, _restore_fn
    _set_all_fn   = set_all_fn
    _get_zones_fn = get_zones_fn
    _restore_fn   = restore_fn
    _start_watchdog()
    log.info("Operator interface initialised")


# ── blueprint ─────────────────────────────────────────────────────────────────
operator_bp = Blueprint("operator", __name__, template_folder="templates")


@operator_bp.route("/operator")
def operator_page():
    return render_template("operator.html")


# ── API: status ───────────────────────────────────────────────────────────────
@operator_bp.route("/api/override/status")
def override_status():
    with _lock:
        if _override_active and _override_end_utc:
            remaining_s = max(0.0, _override_end_utc - time.time())
            hours, rem  = divmod(int(remaining_s), 3600)
            mins        = rem // 60
            return jsonify({
                "active":        True,
                "setpoint":      _override_sp,
                "end_utc":       _override_end_utc,
                "remaining_s":   remaining_s,
                "remaining_str": f"{hours}h {mins:02d}m",
            })
        return jsonify({"active": False})


# ── API: start override ───────────────────────────────────────────────────────
@operator_bp.route("/api/override/start", methods=["POST"])
def override_start():
    global _override_active, _override_end_utc, _override_sp, _saved_setpoints

    j = request.get_json(force=True) or {}
    try:
        sp    = float(j["setpoint"])
        hours = float(j["hours"])
    except (KeyError, ValueError, TypeError) as exc:
        return jsonify({"success": False, "error": f"Bad params: {exc}"}), 400

    if not (0.5 <= hours <= 24):
        return jsonify({"success": False, "error": "hours must be 0.5–24"}), 400
    if not (0 < sp <= 400):
        return jsonify({"success": False, "error": "setpoint out of range (1–400 °C)"}), 400

    # Save current setpoints so we can restore them on cancel/expiry
    try:
        zones = _get_zones_fn()
        saved = [
            {"line": z["line"], "zone": z["zone"], "sp": z.get("setpoint") or sp}
            for z in zones
            if z.get("setpoint") is not None and z.get("line") and z.get("zone")
        ]
    except Exception as exc:
        log.warning("Could not read current setpoints for save: %s", exc)
        saved = []

    # Apply override SP to all zones
    try:
        _set_all_fn(sp)
    except Exception as exc:
        return jsonify({"success": False, "error": f"Setpoint write failed: {exc}"}), 500

    end_time = time.time() + hours * 3600
    with _lock:
        _saved_setpoints  = saved
        _override_sp      = sp
        _override_end_utc = end_time
        _override_active  = True

    end_local = datetime.fromtimestamp(end_time).strftime("%H:%M %d-%b")
    log.info("Override started: SP=%.1f°C for %.1fh, ends %s, saved %d setpoints",
             sp, hours, end_local, len(saved))
    return jsonify({"success": True, "setpoint": sp, "hours": hours, "ends": end_local})


# ── API: cancel override ──────────────────────────────────────────────────────
@operator_bp.route("/api/override/cancel", methods=["POST"])
def override_cancel():
    _do_cancel(reason="operator")
    return jsonify({"success": True})


# ── internal helpers ──────────────────────────────────────────────────────────
def _do_cancel(reason: str = "auto"):
    global _override_active, _override_end_utc, _override_sp

    with _lock:
        if not _override_active:
            return
        saved = list(_saved_setpoints)
        _override_active  = False
        _override_end_utc = None
        _override_sp      = None

    log.info("Override cancelled (%s); restoring %d setpoints", reason, len(saved))
    if saved and _restore_fn:
        try:
            _restore_fn(saved)
        except Exception as exc:
            log.warning("Restore failed: %s", exc)


def _start_watchdog():
    """Daemon thread — auto-cancels override when the timer expires."""
    def _loop():
        while True:
            time.sleep(30)
            with _lock:
                active = _override_active
                end    = _override_end_utc
            if active and end and time.time() >= end:
                log.info("Override timer expired — auto-cancelling")
                _do_cancel(reason="expired")

    t = threading.Thread(target=_loop, daemon=True, name="operator-watchdog")
    t.start()
    log.debug("Operator watchdog started")


# ── public accessor for schedule suppression ──────────────────────────────────
def override_active() -> bool:
    """Return True while an operator override is in effect.
    Called by lynx_schedule._fire() to suppress scheduled setpoint changes."""
    with _lock:
        return _override_active
