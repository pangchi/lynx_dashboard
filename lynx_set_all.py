#!/usr/bin/env python3
"""
LYNX Set All Zones – CLI Version (with external config)

Usage:
    python lynx_set_all.py 60
    python lynx_set_all.py --temp 70
    python lynx_set_all.py --config config.json 75

Config file example (config.json):
{
    "dashboard_ip": "127.0.0.1",
    "port": 5000,
    "timeout": 8
}
"""

import requests
import argparse
import sys
import time
import json
import os

# ========= DEFAULT CONFIG =========
DEFAULT_CONFIG = {
    "dashboard_ip": "127.0.0.1",
    "port": 5000,
    "timeout": 8
}

def load_config(config_path: str):
    if not config_path:
        #return DEFAULT_CONFIG
        config_path = "config.json"

    if not os.path.exists(config_path):
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception as e:
        print(f"Failed to read config file: {e}")
        sys.exit(1)

    # Merge with defaults
    merged = DEFAULT_CONFIG.copy()
    merged.update(config)
    return merged


def set_all_to(temp_c: float, config: dict):
    host = config["dashboard_ip"]
    port = config["port"]
    timeout = config.get("timeout", 8)

    url_status = f"http://{host}:{port}/api/status"
    url_set = f"http://{host}:{port}/api/setpoint"

    print(f"Setting ALL zones to {temp_c} °C")
    print(f"Connecting to dashboard at {host}:{port} ...")

    try:
        # Get current zone list
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

        # Confirm action
        '''
        confirm = input(f"Confirm set ALL zones to {temp_c} °C? (y/N): ")
        if confirm.lower() != "y":
            print("Aborted.")
            sys.exit(0)
        '''
        
        # Build batch payload safely
        updates = []
        for z in zones:
            try:
                updates.append({
                    "line": z["line"],
                    "zone": z["zone"],
                    "sp": temp_c
                })
            except KeyError:
                print(f"Skipping malformed zone: {z}")

        # Send batch setpoint command
        resp = requests.post(url_set, json={"updates": updates}, timeout=timeout)
        resp.raise_for_status()
        result = resp.json()

        if result.get("success"):
            print("\nSUCCESS!")
            print(f"All {len(updates)} zones are now set to {temp_c} °C")

            details = result.get("details", [])
            failed = [d for d in details if not d.get("success")]

            if failed:
                print(f"Warning: {len(failed)} zones failed")
        else:
            print("\nFAILED")
            print(result.get("error", "Unknown error"))

    except requests.exceptions.ConnectionError:
        print("Cannot connect to dashboard.")
        print(f"   → Is the dashboard running on http://{host}:{port} ?")
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
        epilog="Example: python lynx_set_all.py 75.0 --config config.json"
    )

    parser.add_argument(
        "temperature",
        type=float,
        nargs="?",
        help="Target temperature in °C"
    )

    parser.add_argument(
        "--temp",
        type=float,
        dest="temperature_alt",
        help="Alternative way: --temp 70"
    )

    parser.add_argument(
        "--config",
        type=str,
        help="Path to config JSON file"
    )

    args = parser.parse_args()

    temp = args.temperature or args.temperature_alt
    if temp is None:
        parser.error("You must provide a temperature (e.g. 60) or use --temp 60")

    config = load_config(args.config)

    set_all_to(temp, config)

    print("\nDone.")
    time.sleep(2)
