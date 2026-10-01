#!/usr/bin/env python3
"""
BriskHeat LYNX / Centipede 2 – Modbus Reader
=============================================
Supports:
  • BriskHeat LYNX via Modbus TCP (OI Gateway) or RTU (serial)
  • BriskHeat Centipede 2 via Modbus RTU RS-232 (19200 8N1)

Device types
  device_type = "lynx"       (default) – LYNX register map
  device_type = "centipede2"           – Centipede 2 register map

Centipede 2 serial spec (Appendix 2, manual 725437):
  Baud: 19200 (fixed)  |  8 data bits  |  1 stop bit  |  No parity  |  RS-232

Centipede 2 register map (Appendix B, manual 2506264):
  Addr  2 : Zone Status        (R/W)
  Addr  3 : Sampled Temp       (R,   °C × 100)
  Addr  4 : Controller Config  (R/W)
  Addr  5 : Setpoint           (R/W, °C × 100)
  Addr  6 : Hi Limit alarm     (R/W, °C × 100)
  Addr  7 : Lo Limit alarm     (R/W, °C × 100)
  Addr 16 : PWM % (Output)     (R)

Centipede 2 unit_id = zone number (1-based, matches module address)

Zone dict returned by read_all_zones():
  {
    "line":           int,
    "zone":           int,
    "pv":             float | None,   # °C
    "setpoint":       float | None,   # °C
    "output_percent": float | None,   # %
    "current":        float | None,   # A (LYNX only)
    "status":         str,
  }
"""
from __future__ import annotations

import argparse
import logging
import math
import time
from typing import Optional, Tuple

log = logging.getLogger("LynxReader")

# ── Centipede 2 constants ─────────────────────────────────────────────────────
# The C2MOD-OI-7 is a single Modbus RTU slave. Zone data is accessed indirectly:
#   1. Write zone number to OI reg 3 (Module Focus Address)
#   2. Write zone register number to OI reg 4 (Jacket Command)
#   3. Read result from OI reg 5 (Jacket Poll Response)
#   OR read zone regs 2–16 in bulk via OI reg 4 with count > 1
#
# OI Registers (Appendix A of manual):
C2_BAUD              = 19200
C2_BYTESIZE          = 8
C2_PARITY            = "N"
C2_STOPBITS          = 1
C2_ZONES_PER_STRING  = 16   # zones per line in dashboard display
C2_MAX_UNITS         = 128  # max zones to scan
C2_OI_FOCUS_REG      = 3    # OI reg 3: write zone number to select zone
C2_OI_CMD_REG        = 4    # OI reg 4: write zone reg addr, read zone reg value
C2_OI_POLL_REG       = 5    # OI reg 5: poll response
# Zone Registers (Appendix B of manual) — accessed after setting focus:
C2_ZONE_STATUS       = 2    # zone status word (hex)
C2_ZONE_TEMP         = 3    # PV temperature °C × 100
C2_ZONE_SETPOINT     = 5    # setpoint °C × 100
C2_ZONE_PWM          = 16   # PWM duty cycle %

# Auto-detect baud order (Centipede 2 is always 19200 so it will succeed first)
AUTO_BAUD_ORDER  = [19200, 9600, 38400, 57600, 115200]

# ── pymodbus helpers ──────────────────────────────────────────────────────────

def _pymodbus_version():
    try:
        import pymodbus as _pm
        return tuple(int(x) for x in _pm.__version__.split(".")[:2])
    except Exception:
        return (3, 0)


def _make_tcp_client(host, port, timeout):
    ver = _pymodbus_version()
    try:
        from pymodbus.client import ModbusTcpClient
    except ImportError:
        from pymodbus.client.sync import ModbusTcpClient
    kwargs = dict(host=host, port=port, timeout=timeout)
    if ver >= (3, 0):
        kwargs.update(retries=1, reconnect_delay=1.0)
    else:
        kwargs.update(retries=1)
    return ModbusTcpClient(**kwargs)


