#!/usr/bin/env python3
"""
LYNX Set All Zones – CLI Version (reads config.ini)

Usage:
    python lynx_set_all.py 60
    python lynx_set_all.py --temp 70
    python lynx_set_all.py --config /path/to/config.ini 75

config.ini [flask] section used:
    [flask]
    port = 5000

config.ini [dashboard] section (optional, falls back to localhost):
    [dashboard]
    ip = 127.0.0.1
"""

import requests
import argparse
import sys
import time
import configparser
import os

# ========= DEFAULT CONFIG =========
DEFAULT_CONFIG = {
    "dashboard_ip": "127.0.0.1",
    "port": 5000,
    "timeout": 8,
}


def load_config(config_path: str) -> dict:
    if not config_path:
        config_path = os.path.join(os.path.dirname(__file__), "config.ini")

    if not os.path.exists(config_path):
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    cfg = configparser.ConfigParser()
    try:
        cfg.read(config_path)
    except Exception as e:
        print(f"Failed to read config file: {e}")
        sys.exit(1)

    merged = DEFAULT_CONFIG.copy()

    # Flask port (same file as dashboard uses)
    if cfg.has_option("flask", "port"):
        merged["port"] = cfg.getint("flask", "port")

    # Optional [dashboard] section for remote IP
    if cfg.has_option("dashboard", "ip"):
        merged["dashboard_ip"] = cfg.get("dashboard", "ip")

    # Optional timeout override
    if cfg.has_option("dashboard", "timeout"):
        merged["timeout"] = cfg.getfloat("dashboard", "timeout")

    return merged


def set_all_to(temp_c: float, config: dict):
    host    = config["dashboard_ip"]
    port    = config["port"]
    timeout = config.get("timeout", 8)

    url_status = f"http://{host}:{port}/api/status"
    url_set    = f"http://{host}:{port}/api/setpoint"

    print(f"Setting ALL zones to {temp_c} °C")
    print(f"Connecting to dashboard at {host}:{port} ...")

    try:
        r = requests.get(url_status, timeout=timeout)
        r.raise_for_status()
        data = r.json()

        if "zones" not in data:
            print("Invalid response format from dashboard")
            sys.exit(1)

        zones = data["zones"]
        if not zones:
            print("No zones found – is the dashboard running and connected?")
            sys.exit(1)

        print(f"Found {len(zones)} active zones")

        updates = []
        for z in zones:
            try:
                updates.append({"line": z["line"], "zone": z["zone"], "sp": temp_c})
            except KeyError:
                print(f"Skipping malformed zone: {z}")

        resp = requests.post(url_set, json={"updates": updates}, timeout=timeout)
        resp.raise_for_status()
        result = resp.json()

        if result.get("success"):
            print(f"\nSUCCESS! All {len(updates)} zones set to {temp_c} °C")
            failed = [d for d in result.get("details", []) if not d.get("success")]
            if failed:
                print(f"Warning: {len(failed)} zone(s) failed")
        else:
            print("\nFAILED")
            print(result.get("error", "Unknown error"))

    except requests.exceptions.ConnectionError:
        print(f"Cannot connect – is the dashboard running on http://{host}:{port} ?")
    except requests.exceptions.Timeout:
        print("Request timed out – network issue or dashboard too slow.")
    except requests.exceptions.HTTPError as e:
        print(f"HTTP error: {e}")
        if e.response is not None:
            print(e.response.text)
    except Exception as e:
        print(f"Unexpected error: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Set ALL LYNX zones to the same setpoint temperature",
        epilog="Example: python lynx_set_all.py 75.0 --config config.ini"
    )
    parser.add_argument(
        "temperature", type=float, nargs="?",
        help="Target temperature in °C"
    )
    parser.add_argument(
        "--temp", type=float, dest="temperature_alt",
        help="Alternative: --temp 70"
    )
    parser.add_argument(
        "--config", type=str,
        help="Path to config.ini (default: config.ini next to this script)"
    )

    args = parser.parse_args()

    temp = args.temperature or args.temperature_alt
    if temp is None:
        parser.error("Provide a temperature, e.g.: python lynx_set_all.py 60")

    config = load_config(args.config)
    set_all_to(temp, config)

    print("\nDone.")
    time.sleep(2)
