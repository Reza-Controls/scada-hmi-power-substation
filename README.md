# SCADA/HMI Mimic Panel — Power Distribution Substation

A web-based HMI single-line diagram for a distribution substation: simulated
RTU/protective relays over Modbus TCP, real-time protection logic (overcurrent,
transformer thermal, undervoltage permissive, breaker lockout), operator
breaker controls, and trending — plus the control narrative and alarm
rationalization documents that would precede this in a real project.

## Demo

![Substation HMI screenshot](screenshots/hmi_screenshot.png)

## Documents

- **`control_narrative.docx`** — process description, energization sequence,
  and a protection scheme table using real IEEE C37.2 device numbers
  (27, 49, 50, 51, 81, 86).
- **`alarm_rationalization.xlsx`** — ISA-18.2-style table of every alarm.

## Architecture

```
plc_simulator.py  --(Modbus TCP, port 5040)-->  bridge_server.py  --(WebSocket)-->  browser (index.html)
     |                                                  |
  electrical physics +                           alarm evaluation +
  protection logic (50/51/49/27/86)             command relay back to RTU
```

- **`plc_simulator.py`** — Simulates the substation: incoming feed → CB-101 →
  transformer T-1 → secondary bus → three feeder breakers (CB-201/202/203).
  Protection logic is enforced in the process model itself — a feeder
  breaker will trip on sustained overcurrent (51) even if commanded closed,
  and CB-101 trips instantly on a bus fault (50) or transformer overtemp (49),
  exactly like real protective relays operate independently of SCADA.
- **`bridge_server.py`** — Polls the simulated RTU, evaluates every alarm
  from the rationalization table, streams to the browser over WebSocket, and
  relays operator commands (breaker close/open, protection reset) back over
  Modbus.
- **`index.html`** — The HMI single-line diagram: breaker symbols that
  change color by state (green = closed, gray = open, red = tripped/lockout),
  energized lines that highlight when carrying power, live metering, an
  alarm banner, and a trend chart.

## Running it

```bash
pip install -r requirements.txt

# Terminal 1 — simulated RTU
python plc_simulator.py

# Terminal 2 — bridge server + HMI
python bridge_server.py
```

Then open **http://localhost:5070** in your browser.

## Try this to see the protection logic work

1. Close **CB-101** — incoming breaker energizes, and the bus voltage rises
   toward ~13.8 kV.
2. Close **CB-201, CB-202, CB-203** — each feeder energizes and starts
   drawing current.
3. Leave it running — occasionally a simulated downstream fault will spike a
   feeder's current, and if sustained, its breaker will trip on time-overcurrent
   protection (51) after the coordination delay. Rarely, a simulated bus fault
   will trip CB-101 instantly (50).
4. Once tripped, a breaker is in **lockout** (86) and won't reclose even if
   you click Close — you must click **Reset** first, exactly like a real
   protection lockout relay.

## What I'd do differently at production scale

- Replace the custom simulator with a real protective relay simulator or
  RTU, communicating via DNP3 (the actual utility SCADA protocol) instead of
  Modbus
- Use a real historian for long-term trending instead of in-memory,
  browser-session-only charts
- Add time-synchronized event recording (sequence-of-events) for post-fault
  analysis, standard practice in real substations
- Add a proper single-line diagram editor instead of hand-coded SVG
  coordinates

## Tech stack

Python · pymodbus (Modbus TCP) · Flask · Flask-SocketIO · vanilla JS · Chart.js · SVG