def _make_rtu_client(serial_port, baudrate, bytesize, parity, stopbits, timeout):
    ver = _pymodbus_version()
    try:
        from pymodbus.client import ModbusSerialClient
    except ImportError:
        from pymodbus.client.sync import ModbusSerialClient

    if ver >= (3, 0):
        # pymodbus 3.x: framing method is a positional-style kwarg
        kwargs = dict(
            port=serial_port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout,
            retries=1,
        )
    else:
        # pymodbus 2.x: method="rtu" selects RTU framing explicitly
        kwargs = dict(
            method="rtu",
            port=serial_port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout,
        )
    return ModbusSerialClient(**kwargs)


# ── Auto baud detection ───────────────────────────────────────────────────────

def detect_baud(serial_port: str,
                bytesize: int = 8,
                parity:   str = "N",
                stopbits: int = 1,
                timeout:  float = 1.5,
                unit_id:  int = 1,
                try_bauds = None) -> Optional[int]:
    """
    Try each baud rate in order and return the first one that gets a valid
    Modbus response to a read_holding_registers(addr=0, count=1, unit=unit_id).
    Returns the detected baud rate, or None if none respond.
    """
    if try_bauds is None:
        try_bauds = AUTO_BAUD_ORDER

    for baud in try_bauds:
        log.info("Auto-detect baud: trying %d on %s …", baud, serial_port)
        client = _make_rtu_client(serial_port, baud, bytesize, parity, stopbits, timeout)
        try:
            if not client.connect():
                continue
            resp = client.read_input_registers(
                address=0, count=1,
                **_slave_kwarg(unit_id)
            )
            if resp and not resp.isError():
                log.info("Auto-detect baud: found %d baud", baud)
                return baud
        except Exception as e:
            log.debug("Auto-detect baud %d: %s", baud, e)
        finally:
            try:
                client.close()
            except Exception:
                pass

    log.warning("Auto-detect baud: no response on %s", serial_port)
    return None


def _slave_kwarg(unit_id: int) -> dict:
    """Return the correct keyword for the Modbus slave/unit ID per pymodbus version.
    pymodbus <3.0  → unit=
    pymodbus 3.0–3.5 → slave=
    pymodbus 3.6+  → device_id=
    Detected at runtime by inspecting the actual method signature.
    """
    try:
        from pymodbus.client import ModbusTcpClient
        import inspect
        sig = inspect.signature(ModbusTcpClient.read_holding_registers)
        if "device_id" in sig.parameters:
            return {"device_id": unit_id}
        if "slave" in sig.parameters:
            return {"slave": unit_id}
        return {"unit": unit_id}
    except Exception:
        ver = _pymodbus_version()
        if ver >= (3, 6):
            return {"device_id": unit_id}
        if ver >= (3, 0):
            return {"slave": unit_id}
        return {"unit": unit_id}


# ── Main class ────────────────────────────────────────────────────────────────

