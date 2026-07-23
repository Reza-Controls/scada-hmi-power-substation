"""
plc_simulator.py

Simulates the substation's RTU/protective relay system over Modbus TCP.
Protection logic (overcurrent, thermal, lockout) is enforced directly in the
process model, the same way real protective relays operate independently of
SCADA supervisory control.

Modbus map
----------
Holding registers (analog values):
  0: VT-101 incoming voltage      (kV   x10)
  1: CT-101 incoming current      (A    x10)
  2: TT-201 winding temperature   (degC x10)
  3: TT-202 oil temperature       (degC x10)
  4: VT-301 bus voltage           (kV   x10)
  5: FT-301 bus frequency         (Hz   x100)
  6: CT-401 feeder 1 current      (A    x10)
  7: CT-402 feeder 2 current      (A    x10)
  8: CT-403 feeder 3 current      (A    x10)

Discrete inputs (status bits):
  0: CB-101 closed   1: CB-101 tripped (lockout)
  2: CB-201 closed   3: CB-201 tripped
  4: CB-202 closed   5: CB-202 tripped
  6: CB-203 closed   7: CB-203 tripped

Coils (command bits, written by the HMI/bridge):
  0: CB-101 close command (level: True = commanded closed)
  1: CB-101 reset command (pulse: clears lockout after a trip)
  2: CB-201 close command   3: CB-201 reset command
  4: CB-202 close command   5: CB-202 reset command
  6: CB-203 close command   7: CB-203 reset command

Run this first, then run bridge_server.py.
"""

import asyncio
import logging
import random

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusSlaveContext,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("plc_simulator")

HOST = "127.0.0.1"
PORT = 5040
UPDATE_INTERVAL_SEC = 2.0

NOMINAL_VT101 = 69.0     # kV
NOMINAL_VT301 = 13.8     # kV
TRANSFORMER_RATIO = NOMINAL_VT101 / NOMINAL_VT301
NOMINAL_FREQ = 60.0

# --- Protection setpoints (must match control_narrative.docx) ---------------
BUS_UV_PERMISSIVE = 0.90 * NOMINAL_VT301   # INT-01: feeder close permissive
TT_HIHI = 120.0                             # INT-02 (winding), degC
TT202_HIHI = 100.0                          # INT-02 (oil), degC
CT101_INSTANTANEOUS = 460.0                 # INT-04 (50), A
FEEDER_PICKUP = 350.0                       # INT-03 (51), A
FEEDER_TIME_DELAY_TICKS = 4                 # INT-03 coordination time
FEEDER_FAULT_CHANCE_PER_TICK = 0.012
FEEDER_FAULT_DURATION_TICKS = 6
BUS_FAULT_CHANCE_PER_TICK = 0.003


class Breaker:
    """A breaker with a close command, protection trip/lockout, and overcurrent timer."""

    def __init__(self, name):
        self.name = name
        self.closed = False
        self.tripped = False
        self.overcurrent_ticks = 0

    def apply_command(self, close_cmd, reset_cmd, permissive_ok):
        if reset_cmd and self.tripped:
            self.tripped = False
            self.overcurrent_ticks = 0
            log.info(f"{self.name} lockout reset by operator")
        self.closed = bool(close_cmd) and not self.tripped and permissive_ok

    def check_time_overcurrent(self, current, pickup, delay_ticks):
        if not self.closed:
            self.overcurrent_ticks = 0
            return
        if current > pickup:
            self.overcurrent_ticks += 1
            if self.overcurrent_ticks >= delay_ticks:
                self.trip("51 time-overcurrent")
        else:
            self.overcurrent_ticks = 0

    def trip(self, reason):
        if not self.tripped:
            log.warning(f"{self.name} TRIPPED ({reason})")
        self.closed = False
        self.tripped = True
        self.overcurrent_ticks = 0


