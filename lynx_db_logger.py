#!/usr/bin/env python3
"""
lynx_db_logger.py
-----------------
PostgreSQL logger for BriskHeat LYNX dashboard.
• Auto-creates the database and user on first run if they don't exist.
• Auto-creates the table and indexes on first connect.
• Stores zone PV / SP / output / current / status every `interval_sec` seconds.

Schema created automatically:

  lynx_zone_log (id, ts, line, zone, pv, setpoint, output_pct, current_a, status)

Usage (in lynx_dashboard.py)
-----------------------------
from lynx_db_logger import LynxDBLogger

logger = LynxDBLogger(
    host="localhost", port=5432,
    dbname="lynx", user="lynx_user", password="secret",
    interval_sec=60,
    admin_user="postgres", admin_password=""   # only needed for first-time DB creation
)
logger.start()
logger.submit(zone_list)   # non-blocking
"""

import os
import shutil
import threading
import time
import queue
import logging
from datetime import datetime, timezone

log = logging.getLogger("LynxDBLogger")


# ── DB / schema bootstrap ─────────────────────────────────────────────────────

def _import_psycopg2():
    try:
        import psycopg2
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip",
                               "install", "--quiet", "psycopg2-binary"])
        import psycopg2
    return psycopg2


def ensure_db_exists(host, port, dbname, user, password,
                     admin_user="postgres", admin_password=""):
    """
    Connect to the 'postgres' maintenance database as admin and:
      1. Create the role/user if it doesn't exist.
      2. Create the target database if it doesn't exist.

    Safe to call every startup – all statements are no-ops if already present.
    Logs a clear message for each action taken.
    """
    psycopg2 = _import_psycopg2()

    # Connect to the maintenance DB (always exists).
    # If admin_password is blank, omit it entirely so PostgreSQL falls back
    # to peer/trust auth (the common default for the local postgres superuser).
    try:
        connect_kwargs = dict(host=host, port=port, dbname="postgres", user=admin_user)
        if admin_password:
            connect_kwargs["password"] = admin_password
        admin_conn = psycopg2.connect(**connect_kwargs)
    except Exception as exc:
        log.error("Cannot connect to PostgreSQL as admin (%s@%s:%s/postgres): %s",
                  admin_user, host, port, exc)
        raise

    admin_conn.autocommit = True   # CREATE DATABASE requires no open transaction

    try:
        with admin_conn.cursor() as cur:
            # ── 1. Create user/role if missing ──────────────────────────
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,))
            if not cur.fetchone():
                # Can't use %s for identifiers; use a safe manual quote
                cur.execute(
                    f"CREATE USER {_quote_ident(user)} WITH PASSWORD %s",
                    (password,)
                )
                log.info("Created PostgreSQL user: %s", user)
            else:
                log.debug("PostgreSQL user already exists: %s", user)

            # ── 2. Create database if missing ────────────────────────────
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if not cur.fetchone():
                cur.execute(
                    f"CREATE DATABASE {_quote_ident(dbname)} OWNER {_quote_ident(user)}"
                )
                log.info("Created PostgreSQL database: %s (owner: %s)", dbname, user)
            else:
                log.debug("PostgreSQL database already exists: %s", dbname)

            # ── 3. Grant DB-level privileges (idempotent) ───────────────
            cur.execute(
                f"GRANT ALL PRIVILEGES ON DATABASE {_quote_ident(dbname)}"
                f" TO {_quote_ident(user)}"
            )

    finally:
        admin_conn.close()

    # ── 4. Grant schema privileges via a separate connection to the target DB ──
    # PostgreSQL 15+ revoked CREATE on the public schema by default.
    # This must run connected to the target DB, not the maintenance DB.
    try:
        schema_kwargs = dict(host=host, port=port, dbname=dbname, user=admin_user)
        if admin_password:
            schema_kwargs["password"] = admin_password
        schema_conn = psycopg2.connect(**schema_kwargs)
        schema_conn.autocommit = True
        with schema_conn.cursor() as cur:
            cur.execute(
                f"GRANT ALL ON SCHEMA public TO {_quote_ident(user)}"
            )
        schema_conn.close()
        log.debug("Schema privileges granted to %s on %s.public", user, dbname)
    except Exception as exc:
        log.warning("Schema grant skipped (may already be set): %s", exc)

    log.info("DB bootstrap complete → %s@%s:%s/%s", user, host, port, dbname)


def _quote_ident(name: str) -> str:
    """Minimal safe identifier quoting (double-quote, escape internal quotes)."""
    return '"' + name.replace('"', '""') + '"'


# ── Logger class ──────────────────────────────────────────────────────────────

