"""
bridge_server.py

Bridges the simulated substation RTU (plc_simulator.py, Modbus TCP) to a web
browser: polls tags, evaluates alarms from alarm_rationalization.xlsx, streams
to the browser over WebSocket, and relays operator commands (breaker close,
reset) back down over Modbus.

Run plc_simulator.py first, then this. Open http://localhost:5060 in a browser.
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone

from flask import Flask, send_from_directory
from flask_socketio import SocketIO
from pymodbus.client import ModbusTcpClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bridge_server")

PLC_HOST = "127.0.0.1"
PLC_PORT = 5040
POLL_INTERVAL_SEC = 1.5
WEB_PORT = 5070

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

TAG_NAMES = ["vt101", "ct101", "tt201", "tt202", "vt301", "freq", "ct401", "ct402", "ct403"]
TAG_SCALES = {"vt101": 10, "ct101": 10, "tt201": 10, "tt202": 10, "vt301": 10, "freq": 100, "ct401": 10, "ct402": 10, "ct403": 10}
STATUS_NAMES = [
    "cb101_closed", "cb101_tripped", "cb201_closed", "cb201_tripped",
    "cb202_closed", "cb202_tripped", "cb203_closed", "cb203_tripped",
]

ALARM_DEFS = [
    ("VT101_LO", "Incoming line voltage low", "High", lambda t: t["vt101"] < 0.90 * 69.0),
    ("VT101_HI", "Incoming line voltage high", "Medium", lambda t: t["vt101"] > 1.10 * 69.0),
    ("TT201_HI", "Transformer winding temperature high", "Medium", lambda t: t["tt201"] > 105),
    ("TT201_HIHI", "Transformer winding temperature high-high", "High", lambda t: t["tt201"] > 120),
    ("TT202_HI", "Transformer oil temperature high", "Medium", lambda t: t["tt202"] > 85),
    ("TT202_HIHI", "Transformer oil temperature high-high", "High", lambda t: t["tt202"] > 100),
    ("VT301_LO", "Secondary bus voltage low", "High", lambda t: t["cb101_closed"] and t["vt301"] < 0.90 * 13.8),
    ("VT301_HI", "Secondary bus voltage high", "Medium", lambda t: t["vt301"] > 1.10 * 13.8),
    ("FT301_LO", "Bus frequency low", "Medium", lambda t: t["cb101_closed"] and t["freq"] < 59.5),
    ("FT301_HI", "Bus frequency high", "Medium", lambda t: t["freq"] > 60.5),
    ("CT401_HI", "Feeder 1 current high", "Medium", lambda t: t["ct401"] > 0.85 * 350),
    ("CT402_HI", "Feeder 2 current high", "Medium", lambda t: t["ct402"] > 0.85 * 350),
    ("CT403_HI", "Feeder 3 current high", "Medium", lambda t: t["ct403"] > 0.85 * 350),
    ("CB101_TRIP", "Incoming breaker trip", "High", lambda t: t["cb101_tripped"]),
    ("CB201_TRIP", "Feeder 1 breaker trip", "High", lambda t: t["cb201_tripped"]),
    ("CB202_TRIP", "Feeder 2 breaker trip", "High", lambda t: t["cb202_tripped"]),
    ("CB203_TRIP", "Feeder 3 breaker trip", "High", lambda t: t["cb203_tripped"]),
]

alarm_state = {}
alarm_lock = threading.Lock()
latest_tags = {}
tags_lock = threading.Lock()

modbus_client = ModbusTcpClient(PLC_HOST, port=PLC_PORT)

COIL_MAP = {
    "CB101_CLOSE": 0, "CB101_RESET": 1,
    "CB201_CLOSE": 2, "CB201_RESET": 3,
    "CB202_CLOSE": 4, "CB202_RESET": 5,
    "CB203_CLOSE": 6, "CB203_RESET": 7,
}


def connect_plc():
    log.info(f"Connecting to simulated substation RTU at {PLC_HOST}:{PLC_PORT} ...")
    for attempt in range(10):
        if modbus_client.connect():
            log.info("Connected to RTU.")
            return True
        log.warning(f"RTU not reachable yet, retrying... ({attempt + 1}/10)")
        time.sleep(2)
    return False


def read_tags():
    hr = modbus_client.read_holding_registers(address=0, count=9, slave=0)
    di = modbus_client.read_discrete_inputs(address=0, count=8, slave=0)
    if hr.isError() or di.isError():
        raise IOError("Modbus read error")

    tags = {}
    for name, raw in zip(TAG_NAMES, hr.registers):
        tags[name] = raw / TAG_SCALES[name]
    for name, bit in zip(STATUS_NAMES, di.bits[:8]):
        tags[name] = bool(bit)
    return tags


def evaluate_alarms(tags):
    now = datetime.now(timezone.utc).isoformat()
    with alarm_lock:
        for alarm_id, description, priority, condition in ALARM_DEFS:
            is_active = condition(tags)
            existing = alarm_state.get(alarm_id)
            if is_active:
                if existing is None or not existing["active"]:
                    alarm_state[alarm_id] = {
                        "id": alarm_id, "description": description, "priority": priority,
                        "active": True, "acknowledged": False, "since": now,
                    }
                    log.warning(f"ALARM ACTIVE: {alarm_id} - {description} ({priority})")
            else:
                if existing is not None and existing["active"]:
                    existing["active"] = False
                    log.info(f"Alarm cleared: {alarm_id}")
        return list(alarm_state.values())


def poll_loop():
    if not connect_plc():
        log.error("Could not connect to RTU after retries. Is plc_simulator.py running?")
        return

    while True:
        try:
            tags = read_tags()
            alarms = evaluate_alarms(tags)
            with tags_lock:
                latest_tags.clear()
                latest_tags.update(tags)
            socketio.emit("tag_update", {"tags": tags, "alarms": alarms})
        except Exception as e:
            log.error(f"Poll error: {e}")
        time.sleep(POLL_INTERVAL_SEC)


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/vendor_js/<path:filename>")
def vendor_js(filename):
    return send_from_directory(os.path.join(BASE_DIR, "vendor_js"), filename)


@socketio.on("connect")
def on_connect():
    with tags_lock:
        tags_copy = dict(latest_tags)
    with alarm_lock:
        alarms_copy = list(alarm_state.values())
    socketio.emit("tag_update", {"tags": tags_copy, "alarms": alarms_copy})


@socketio.on("command")
def on_command(data):
    """data: {tag: 'CB101_CLOSE'|'CB101_RESET'|..., state: bool}"""
    tag = data.get("tag")
    state = bool(data.get("state"))
    if tag not in COIL_MAP:
        log.warning(f"Unknown command tag: {tag}")
        return
    try:
        modbus_client.write_coil(COIL_MAP[tag], state, slave=0)
        log.info(f"Command sent: {tag} = {state}")
    except Exception as e:
        log.error(f"Failed to send command {tag}={state}: {e}")


@socketio.on("ack_alarm")
def on_ack_alarm(data):
    alarm_id = data.get("id")
    with alarm_lock:
        if alarm_id in alarm_state:
            alarm_state[alarm_id]["acknowledged"] = True
            log.info(f"Alarm acknowledged: {alarm_id}")


if __name__ == "__main__":
    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()
    log.info(f"Starting web server on http://localhost:{WEB_PORT}")
    socketio.run(app, host="0.0.0.0", port=WEB_PORT, allow_unsafe_werkzeug=True)