class SubstationState:
    def __init__(self):
        self.t = 0
        self.cb101 = Breaker("CB-101")
        self.cb201 = Breaker("CB-201")
        self.cb202 = Breaker("CB-202")
        self.cb203 = Breaker("CB-203")

        self.vt101 = NOMINAL_VT101
        self.ct101 = 0.0
        self.tt201 = 45.0
        self.tt202 = 40.0
        self.vt301 = 0.0
        self.freq = NOMINAL_FREQ

        self.feeder_base_load = {"f1": 180.0, "f2": 150.0, "f3": 120.0}
        self.feeder_current = {"f1": 0.0, "f2": 0.0, "f3": 0.0}
        self.feeder_fault_ticks = {"f1": 0, "f2": 0, "f3": 0}

    def step(self, coils):
        self.t += 1
        (cb101_close, cb101_reset, cb201_close, cb201_reset,
         cb202_close, cb202_reset, cb203_close, cb203_reset) = coils

        # --- Incoming voltage: small noise around nominal ---
        self.vt101 = NOMINAL_VT101 + random.gauss(0, 0.3)

        # --- CB-101: no permissive of its own, just command + protection ---
        self.cb101.apply_command(cb101_close, cb101_reset, permissive_ok=True)

        # --- Bus energization depends on CB-101 ---
        if self.cb101.closed:
            target_vt301 = self.vt101 / TRANSFORMER_RATIO
        else:
            target_vt301 = 0.0
        self.vt301 += (target_vt301 - self.vt301) * 0.5 + random.gauss(0, 0.02)
        self.vt301 = max(0.0, self.vt301)

        bus_permissive_ok = self.vt301 > BUS_UV_PERMISSIVE

        # --- Feeder breakers: need CB-101 closed + bus voltage permissive (INT-01) ---
        feeder_permissive = self.cb101.closed and bus_permissive_ok
        self.cb201.apply_command(cb201_close, cb201_reset, feeder_permissive)
        self.cb202.apply_command(cb202_close, cb202_reset, feeder_permissive)
        self.cb203.apply_command(cb203_close, cb203_reset, feeder_permissive)

        # --- Feeder currents (with occasional fault injection) ---
        for key, breaker in (("f1", self.cb201), ("f2", self.cb202), ("f3", self.cb203)):
            if breaker.closed:
                if self.feeder_fault_ticks[key] == 0 and random.random() < FEEDER_FAULT_CHANCE_PER_TICK:
                    self.feeder_fault_ticks[key] = FEEDER_FAULT_DURATION_TICKS
                    log.warning(f"Simulated downstream fault starting on {key.upper()}")

                if self.feeder_fault_ticks[key] > 0:
                    self.feeder_current[key] = self.feeder_base_load[key] + 300 + random.gauss(0, 15)
                    self.feeder_fault_ticks[key] -= 1
                else:
                    target = self.feeder_base_load[key]
                    self.feeder_current[key] += (target - self.feeder_current[key]) * 0.3 + random.gauss(0, 4)
                    self.feeder_current[key] = max(0.0, self.feeder_current[key])
            else:
                self.feeder_current[key] = 0.0
                self.feeder_fault_ticks[key] = 0

        self.cb201.check_time_overcurrent(self.feeder_current["f1"], FEEDER_PICKUP, FEEDER_TIME_DELAY_TICKS)
        self.cb202.check_time_overcurrent(self.feeder_current["f2"], FEEDER_PICKUP, FEEDER_TIME_DELAY_TICKS)
        self.cb203.check_time_overcurrent(self.feeder_current["f3"], FEEDER_PICKUP, FEEDER_TIME_DELAY_TICKS)
        # A trip changes .closed, which zeroes current next tick via the branch above

        # --- Transformer/incoming current reflects total feeder load ---
        total_feeder_current = sum(self.feeder_current.values())
        no_load_current = 15.0 if self.cb101.closed else 0.0
        self.ct101 += ((total_feeder_current / TRANSFORMER_RATIO + no_load_current) - self.ct101) * 0.4

        # Rare direct bus fault -> instantaneous (50) trip of CB-101
        if self.cb101.closed and random.random() < BUS_FAULT_CHANCE_PER_TICK:
            self.ct101 = CT101_INSTANTANEOUS + 40
            log.warning("Simulated bus fault (instantaneous)")

        if self.ct101 > CT101_INSTANTANEOUS:
            self.cb101.trip("50 instantaneous overcurrent")

        # --- Transformer thermal model: rises with loading, falls toward ambient ---
        loading_factor = self.ct101 / 100.0  # rough per-unit-ish heating driver
        target_tt201 = 45.0 + loading_factor * 12.0
        target_tt202 = 40.0 + loading_factor * 9.0
        self.tt201 += (target_tt201 - self.tt201) * 0.05 + random.gauss(0, 0.15)
        self.tt202 += (target_tt202 - self.tt202) * 0.04 + random.gauss(0, 0.12)

        if self.tt201 > TT_HIHI or self.tt202 > TT202_HIHI:
            self.cb101.trip("49 transformer thermal")

        # If CB-101 trips, everything downstream loses its permissive next tick naturally

        self.freq = NOMINAL_FREQ + random.gauss(0, 0.03)

        return self._snapshot()

    def _snapshot(self):
        analog = [
            int(round(self.vt101 * 10)),
            int(round(self.ct101 * 10)),
            int(round(self.tt201 * 10)),
            int(round(self.tt202 * 10)),
            int(round(self.vt301 * 10)),
            int(round(self.freq * 100)),
            int(round(self.feeder_current["f1"] * 10)),
            int(round(self.feeder_current["f2"] * 10)),
            int(round(self.feeder_current["f3"] * 10)),
        ]
        status = [
            self.cb101.closed, self.cb101.tripped,
            self.cb201.closed, self.cb201.tripped,
            self.cb202.closed, self.cb202.tripped,
            self.cb203.closed, self.cb203.tripped,
        ]
        return analog, status