class LynxDBLogger:
    """
    Thread-safe PostgreSQL logger.

    Accepts zone snapshots via submit() (non-blocking).
    A dedicated writer thread batches inserts every `interval_sec` seconds,
    so DB latency never slows the Modbus scanner.
    """

    _DDL = [
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

    _INSERT = """
    INSERT INTO lynx_zone_log
        (ts, line, zone, pv, setpoint, output_pct, current_a, status)
    VALUES
        (%(ts)s, %(line)s, %(zone)s, %(pv)s, %(setpoint)s,
         %(output_pct)s, %(current_a)s, %(status)s)
    """

    def __init__(self, host="localhost", port=5432, dbname="lynx",
                 user="postgres", password="",
                 interval_sec=60,
                 admin_user="postgres", admin_password="",
                 purge_threshold=80.0, purge_keep_pct=60.0,
                 mount_path=None):
        self._dsn = dict(host=host, port=port, dbname=dbname,
                         user=user, password=password)
        self._interval        = interval_sec
        self._admin_user      = admin_user
        self._admin_pass      = admin_password
        self._purge_threshold = purge_threshold   # % disk usage that triggers purge
        self._purge_keep_pct  = purge_keep_pct    # % of rows to keep after purge
        self._mount_path      = mount_path or os.path.dirname(os.path.abspath(__file__))
        self._queue           = queue.Queue()
        self._thread          = None
        self._running         = False

    # ── public API ────────────────────────────────────────────────────────────

    def start(self):
        """Bootstrap the DB then start the background writer thread."""
        # Run DB/table creation synchronously before the first write attempt
        try:
            ensure_db_exists(
                host=self._dsn["host"],
                port=self._dsn["port"],
                dbname=self._dsn["dbname"],
                user=self._dsn["user"],
                password=self._dsn["password"],
                admin_user=self._admin_user,
                admin_password=self._admin_pass,
            )
        except Exception as exc:
            log.warning("DB bootstrap failed (will retry on first write): %s", exc)

        self._running = True
        self._thread  = threading.Thread(target=self._writer_loop,
                                         daemon=True, name="LynxDBLogger")
        self._thread.start()
        log.info("LynxDBLogger started (interval=%ds)", self._interval)

    def stop(self):
        self._running = False

    def submit(self, zone_list):
        """
        Non-blocking. Call from background_scanner() after each poll.
        zone_list: list of dicts from LynxTemperatureSystem.read_all_zones()
        """
        if zone_list:
            self._queue.put((datetime.now(timezone.utc), list(zone_list)))

    # ── internals ─────────────────────────────────────────────────────────────

    def _connect(self):
        psycopg2 = _import_psycopg2()
        dsn = {k: v for k, v in self._dsn.items() if k != "password" or v}
        conn = psycopg2.connect(**dsn)
        with conn.cursor() as cur:
            for stmt in self._DDL:
                cur.execute(stmt)
        conn.commit()
        return conn

    def _check_and_purge(self, conn):
        """
        Delete the oldest rows when disk usage on the app mount point
        exceeds purge_threshold (default 80%).
        Keeps the newest purge_keep_pct % of rows (default 60%).
        Runs at most once per write cycle so overhead is negligible.
        """
        if conn is None or conn.closed:
            return

        usage = shutil.disk_usage(self._mount_path)
        pct   = usage.used / usage.total * 100

        if pct < self._purge_threshold:
            return   # nothing to do

        log.warning(
            "Disk %.1f%% full (mount: %s) – purging oldest rows "
            "(keeping newest %.0f%%)...",
            pct, self._mount_path, self._purge_keep_pct
        )

        keep_fraction = self._purge_keep_pct / 100.0
        with conn.cursor() as cur:
            # Count total rows
            cur.execute("SELECT COUNT(*) FROM lynx_zone_log")
            total = cur.fetchone()[0]
            if total == 0:
                return
            keep   = max(1, int(total * keep_fraction))
            delete = total - keep
            if delete <= 0:
                return
            # Delete the oldest `delete` rows by ctid for efficiency
            cur.execute("""
                DELETE FROM lynx_zone_log
                WHERE id IN (
                    SELECT id FROM lynx_zone_log
                    ORDER BY ts ASC
                    LIMIT %s
                )
            """, (delete,))
        conn.commit()
        log.info("Purged %d oldest rows (kept %d). Disk was %.1f%% full.",
                 delete, keep, pct)

    def _writer_loop(self):
        conn       = None
        last_write = 0.0

        while self._running:
            now = time.monotonic()
            if now - last_write < self._interval:
                time.sleep(1)
                continue

            # Drain queue; keep only the most-recent snapshot
            snapshot = None
            while True:
                try:
                    snapshot = self._queue.get_nowait()
                except queue.Empty:
                    break

            if snapshot is None:
                last_write = now
                continue

            ts, zones = snapshot
            rows = [
                {
                    "ts":         ts,
                    "line":       z.get("line"),
                    "zone":       z.get("zone"),
                    "pv":         z.get("pv"),
                    "setpoint":   z.get("setpoint"),
                    "output_pct": z.get("output_percent"),
                    "current_a":  z.get("current"),
                    "status":     z.get("status"),
                }
                for z in zones
            ]

            try:
                if conn is None or conn.closed:
                    conn = self._connect()

                with conn.cursor() as cur:
                    cur.executemany(self._INSERT, rows)
                conn.commit()
                log.info("Logged %d zone rows at %s", len(rows),
                         ts.strftime("%H:%M:%S"))

            except Exception as exc:
                log.error("DB write failed: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                conn = None   # force reconnect next cycle

            last_write = time.monotonic()

            # Check disk usage and purge oldest rows if over threshold
            try:
                self._check_and_purge(conn)
            except Exception as exc:
                log.warning("Purge check failed: %s", exc)
