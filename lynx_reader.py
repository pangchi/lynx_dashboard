#!/usr/bin/env python3
"""
BriskHeat LYNX – Read All Zones (1–128 across 4 lines) via OI Gateway (Modbus TCP)
Class-based, reusable, CLI + import friendly
"""
from __future__ import annotations

import argparse
import time
from typing import Optional, Tuple
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException


class LynxTemperatureSystem:
    REGISTERS_PER_ZONE = 24
    ZONES_PER_LINE = 32
    LINE_OFFSET = 3072
    ZONE_START_OFFSET = 1024

    def __init__(
        self,
        host: str = "192.168.200.20",
        port: int = 502,
        unit_id: int = 1,
        timeout: float = 3.0,
        connect_timeout: float = 10.0,
        lines: Tuple[int, ...] = (1, 2, 3, 4),
    ):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.lines = lines

        self.client = ModbusTcpClient(
            host=host,
            port=port,
            timeout=timeout,
            retries=1,
            reconnect_delay=1000,
        )

    @staticmethod
    def raw_to_temp(raw: int) -> float:
        """Convert raw register value (x100) to °C"""
        if raw in (0x8000, 0xFFFF):
            return float("nan")
        return raw / 100.0

    @staticmethod
    def raw_to_current(raw: int) -> float:
        """Convert raw current register (x1000) to Amps"""
        if raw == 0xFFFF:
            return float("nan")
        return raw / 1000.0

    def _calc_base_address(self, line: int, zone: int) -> int:
        """Calculate base register offset for a given line/zone"""
        if not (1 <= line <= 4 and 1 <= zone <= 32):
            raise ValueError("Line must be 1–4, Zone must be 1–32")
        return (
            (line - 1) * self.LINE_OFFSET
            + (zone - 1) * self.REGISTERS_PER_ZONE
            + self.ZONE_START_OFFSET
        )

    def connect(self) -> bool:
        """Connect to the OI Gateway"""
        print(f"Connecting to BriskHeat LYNX OI at {self.host}:{self.port}...", end="")
        if not self.client.connect():
            print(" FAILED")
            return False
        print(" OK")
        return True

    def close(self):
        """Close connection"""
        self.client.close()

    def read_all_zones(self) -> list[dict]:
        """
        Read all zones across configured lines.
        Returns a list of dicts with zone data (or empty if not connected).
        """
        if not self.client.connected:
            if not self.connect():
                return []

        results = []
        active_zones = 0
        start_time = time.time()

        print(f"{'Line':<6} {'Zone':<6} {'SP (°C)':<12} {'PV (°C)':<12} {'Output (%)':<12} {'Current (A)':<12} {'Status'}")
        print("-" * 90)

        try:
            for line in self.lines:
                for zone in range(1, self.ZONES_PER_LINE + 1):
                    base_addr = self._calc_base_address(line, zone)

                    # Read Setpoint (Holding Register)
                    sp_resp = self.client.read_holding_registers(
                        address=base_addr, count=1, device_id=self.unit_id
                    )
                    # Read PV, Output %, Current (Input Registers)
                    pv_resp = self.client.read_input_registers(
                        address=base_addr, count=3, device_id=self.unit_id
                    )

                    # Skip if both reads failed (likely unused zone)
                    if sp_resp.isError() and pv_resp.isError():
                        continue

                    sp_raw = sp_resp.registers[0] if not sp_resp.isError() else 0x8000
                    pv_raw = pv_resp.registers[0] if not pv_resp.isError() and pv_resp.registers else 0x8000
                    duty = pv_resp.registers[1] if not pv_resp.isError() and len(pv_resp.registers) > 1 else 0
                    curr_raw = pv_resp.registers[2] if not pv_resp.isError() and len(pv_resp.registers) > 2 else 0xFFFF

                    sp_temp = self.raw_to_temp(sp_raw)
                    pv_temp = self.raw_to_temp(pv_raw)
                    current = self.raw_to_current(curr_raw)

                    # Skip truly empty zones (SP and PV both invalid, or SP=0)
                    if (sp_raw == 0x8000 and pv_raw == 0x8000) or sp_temp == 0.0:
                        continue

                    # Determine status
                    status = "OK"
                    if pv_raw == 0x8000:
                        status = "NO TC"
                    elif abs(sp_temp - pv_temp) > 50 and duty >= 100:
                        status = "HEATING"
                    elif duty == 0 and pv_temp > sp_temp + 5:
                        status = "OVER TEMP"

                    # Print row
                    print(
                        f"{line:<6} {zone:<6} "
                        f"{sp_temp if sp_raw != 0x8000 else '--':<12.2f} "
                        f"{pv_temp if pv_raw != 0x8000 else '--':<12.2f} "
                        f"{duty if duty <= 100 else '--':<12} "
                        f"{current if current != float('nan') else '--':<12.2f} "
                        f"{status}"
                    )

                    results.append(
                        {
                            "line": line,
                            "zone": zone,
                            "setpoint": sp_temp if sp_raw != 0x8000 else None,
                            "pv": pv_temp if pv_raw != 0x8000 else None,
                            "output_percent": duty if duty <= 100 else None,
                            "current": current if current != float("nan") else None,
                            "status": status,
                        }
                    )
                    active_zones += 1

        finally:
            elapsed = time.time() - start_time
            print("-" * 90)
            print(f"Scan complete: {active_zones} active zone(s) found in {elapsed:.2f} seconds")

        return results


def main():
    parser = argparse.ArgumentParser(description="Read all zones from BriskHeat LYNX via OI Gateway")
    parser.add_argument("--host", default="192.168.200.20", help="OI Gateway IP address (default: 192.168.200.20)")
    parser.add_argument("--port", type=int, default=502, help="Modbus TCP port (default: 502)")
    parser.add_argument("--lines", nargs="+", type=int, choices=[1, 2, 3, 4], default=[1, 2, 3, 4],
                        help="Lines to scan (1-4), default all")
    parser.add_argument("--timeout", type=float, default=3.0, help="Modbus timeout in seconds")

    args = parser.parse_args()

    system = LynxTemperatureSystem(
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        lines=tuple(args.lines),
    )

    try:
        system.read_all_zones()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"\nError: {e}")
    finally:
        system.close()


if __name__ == "__main__":
    main()