class LynxTemperatureSystem:
    # LYNX register map constants
    REGISTERS_PER_ZONE = 24
    ZONES_PER_LINE     = 32
    LINE_OFFSET        = 3072
    ZONE_START_OFFSET  = 1024

    def __init__(
        self,
        # TCP params
        host:        str   = "192.168.200.20",
        port:        int   = 502,
        # Serial / RTU params
        use_serial:  bool  = False,
        serial_port: str   = "/dev/ttyUSB0",
        baudrate:    int   = 9600,
        bytesize:    int   = 8,
        parity:      str   = "N",
        stopbits:    int   = 1,
        # Device type
        device_type:  str = "lynx",   # "lynx" or "centipede2"
        c2_max_units:      int = 128,   # max unit IDs to scan for Centipede 2
        c2_miss_threshold: int = 10,    # stop after this many consecutive non-responding units
        c2_password:       str = "briskheat",  # LUI login password for C2MOD-OI-7
        c2_dump_secs:      int = 1,            # seconds for Dump: command (1s is enough; OI sends all zones in <1s)
        # Common
        unit_id:         int   = 1,
        timeout:         float = 3.0,
        connect_timeout: float = 10.0,
        lines:           Tuple[int, ...] = (1, 2, 3, 4),
    ):
        self.host            = host
        self.port            = port
        self.use_serial      = use_serial
        self.serial_port     = serial_port
        self.baudrate        = baudrate
        self.bytesize        = bytesize
        self.parity          = parity
        self.stopbits        = stopbits
        self.device_type     = device_type.lower().strip()
        self.c2_max_units       = c2_max_units
        self.c2_miss_threshold  = c2_miss_threshold
        self.c2_password        = c2_password
        self.c2_dump_secs       = c2_dump_secs
        self.unit_id         = unit_id
        self.timeout         = timeout
        self.connect_timeout = connect_timeout
        self.lines           = lines

        # Centipede 2 overrides serial params to spec values
        if self.device_type == "centipede2":
            self.baudrate  = C2_BAUD
            self.bytesize  = C2_BYTESIZE
            self.parity    = C2_PARITY
            self.stopbits  = C2_STOPBITS
            self.use_serial = True
            log.info("Centipede 2 mode: RS-232 %d 8N1 on %s",
                     self.baudrate, self.serial_port)

        self.client = self._build_client()

    # ── connection ────────────────────────────────────────────────────────────

    def _build_client(self):
        if self.use_serial:
            return _make_rtu_client(
                self.serial_port, self.baudrate,
                self.bytesize, self.parity, self.stopbits,
                self.timeout,
            )
        return _make_tcp_client(self.host, self.port, self.timeout)

    def connect(self) -> bool:
        # Quick existence check only — let pymodbus open the port itself
        if self.use_serial:
            import os
            if not os.path.exists(self.serial_port):
                log.error("Serial port %s not found — check cable/USB adapter",
                          self.serial_port)
                return False

        try:
            ok = self.client.connect()
        except Exception as e:
            log.error("connect() raised: %s", e)
            return False

        if ok:
            desc = (f"RTU {self.serial_port} @ {self.baudrate} baud"
                    if self.use_serial else f"TCP {self.host}:{self.port}")
            log.info("Connected [%s] device=%s", desc, self.device_type)
        else:
            log.error("Failed to connect to %s",
                      self.serial_port if self.use_serial
                      else f"{self.host}:{self.port}")
        return ok

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass

    def _ensure_connected(self) -> bool:
        if not getattr(self.client, "connected", False):
            return self.connect()
        return True

    # ── conversions ───────────────────────────────────────────────────────────

    @staticmethod
    def raw_to_temp(raw: int) -> float:
        if raw in (0x8000, 0xFFFF, 0):
            return float("nan")
        return raw / 100.0

    @staticmethod
    def raw_to_current(raw: int) -> float:
        if raw == 0xFFFF:
            return float("nan")
        return raw / 1000.0

    # ── LYNX address ──────────────────────────────────────────────────────────

    def _calc_base_address(self, line: int, zone: int) -> int:
        if not (1 <= line <= 4 and 1 <= zone <= 32):
            raise ValueError("Line must be 1–4, Zone must be 1–32")
        return (
            (line - 1) * self.LINE_OFFSET
            + (zone - 1) * self.REGISTERS_PER_ZONE
            + self.ZONE_START_OFFSET
        )

    # ── read all zones (dispatches per device type) ───────────────────────────

    def read_all_zones(self) -> list:
        if not self._ensure_connected():
            raise ConnectionError("Cannot connect to device")
        if self.device_type == "centipede2":
            return self._read_all_centipede2()
        return self._read_all_lynx()

    # ── LYNX reader ───────────────────────────────────────────────────────────

    def _read_all_lynx(self) -> list:
        results = []
        for line in self.lines:
            try:
                results.extend(self._read_lynx_line(line))
            except Exception as exc:
                log.warning("LYNX Line %d read error: %s", line, exc)
                try: self.client.close()
                except Exception: pass
        return results

    def _read_all_centipede2(self) -> list:
        """
        Read all Centipede 2 zones via C2MOD-OI-7 LUI text protocol (RS-232).

        The C2MOD-OI-7 has two shells on the same serial port:
          - OEM setup shell: always intercepts first — needs its own password
            (unknown/factory). Two wrong attempts → disconnect → restart.
          - Application shell (BH>): reached after OEM times out OR if already
            logged in from a previous session.

        Strategy:
          1. Send CRLF — if OEM shell active, blank = wrong password
          2. After two wrong attempts OEM disconnects automatically
          3. Immediately send application password with CRLF
          4. If BH> prompt seen at any point → go straight to Dump
        """
        import re, time as _time
        import serial as _serial

        log.debug("C2: opening raw serial for LUI dump")

        try:
            self.client.close()
        except Exception:
            pass

        results = []
        try:
            ser = _serial.Serial(
                self.serial_port, self.baudrate,
                bytesize=self.bytesize, parity=self.parity,
                stopbits=self.stopbits, timeout=2
            )
            ser.reset_input_buffer()

            def _send(text="", crlf=False, char_delay=0.05, post_delay=1.0):
                """Send text char-by-char then CR only (not CRLF — OI requires CR only)."""
                for c in text:
                    ser.write(c.encode())
                    _time.sleep(char_delay)
                ser.write(b"\r\n" if crlf else b"\r")
                _time.sleep(post_delay)
                r = ser.read(2048).decode(errors="ignore")
                log.debug("C2 tx=%r  rx=%r", text or "<blank>", r[:100])
                return r

            def _read_buf(secs=5):
                """Read dump output. Stops when:
                  - BH> prompt appears (dump finished)
                  - 2s of silence after receiving data (OI done streaming)
                  - Hard timeout (secs) expires
                """
                buf = ""
                last_data = _time.time()
                deadline  = _time.time() + secs
                received_any = False
                while _time.time() < deadline:
                    chunk = ser.read(4096).decode(errors="ignore")
                    if chunk:
                        buf += chunk
                        last_data = _time.time()
                        received_any = True
                    if "BH>" in buf:
                        break
                    # Stop if we got data and then 2s of silence
                    if received_any and (_time.time() - last_data) > 2.0:
                        break
                return buf

            def _do_dump():
                log.debug("C2: running Dump:%d", self.c2_dump_secs)
                _send(f"Dump:{self.c2_dump_secs}", crlf=False,
                      char_delay=0.05, post_delay=0.3)
                # Wait for dump timer + small buffer, then read with 2s silence detection
                _time.sleep(self.c2_dump_secs + 0.5)
                buf = _read_buf(secs=self.c2_dump_secs + 4)
                # Send CR to get BH> prompt back, then Bye to exit
                ser.write(b"\r"); _time.sleep(0.5); ser.read(512)
                _send("Bye", crlf=False, post_delay=0.5)
                return buf

            # ── Step 1: poke and check state ────────────────────────────────
            r = _send("", crlf=False, post_delay=1.2)

            if "BH>" in r or "Bad command" in r:
                # Already at application shell — go straight to dump
                log.debug("C2: already at BH> shell")
                buf = _do_dump()

            elif "password" in r.lower():
                # OEM shell active — send two blank CRLF to burn through
                # OEM's two-attempt lockout, then send application password
                log.debug("C2: OEM shell detected, burning through lockout")
                r2 = _send("", crlf=False, post_delay=1.2)  # 2nd wrong = disconnect

                # OEM disconnects and restarts — wait for it
                _time.sleep(2)
                ser.reset_input_buffer()

                # Now send application password immediately after restart
                r3 = _send(self.c2_password, crlf=False, char_delay=0.05, post_delay=1.5)

                if "BH>" in r3 or "Bad command" in r3:
                    log.debug("C2: logged into application shell")
                    buf = _do_dump()
                elif "incorrect" in r3.lower() or "password" in r3.lower():
                    # OEM restarted again — try one more time with blank to bypass
                    log.debug("C2: OEM restarted, trying blank bypass")
                    _send("", crlf=False, post_delay=1.0)
                    r4 = _send("", crlf=False, post_delay=1.5)
                    if "disabled" in r4.lower() or "BH>" in r4:
                        buf = _do_dump()
                    else:
                        log.error("C2: cannot get past OEM shell. Response: %r", r4[:100])
                        ser.close()
                        return []
                else:
                    log.warning("C2: unexpected response after password: %r", r3[:100])
                    buf = _do_dump()
            else:
                log.warning("C2: unknown initial state: %r", r[:100])
                buf = _do_dump()

            ser.close()
            log.debug("C2: dump buf length=%d", len(buf))

            # Parse zone lines — keep only the LAST reading per zone number
            # (OI streams the full list repeatedly for the dump duration)
            pat = re.compile(
                r'Z(\d{4})\s+ST(\w+)\s+S([\d.]+)C\s+H[\d.]+C\s+L[\d.]+C\s+PV([\d.]+)C\s+DC(\d+)%\s+(\w+)'
            )
            zone_map = {}   # zone_num → last parsed entry
            for m in pat.finditer(buf):
                zone_num = int(m.group(1))
                st_raw   = m.group(2)
                sp       = float(m.group(3))
                pv       = float(m.group(4))
                dc       = int(m.group(5))
                htok     = m.group(6)

                line = ((zone_num - 1) // C2_ZONES_PER_STRING) + 1
                zone = ((zone_num - 1) % C2_ZONES_PER_STRING) + 1

                try:    st_val = int(st_raw, 16)
                except: st_val = 0

                if   htok != "HTOK":  status = "FAULT"
                elif st_val & 0x0010: status = "NO TC"
                elif st_val & 0x0008: status = "NO TC"
                elif st_val & 0x0002: status = "OVER TEMP"
                elif st_val & 0x0004: status = "LO TEMP"
                elif dc > 0:          status = "HEATING"
                else:                 status = "OK"

                zone_map[zone_num] = {
                    "line":           line,
                    "zone":           zone,
                    "pv":             pv,
                    "setpoint":       sp,
                    "output_percent": dc,
                    "current":        None,
                    "status":         status,
                }

            # Sort by zone number and return unique zones only
            results = [zone_map[k] for k in sorted(zone_map)]

        except Exception as e:
            log.error("C2 LUI scan error: %s", e)
            try: ser.close()
            except Exception: pass
        finally:
            try: self.client.connect()
            except Exception: pass

        log.info("C2 LUI scan complete: %d zones found", len(results))
        return results

    def _read_lynx_line(self, line: int) -> list:
        """
        LYNX register map per zone (base = _calc_base_address(line, zone)):
          Holding register [base+0] = Setpoint  (×100, °C)
          Input  register  [base+0] = PV         (×100, °C)
          Input  register  [base+1] = Output %   (×10)
          Input  register  [base+2] = Current    (×1000, A)
        Each zone has REGISTERS_PER_ZONE=24 address slots.
        """
        zones = []
        for z in range(self.ZONES_PER_LINE):
            zone     = z + 1
            base     = self._calc_base_address(line, zone)

            # Read Setpoint from holding register
            sp_resp  = self.client.read_holding_registers(
                address=base, count=1,
                **_slave_kwarg(self.unit_id)
            )
            # Read PV, Output%, Current from input registers
            pv_resp  = self.client.read_input_registers(
                address=base, count=3,
                **_slave_kwarg(self.unit_id)
            )

            sp_err = sp_resp is None or sp_resp.isError()
            pv_err = pv_resp is None or pv_resp.isError()

            if sp_err and pv_err:
                continue  # zone not present

            sp_raw   = sp_resp.registers[0] if not sp_err else 0x8000
            pv_raw   = pv_resp.registers[0] if not pv_err and len(pv_resp.registers) > 0 else 0x8000
            out_raw  = pv_resp.registers[1] if not pv_err and len(pv_resp.registers) > 1 else 0
            curr_raw = pv_resp.registers[2] if not pv_err and len(pv_resp.registers) > 2 else 0xFFFF

            # Skip truly empty zones
            if (sp_raw == 0x8000 and pv_raw == 0x8000) or sp_raw == 0:
                continue

            sp      = self.raw_to_temp(sp_raw)
            pv      = self.raw_to_temp(pv_raw)
            out_pct = out_raw
            current = self.raw_to_current(curr_raw)

            import math
            # Determine status
            status = "OK"
            if math.isnan(pv):
                status = "NO TC"
            elif not math.isnan(sp) and abs(pv - sp) > 5 and out_pct >= 100:
                status = "HEATING"
            elif out_pct == 0 and not math.isnan(sp) and pv > sp + 5:
                status = "OVER TEMP"

            zones.append({
                "line":           line,
                "zone":           zone,
                "pv":             None if math.isnan(pv) else pv,
                "setpoint":       None if math.isnan(sp) else sp,
                "output_percent": out_pct,
                "current":        None if math.isnan(current) else current,
                "status":         status,
            })
        return zones

    # ── Centipede 2 reader ────────────────────────────────────────────────────

    def _read_centipede2_zone(self, zone_num: int, line: int, zone: int) -> Optional[dict]:
        """
        Read one zone via the C2MOD-OI-7 OI gateway (Appendix A & B of manual).
        The OI is the single Modbus slave at self.unit_id.

        Step 1: Write zone_num to OI reg 3 (Module Focus Address)
        Step 2: Read 15 registers from OI reg 4 (Jacket Command)
                which returns zone regs 2–16:
                  offset 0 = zone reg 2  = Status word
                  offset 1 = zone reg 3  = PV temperature °C × 100
                  offset 3 = zone reg 5  = Setpoint °C × 100
                  offset 14= zone reg 16 = PWM duty cycle %
        """
        oi_unit = self.unit_id   # OI gateway Modbus address

        # Step 1: Focus on zone
        wr = self.client.write_register(
            address=C2_OI_FOCUS_REG, value=zone_num,
            **_slave_kwarg(oi_unit)
        )
        if wr is None or wr.isError():
            return None

        # Step 2: Read zone regs 2–16 via OI command register
        resp = self.client.read_holding_registers(
            address=C2_OI_CMD_REG, count=15,
            **_slave_kwarg(oi_unit)
        )
        if resp is None or resp.isError():
            return None

        regs = resp.registers
        if len(regs) < 15:
            return None

        status_raw = regs[0]   # zone reg 2 — status word
        temp_raw   = regs[1]   # zone reg 3 — PV °C × 100
        sp_raw     = regs[3]   # zone reg 5 — setpoint °C × 100
        pwm        = regs[14]  # zone reg 16 — PWM %

        # Zone not present / disabled
        if status_raw == 0 and temp_raw == 0 and sp_raw == 0:
            return None

        pv = temp_raw / 100.0 if temp_raw not in (0x8000, 0xFFFF) else None
        sp = sp_raw   / 100.0 if sp_raw   not in (0x8000, 0xFFFF, 0) else None

        # Decode status word (manual p.12 — hex status codes)
        if   status_raw & 0x0010: status = "NO TC"       # RTD open
        elif status_raw & 0x0008: status = "NO TC"       # RTD short
        elif status_raw & 0x0002: status = "OVER TEMP"   # high alarm
        elif status_raw & 0x0004: status = "LO TEMP"     # low alarm
        elif pwm and pwm > 0:     status = "HEATING"
        else:                     status = "OK"

        return {
            "line":           line,
            "zone":           zone,
            "pv":             pv,
            "setpoint":       sp,
            "output_percent": pwm,
            "current":        None,   # not available via Modbus on C2
            "status":         status,
        }

    def _signed(value: int) -> int:
        return value if value < 0x8000 else value - 0x10000

    # ── context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()


# ── Auto baud detection API ───────────────────────────────────────────────────

def auto_detect_baud(serial_port: str,
                     device_type: str = "lynx",
                     timeout: float = 1.5) -> Optional[int]:
    """
    Convenience wrapper.  For Centipede 2, only tries 19200.
    For LYNX RTU, tries the full AUTO_BAUD_ORDER list.
    Returns detected baud or None.
    """
    if device_type == "centipede2":
        bauds = [C2_BAUD]
    else:
        bauds = AUTO_BAUD_ORDER
    return detect_baud(serial_port, timeout=timeout, try_bauds=bauds)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Read all zones from BriskHeat LYNX / Centipede 2"
    )
    parser.add_argument("--host",    default="192.168.200.20")
    parser.add_argument("--port",    type=int, default=502)
    parser.add_argument("--serial",  help="Serial port (e.g. /dev/ttyUSB0)")
    parser.add_argument("--baud",    type=int, default=0,
                        help="Baud rate (0 = auto-detect)")
    parser.add_argument("--device",  default="lynx",
                        choices=["lynx", "centipede2"],
                        help="Device type")
    parser.add_argument("--lines",     nargs="+", type=int,
                        default=[1, 2, 3, 4])
    parser.add_argument("--timeout",   type=float, default=3.0)
    parser.add_argument("--max-units", type=int,   default=128,
                        help="Centipede 2: max unit IDs to scan (default 128)")
    parser.add_argument("--password",  default="briskheat",
                        help="Centipede 2 OI password (default: briskheat)")
    parser.add_argument("--dump-secs", type=int,   default=3,
                        help="Centipede 2 Dump duration seconds (default: 3)")
    parser.add_argument("--debug",     action="store_true",
                        help="Show detailed session trace")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    baudrate = args.baud
    use_serial = bool(args.serial)

    if use_serial and baudrate == 0:
        print(f"Auto-detecting baud on {args.serial} …")
        baudrate = auto_detect_baud(args.serial, args.device, args.timeout)
        if baudrate is None:
            print("ERROR: No device responded. Check cable and unit ID.")
            return
        print(f"Detected: {baudrate} baud")

    system = LynxTemperatureSystem(
        host=args.host, port=args.port,
        use_serial=use_serial,
        serial_port=args.serial or "/dev/ttyUSB0",
        baudrate=baudrate or 9600,
        device_type=args.device,
        c2_max_units=args.max_units,
        c2_password=args.password,
        c2_dump_secs=args.dump_secs,
        timeout=args.timeout,
        lines=tuple(args.lines),
    )

    try:
        zones = system.read_all_zones()
        if zones:
            if args.device == "centipede2":
                print(f"{'Zone#':<7} {'Line':<5} {'Zone':<5} {'SP':>8} {'PV':>8} {'DC%':>5}  Status")
                print("-" * 52)
                for z in zones:
                    znum = (z["line"]-1)*C2_ZONES_PER_STRING + z["zone"]
                    print(f"Z{znum:04d}  {z['line']:<5} {z['zone']:<5} "
                          f"{z['setpoint'] or 0:>8.1f} {z['pv'] or 0:>8.1f} "
                          f"{z['output_percent'] or 0:>5}  {z['status']}")
            else:
                print(f"{'Line':<6} {'Zone':<6} {'SP':>8} {'PV':>8} {'Out%':>7} {'A':>8}  Status")
                print("-" * 58)
                for z in zones:
                    curr = f"{z['current']:.3f}" if z["current"] is not None else "  n/a"
                    print(f"{z['line']:<6} {z['zone']:<6} "
                          f"{z['setpoint'] or 0:>8.2f} {z['pv'] or 0:>8.2f} "
                          f"{z['output_percent'] or 0:>7} {curr:>8}  {z['status']}")
        else:
            print("No active zones found.")
            if args.device == "centipede2":
                print("\nTroubleshoot checklist:")
                print(f"  1. OI must be in LUI mode (Com 1 on touchscreen)")
                print(f"  2. Run with --debug to see full session trace")
                print(f"  3. Check port is free:  fuser /dev/ttyUSB0")
                print(f"  4. Try longer dump:     --dump-secs 5")
                print(f"  5. Wrong password?      --password <pw>")
        print(f"\n{len(zones)} active zone(s) found.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        system.close()


if __name__ == "__main__":
    main()