async def run_server(context):
    log.info(f"Starting simulated substation RTU (Modbus TCP) on {HOST}:{PORT}")
    await StartAsyncTcpServer(context=context, address=(HOST, PORT))


async def update_loop(context):
    slave_id = 0x00
    state = SubstationState()
    while True:
        coil_values = context[slave_id].getValues(1, 0, count=8)
        analog, status = state.step(coil_values)

        context[slave_id].setValues(3, 0, analog)
        context[slave_id].setValues(2, 0, status)

        log.info(
            f"VT101={state.vt101:5.1f}kV CT101={state.ct101:5.1f}A "
            f"TT201={state.tt201:5.1f}C TT202={state.tt202:5.1f}C "
            f"VT301={state.vt301:5.2f}kV F={state.freq:5.2f}Hz  "
            f"CB101={'CLSD' if state.cb101.closed else ('TRIP' if state.cb101.tripped else 'open')}  "
            f"F1={state.feeder_current['f1']:5.1f}A({'CLSD' if state.cb201.closed else ('TRIP' if state.cb201.tripped else 'open')})  "
            f"F2={state.feeder_current['f2']:5.1f}A({'CLSD' if state.cb202.closed else ('TRIP' if state.cb202.tripped else 'open')})  "
            f"F3={state.feeder_current['f3']:5.1f}A({'CLSD' if state.cb203.closed else ('TRIP' if state.cb203.tripped else 'open')})"
        )
        await asyncio.sleep(UPDATE_INTERVAL_SEC)


async def main():
    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 10),
        co=ModbusSequentialDataBlock(0, [0] * 10),
        hr=ModbusSequentialDataBlock(0, [0] * 12),
    )
    context = ModbusServerContext(slaves=store, single=True)
    await asyncio.gather(run_server(context), update_loop(context))


if __name__ == "__main__":
    asyncio.run(main())
