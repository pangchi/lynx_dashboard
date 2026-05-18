#!/usr/bin/env python3
"""
test_connection.py
------------------
Quick connectivity test for both Modbus OI Gateway and PostgreSQL.
Reads all settings from config.ini in the same folder.
Auto-creates the database and user if they don't exist yet.

Usage:
    python test_connection.py
"""

import configparser
import os
import sys

# ── Load config.ini ───────────────────────────────────────────────────────────
CFG_FILE = os.path.join(os.path.dirname(__file__), "config.ini")
cfg = configparser.ConfigParser()
if not cfg.read(CFG_FILE):
    print(f"ERROR: config.ini not found at {CFG_FILE}")
    sys.exit(1)

OI_HOST        = cfg.get    ("modbus",   "host")
OI_PORT        = cfg.getint ("modbus",   "port")
MODBUS_TIMEOUT = cfg.getfloat("modbus",  "timeout")

DB_HOST        = cfg.get    ("database", "host")
DB_PORT        = cfg.getint ("database", "port")
DB_NAME        = cfg.get    ("database", "name")
DB_USER        = cfg.get    ("database", "user")
DB_PASSWORD    = cfg.get    ("database", "password")
DB_ADMIN_USER  = cfg.get    ("database", "admin_user",     fallback="postgres")
DB_ADMIN_PASS  = cfg.get    ("database", "admin_password", fallback="")

print("=" * 50)
print("  LYNX Connectivity Test")
print("=" * 50)

# ── psycopg2 auto-install ─────────────────────────────────────────────────────
try:
    import psycopg2
except ImportError:
    import subprocess
    print("\nInstalling psycopg2-binary...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "psycopg2-binary"])
    import psycopg2

# ── Test 1: Modbus OI Gateway ─────────────────────────────────────────────────
print(f"\n[1] Modbus OI Gateway → {OI_HOST}:{OI_PORT}")
try:
    from lynx_reader import LynxTemperatureSystem
    system = LynxTemperatureSystem(host=OI_HOST, port=OI_PORT, timeout=MODBUS_TIMEOUT)
    if system.connect():
        print("    ✓ Connection SUCCESSFUL")
        system.close()
    else:
        print("    ✗ Cannot reach OI Gateway – check IP / firewall / cabling")
except Exception as e:
    print(f"    ✗ Error: {e}")

# ── Test 2: PostgreSQL – bootstrap then verify ────────────────────────────────
print(f"\n[2] PostgreSQL → {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")

# Step 2a: ensure DB + user exist (connects via admin account to 'postgres' DB)
print(f"    Checking DB via admin ({DB_ADMIN_USER}@{DB_HOST}:{DB_PORT}/postgres)...")
try:
    from lynx_db_logger import ensure_db_exists
    ensure_db_exists(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
        admin_user=DB_ADMIN_USER, admin_password=DB_ADMIN_PASS,
    )
    print(f"    ✓ Database '{DB_NAME}' and user '{DB_USER}' are ready")
except Exception as e:
    print(f"    ✗ Bootstrap failed: {e}")
    print(f"      → Is PostgreSQL running on {DB_HOST}:{DB_PORT}?")
    print(f"      → Does admin user '{DB_ADMIN_USER}' exist with correct password?")
    print(f"      → Check [database] admin_user / admin_password in config.ini")
    print("\n" + "=" * 50)
    sys.exit(1)

# Step 2b: connect as the app user and verify table
print(f"    Connecting as '{DB_USER}'...")
try:
    kwargs = dict(host=DB_HOST, port=DB_PORT,
                  dbname=DB_NAME, user=DB_USER, connect_timeout=5)
    if DB_PASSWORD:
        kwargs["password"] = DB_PASSWORD
    conn = psycopg2.connect(**kwargs)
    with conn.cursor() as cur:
        cur.execute("SELECT version();")
        version = cur.fetchone()[0].split(",")[0]
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'lynx_zone_log'
        """)
        table_exists = cur.fetchone()[0] == 1
    conn.close()
    print(f"    ✓ Connection SUCCESSFUL ({version})")
    if table_exists:
        print("    ✓ Table lynx_zone_log exists")
    else:
        print("    ⚠ Table lynx_zone_log not yet created – it will be created on first dashboard run")
except psycopg2.OperationalError as e:
    print(f"    ✗ Auth failed: {e}".strip())
    print(f"      → Check [database] user / password in config.ini")
    print(f"      → Or run: ALTER USER {DB_USER} WITH PASSWORD 'yourpassword';")
except Exception as e:
    print(f"    ✗ Error: {e}")

print("\n" + "=" * 50)
