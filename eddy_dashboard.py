#!/usr/bin/env python3
import atexit
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

import websocket
from flask import Flask, Response, jsonify, request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "eddy_dashboard_config.json")

DEFAULT_CONFIG = {
    "moonraker_host": "127.0.0.1",
    "moonraker_port": 7125,
    "probe_type": "btt_eddy",
    "eddy_sensor_name": "btt_eddy",
    "temperature_probe_name": "btt_eddy",
    "cartographer_model_text": "",
    "web_host": "0.0.0.0",
    "web_port": 8085,
    "calibration_text": ""
}

MAX_HISTORY = 50000

app = Flask(__name__)
lock = threading.Lock()
history = deque(maxlen=MAX_HISTORY)

connected = False
latest_temperature = None
baseline_frequency = None
baseline_temperature = None
baseline_z = None

config = dict(DEFAULT_CONFIG)
cal_by_freq = []
carto_model = None
active_klipper_ws = None
cartographer_stream_owned = False


def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            config.update(saved)
        except Exception as e:
            print("Could not load config:", e)


def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def parse_calibration_text(text):
    """
    Accepts pasted Klipper SAVE_CONFIG calibration text such as:

    calibrate =
    #*#   0.050000:678437.389,0.090000:678275.407,
    #*#   0.130000:678111.331,...

    It also tolerates markdown artifacts such as #*#, *#*#, and trailing *.
    """
    pairs = re.findall(
        r'([+-]?\d+(?:\.\d+)?)\s*:\s*([+-]?\d+(?:\.\d+)?)',
        text or ""
    )

    parsed = []
    for z_text, freq_text in pairs:
        try:
            z = float(z_text)
            freq = float(freq_text)
        except ValueError:
            continue
        parsed.append((z, freq))

    if len(parsed) < 2:
        raise ValueError("Could not find at least two calibration points.")

    # Remove duplicate frequency entries while preserving the last value.
    by_freq = {}
    for z, freq in parsed:
        by_freq[freq] = z

    result = sorted((freq, z) for freq, z in by_freq.items())

    if len(result) < 2:
        raise ValueError("Calibration does not contain enough unique frequencies.")

    return result


def rebuild_calibration():
    global cal_by_freq
    text = config.get("calibration_text", "")
    if not text.strip():
        cal_by_freq = []
        return
    cal_by_freq = parse_calibration_text(text)


def frequency_to_z(freq):
    if not cal_by_freq:
        return None

    if freq < cal_by_freq[0][0] or freq > cal_by_freq[-1][0]:
        return None

    for i in range(len(cal_by_freq) - 1):
        f1, z1 = cal_by_freq[i]
        f2, z2 = cal_by_freq[i + 1]

        if f1 <= freq <= f2:
            if f2 == f1:
                return z1
            ratio = (freq - f1) / (f2 - f1)
            return z1 + ratio * (z2 - z1)

    return None


def parse_cartographer_model_text(text):
    """
    Parse the Cartographer scan model SAVE_CONFIG block.

    Required:
      coefficients = ...
      domain = lower,upper

    Optional:
      z_offset = ...
      reference_temperature = ...
    """
    if not text or not text.strip():
        raise ValueError("Cartographer scan model data is empty.")

    coeff_match = re.search(
        r"(?im)^\s*(?:#\*#\s*)?coefficients\s*=\s*([^\r\n]+)",
        text
    )
    domain_match = re.search(
        r"(?im)^\s*(?:#\*#\s*)?domain\s*=\s*([^\r\n]+)",
        text
    )
    z_match = re.search(
        r"(?im)^\s*(?:#\*#\s*)?z_offset\s*=\s*([+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?)",
        text
    )
    temp_match = re.search(
        r"(?im)^\s*(?:#\*#\s*)?reference_temperature\s*=\s*([+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?)",
        text
    )

    if not coeff_match or not domain_match:
        raise ValueError(
            "Could not find both 'coefficients =' and 'domain =' in the Cartographer scan model."
        )

    def numbers(value):
        return [
            float(x)
            for x in re.findall(
                r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?",
                value,
                re.I
            )
        ]

    coefficients = numbers(coeff_match.group(1))
    domain = numbers(domain_match.group(1))

    if len(coefficients) < 2:
        raise ValueError("Cartographer model has too few coefficients.")

    if len(domain) != 2 or domain[0] == domain[1]:
        raise ValueError("Cartographer model domain is invalid.")

    return {
        "coefficients": coefficients,
        "domain": domain,
        "z_offset": float(z_match.group(1)) if z_match else 0.0,
        "reference_temperature": float(temp_match.group(1)) if temp_match else None
    }


def cartographer_frequency_to_distance(frequency):
    """
    Reproduce Cartographer's raw scan-model Polynomial evaluation.

    Cartographer constructs:
        Polynomial(coefficients, domain=domain)

    NumPy Polynomial maps 'domain' onto the default window [-1, 1],
    so we perform the same mapping here without adding NumPy as a dependency.

    This does not apply an optional Cartographer coil temperature-compensation
    calibration. With no coil temperature-compensation model configured, this
    matches the scan model's frequency-to-distance calculation.
    """
    if carto_model is None or frequency is None or frequency <= 0:
        return None

    lower, upper = carto_model["domain"]
    inverse_frequency = 1.0 / float(frequency)

    # Match Cartographer: outside the model's inverse-frequency domain is invalid.
    if inverse_frequency < min(lower, upper) or inverse_frequency > max(lower, upper):
        return None

    # Polynomial domain -> default window [-1, 1]
    y = (2.0 * inverse_frequency - (lower + upper)) / (upper - lower)

    value = 0.0
    power = 1.0
    for coefficient in carto_model["coefficients"]:
        value += coefficient * power
        power *= y

    return value + carto_model["z_offset"]


def rebuild_cartographer_model():
    global carto_model

    model_text = config.get("cartographer_model_text", "")
    if not model_text.strip():
        carto_model = None
        return

    carto_model = parse_cartographer_model_text(model_text)


def moonraker_http_url(path):
    host = config["moonraker_host"]
    port = config["moonraker_port"]
    return f"http://{host}:{port}{path}"


def klipper_ws_url():
    host = config["moonraker_host"]
    port = config["moonraker_port"]
    return f"ws://{host}:{port}/klippysocket"


def temperature_worker():
    global latest_temperature

    while True:
        try:
            if config.get("probe_type", "btt_eddy") == "cartographer":
                with lock:
                    latest_temperature = None
                time.sleep(1.0)
                continue

            name = config.get("temperature_probe_name", "").strip()

            if not name:
                with lock:
                    latest_temperature = None
                time.sleep(1.0)
                continue

            object_name = f"temperature_probe {name}"
            encoded = urllib.parse.quote(object_name, safe="")
            url = moonraker_http_url(
                f"/printer/objects/query?{encoded}"
            )

            with urllib.request.urlopen(url, timeout=2) as response:
                data = json.loads(response.read().decode())

            temp = data["result"]["status"][object_name]["temperature"]

            with lock:
                latest_temperature = float(temp)

        except Exception:
            with lock:
                latest_temperature = None

        time.sleep(1.0)


def append_history_point(frequency, temp, z, sensor_time=None):
    global baseline_frequency
    global baseline_temperature
    global baseline_z

    if sensor_time is None:
        sensor_time = time.monotonic()

    with lock:
        if baseline_frequency is None:
            baseline_frequency = frequency

        if baseline_temperature is None and temp is not None:
            baseline_temperature = temp

        if baseline_z is None and z is not None:
            baseline_z = z

        history.append({
            "sensor_time": sensor_time,
            "wall_time": time.time(),
            "frequency": frequency,
            "temperature": temp,
            "z": z
        })


def run_gcode_script(script):
    """Run one G-code command through Moonraker."""
    url = moonraker_http_url("/printer/gcode/script")
    body = json.dumps({"script": script}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=5) as response:
        raw = response.read().decode("utf-8")
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        return None


def start_cartographer_stream():
    """
    Start the Cartographer plugin's own stream session.

    The current Cartographer3D plugin only updates mcu.last_sample while its
    MCU stream is active. CARTOGRAPHER_STREAM starts a plugin-owned Session.
    """
    global cartographer_stream_owned

    if cartographer_stream_owned:
        return True

    try:
        run_gcode_script("CARTOGRAPHER_STREAM ACTION=START")
        cartographer_stream_owned = True
        print("Cartographer live stream started")
        return True
    except Exception as e:
        # If another Cartographer stream session is already active, last_sample
        # may still be live. Do not cancel a session we did not create.
        print("Could not start Cartographer stream:", e)
        return False


def stop_cartographer_stream():
    """Cancel only the CARTOGRAPHER_STREAM session started by this dashboard."""
    global cartographer_stream_owned

    if not cartographer_stream_owned:
        return

    try:
        run_gcode_script("CARTOGRAPHER_STREAM ACTION=CANCEL")
        print("Cartographer live stream stopped")
    except Exception as e:
        print("Could not stop Cartographer stream:", e)
    finally:
        cartographer_stream_owned = False


atexit.register(stop_cartographer_stream)



def cartographer_worker():
    """
    Read live Cartographer3D Plugin v1.x data.

    The live object is 'cartographer', with:
        cartographer.mcu.last_sample.frequency
        cartographer.mcu.last_sample.temperature
        cartographer.mcu.last_sample.time

    The plugin normally stops MCU streaming while idle. The dashboard starts
    CARTOGRAPHER_STREAM to keep last_sample updating, then periodically recycles
    that session so the plugin's in-memory sample list cannot grow forever.
    """
    global connected
    global cartographer_stream_owned

    last_sample_time = None
    last_fresh_wall = 0.0
    last_recycle_wall = time.time()

    print("Using Cartographer object: cartographer")
    start_cartographer_stream()

    try:
        while config.get("probe_type", "btt_eddy") == "cartographer":
            try:
                encoded = urllib.parse.quote("cartographer", safe="")
                url = moonraker_http_url(
                    f"/printer/objects/query?{encoded}"
                )

                with urllib.request.urlopen(url, timeout=2) as response:
                    data = json.loads(response.read().decode())

                status = data["result"]["status"]["cartographer"]
                sample = (
                    status.get("mcu", {})
                          .get("last_sample")
                )

                if not sample:
                    connected = False

                    if not cartographer_stream_owned:
                        start_cartographer_stream()

                    time.sleep(0.1)
                    continue

                frequency = float(sample["frequency"])
                temp = (
                    float(sample["temperature"])
                    if sample.get("temperature") is not None
                    else None
                )
                sample_time = (
                    float(sample["time"])
                    if sample.get("time") is not None
                    else None
                )

                # Only append when Cartographer supplied a new MCU sample.
                if sample_time != last_sample_time:
                    last_sample_time = sample_time
                    last_fresh_wall = time.time()
                    connected = True

                    z = cartographer_frequency_to_distance(frequency)

                    append_history_point(
                        frequency,
                        temp,
                        z,
                        sample_time if sample_time is not None else time.monotonic()
                    )

                # If the sample stopped changing, try to restore a stream.
                elif time.time() - last_fresh_wall > 2.0:
                    connected = False

                    if not cartographer_stream_owned:
                        start_cartographer_stream()

                # CARTOGRAPHER_STREAM stores every sample in a Python list.
                # Recycle our session every 10 seconds to keep its memory bounded.
                if (
                    cartographer_stream_owned
                    and time.time() - last_recycle_wall >= 10.0
                ):
                    stop_cartographer_stream()
                    time.sleep(0.05)
                    start_cartographer_stream()
                    last_recycle_wall = time.time()

            except Exception as e:
                connected = False
                print("Cartographer status read error:", e)
                time.sleep(0.5)
                continue

            # Poll the Klipper status object at 20 Hz.
            time.sleep(0.05)

    finally:
        connected = False
        stop_cartographer_stream()


def klipper_worker():
    global connected
    global active_klipper_ws

    while True:
        probe_type = config.get("probe_type", "btt_eddy")

        if probe_type == "cartographer":
            print("Using Cartographer scanner status stream")
            cartographer_worker()

            if config.get("probe_type", "btt_eddy") == "cartographer":
                time.sleep(1.0)

            continue

        sensor_name = config.get("eddy_sensor_name", "").strip()

        if not sensor_name:
            connected = False
            time.sleep(2)
            continue

        try:
            def on_open(ws):
                global connected
                connected = True
                print("Connected to Klipper LDC1612 stream")

                ws.send(json.dumps({
                    "id": 1,
                    "method": "ldc1612/dump_ldc1612",
                    "params": {
                        "sensor": sensor_name
                    }
                }))

            def on_message(ws, message):
                try:
                    msg = json.loads(message)
                except Exception:
                    return

                if msg.get("id") == 1 and "error" in msg:
                    print("Klipper stream error:", msg["error"])
                    return

                samples = msg.get("params", {}).get("data", [])
                if not samples:
                    return

                frequencies = [
                    float(sample[1])
                    for sample in samples
                ]
                sensor_times = [
                    float(sample[0])
                    for sample in samples
                ]

                frequency = (
                    sum(frequencies)
                    / len(frequencies)
                )
                sensor_time = (
                    sum(sensor_times)
                    / len(sensor_times)
                )

                with lock:
                    temp = latest_temperature

                z = frequency_to_z(frequency)

                append_history_point(
                    frequency,
                    temp,
                    z,
                    sensor_time
                )

            def on_error(ws, error):
                global connected
                connected = False
                print("Klipper websocket error:", error)

            def on_close(ws, status, message):
                global connected
                connected = False
                print("Klipper websocket disconnected")

            ws = websocket.WebSocketApp(
                klipper_ws_url(),
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )

            active_klipper_ws = ws
            ws.run_forever()
            active_klipper_ws = None

        except Exception as e:
            connected = False
            active_klipper_ws = None
            print("Klipper stream exception:", e)

        print("Retrying Klipper connection in 2 seconds...")
        time.sleep(2)


@app.route("/api/status")
def api_status():
    with lock:
        current = dict(history[-1]) if history else {}

        result = {
            "connected": connected,
            "current": current,
            "baseline": {
                "frequency": baseline_frequency,
                "temperature": baseline_temperature,
                "z": baseline_z
            },
            "config": {
                "moonraker_host": config["moonraker_host"],
                "moonraker_port": config["moonraker_port"],
                "probe_type": config.get("probe_type", "btt_eddy"),
                "eddy_sensor_name": config["eddy_sensor_name"],
                "temperature_probe_name": config["temperature_probe_name"],
                "calibration_points": len(cal_by_freq),
                "cartographer_model_loaded": carto_model is not None
            }
        }

    return jsonify(result)


@app.route("/api/history")
def api_history():
    try:
        seconds = float(request.args.get("seconds", 300))
    except ValueError:
        seconds = 300

    cutoff = time.time() - seconds

    with lock:
        points = [
            dict(p)
            for p in history
            if p["wall_time"] >= cutoff
        ]

    return jsonify(points)


@app.route("/api/reset_baseline", methods=["POST"])
def reset_baseline():
    global baseline_frequency
    global baseline_temperature
    global baseline_z

    with lock:
        if not history:
            return jsonify({
                "ok": False,
                "error": "No Eddy data available"
            }), 400

        current = history[-1]
        baseline_frequency = current["frequency"]
        baseline_temperature = current["temperature"]
        baseline_z = current["z"]

    return jsonify({"ok": True})


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        **config,
        "calibration_points": len(cal_by_freq)
    })


@app.route("/api/config", methods=["POST"])
def set_config():
    global baseline_frequency
    global baseline_temperature
    global baseline_z
    global cal_by_freq
    global carto_model
    global active_klipper_ws
    global cartographer_stream_owned

    data = request.get_json(force=True)

    new_config = dict(config)

    if "probe_type" in data:
        probe_type = str(data["probe_type"]).strip().lower()
        if probe_type not in ("btt_eddy", "cartographer"):
            return jsonify({
                "ok": False,
                "error": "Unsupported probe type."
            }), 400
        new_config["probe_type"] = probe_type

    if "moonraker_host" in data:
        new_config["moonraker_host"] = str(data["moonraker_host"]).strip() or "127.0.0.1"

    if "moonraker_port" in data:
        new_config["moonraker_port"] = int(data["moonraker_port"])

    if "eddy_sensor_name" in data:
        new_config["eddy_sensor_name"] = str(data["eddy_sensor_name"]).strip()

    if "temperature_probe_name" in data:
        new_config["temperature_probe_name"] = str(data["temperature_probe_name"]).strip()

    if "cartographer_model_text" in data:
        model_text = str(data["cartographer_model_text"])

        if (
            new_config.get("probe_type", "btt_eddy") == "cartographer"
            and model_text.strip()
        ):
            try:
                parsed_carto_model = parse_cartographer_model_text(model_text)
            except ValueError as e:
                return jsonify({
                    "ok": False,
                    "error": str(e)
                }), 400
        else:
            parsed_carto_model = None

        new_config["cartographer_model_text"] = model_text
    else:
        parsed_carto_model = carto_model

    if "calibration_text" in data:
        text = str(data["calibration_text"])

        if (
            text.strip()
            and new_config.get("probe_type", "btt_eddy") == "btt_eddy"
        ):
            try:
                parsed = parse_calibration_text(text)
            except ValueError as e:
                return jsonify({
                    "ok": False,
                    "error": str(e)
                }), 400
        elif text.strip():
            # Cartographer provides model-derived distance directly
            # through scanner/dump, so this text is stored only for
            # compatibility/reference and is not parsed.
            parsed = []
        else:
            parsed = []

        new_config["calibration_text"] = text
    else:
        parsed = cal_by_freq

    with lock:
        config.update(new_config)

        cal_by_freq = parsed
        carto_model = parsed_carto_model

        # Old points may have been calculated using a different calibration.
        # Clear history and baseline when configuration changes.
        history.clear()
        baseline_frequency = None
        baseline_temperature = None
        baseline_z = None

        save_config()

    # Force the stream worker to reconnect using the new settings.
    if cartographer_stream_owned:
        stop_cartographer_stream()

    try:
        if active_klipper_ws is not None:
            active_klipper_ws.close()
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "calibration_points": len(cal_by_freq),
        "cartographer_model_loaded": carto_model is not None,
        "message": "Settings saved. The Klipper stream will reconnect automatically."
    })


@app.route("/events")
def events():
    def event_stream():
        last_wall_time = 0

        while True:
            point = None

            with lock:
                if history:
                    newest = history[-1]

                    if newest["wall_time"] > last_wall_time:
                        point = dict(newest)
                        last_wall_time = newest["wall_time"]

            if point is not None:
                yield "data: " + json.dumps(point) + "\n\n"

            time.sleep(0.05)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eddy / Cartographer Live Dashboard</title>

<style>
:root { color-scheme: dark; }

body {
    margin: 0;
    background: #101318;
    color: #e8edf2;
    font-family: Arial, Helvetica, sans-serif;
}

.container {
    width: min(1600px, calc(100vw - 24px));
    height: calc(100vh - 18px);
    margin: 0 auto;
    padding: 9px 0;
    box-sizing: border-box;
    display: grid;
    grid-template-rows: auto auto auto minmax(0, 1fr) auto;
    gap: 8px;
    overflow: hidden;
}

h1 {
    margin: 0;
    font-size: 24px;
    line-height: 1.05;
}

.subtitle {
    color: #9ca8b5;
    margin: 2px 0 0;
    font-size: 13px;
}

.dashboard-main {
    min-height: 0;
    display: grid;
    grid-template-columns: 250px minmax(0, 1fr);
    gap: 10px;
}

.metrics {
    min-height: 0;
    display: grid;
    grid-template-columns: 1fr;
    grid-template-rows: repeat(4, minmax(0, 1fr));
    gap: 8px;
    margin: 0;
}

.metric,
.settings-panel,
.chart-panel {
    background: #181d24;
    border: 1px solid #2b333d;
    border-radius: 10px;
}

.metric {
    padding: 12px 14px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
}

.metric-label {
    color: #98a4b2;
    font-size: 12px;
}

.metric-value {
    font-size: 23px;
    font-weight: bold;
    margin-top: 5px;
    white-space: nowrap;
}

.metric-small {
    color: #98a4b2;
    margin-top: 4px;
    font-size: 11px;
}

.controls {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    margin: 0;
}

button,
select,
input,
textarea {
    background: #1d242d;
    color: #e8edf2;
    border: 1px solid #3a4653;
    border-radius: 7px;
    font-size: 14px;
}

button,
select {
    padding: 7px 11px;
}

button:hover {
    background: #27313d;
}

.status {
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 13px;
}

.connected {
    background: #12351f;
    color: #79e29a;
}

.disconnected {
    background: #441b1b;
    color: #ff9090;
}

.settings-panel {
    position: fixed;
    z-index: 20;
    top: 70px;
    left: 50%;
    transform: translateX(-50%);
    width: min(900px, calc(100vw - 32px));
    max-height: calc(100vh - 100px);
    overflow: auto;
    padding: 16px;
    box-sizing: border-box;
}

.settings-panel[hidden] {
    display: none;
}

.settings-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 14px;
}

.settings-title {
    margin: 0;
    font-size: 18px;
}

.settings-close {
    flex: 0 0 auto;
    padding: 6px 10px;
}

.settings-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 12px;
}

.field label {
    display: block;
    color: #aab4bf;
    font-size: 13px;
    margin-bottom: 5px;
}

.field input {
    box-sizing: border-box;
    width: 100%;
    padding: 9px 10px;
}

.calibration-field {
    margin-top: 14px;
}

.calibration-field textarea {
    box-sizing: border-box;
    width: 100%;
    min-height: 220px;
    padding: 10px;
    resize: vertical;
    font-family: monospace;
}

.settings-actions {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 12px;
    flex-wrap: wrap;
}

.message {
    color: #9ca8b5;
    font-size: 13px;
}

.message.ok { color: #79e29a; }
.message.error { color: #ff9090; }

.charts-grid {
    min-height: 0;
    display: grid;
    grid-template-columns: 1fr;
    grid-template-rows: repeat(3, minmax(0, 1fr));
    gap: 8px;
}

.chart-panel {
    padding: 7px 10px 5px;
    margin: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.chart-title {
    font-size: 13px;
    margin-bottom: 2px;
    color: #d8dee5;
    flex: 0 0 auto;
}

canvas {
    display: block;
    width: 100%;
    height: 100%;
    min-height: 0;
    flex: 1 1 auto;
}

.note {
    color: #909ba7;
    font-size: 10px;
    margin: 0;
    line-height: 1.2;
}

@media (max-width: 850px) {
    .container {
        height: auto;
        min-height: 100vh;
        overflow: visible;
        width: calc(100vw - 16px);
    }

    .dashboard-main {
        grid-template-columns: 1fr;
    }

    .metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        grid-template-rows: auto;
    }

    .charts-grid {
        display: block;
    }

    .chart-panel {
        height: 240px;
        margin-bottom: 8px;
    }
}
</style>
</head>

<body>
<div class="container">

<h1>Eddy / Cartographer Live Dashboard</h1>

<div class="subtitle">
Live eddy-current probe frequency, temperature and Z/distance
</div>

<div class="controls">

<span id="connectionStatus" class="status disconnected">
Disconnected
</span>

<label>
Window:
<select id="windowSelect">
    <option value="60">1 minute</option>
    <option value="300" selected>5 minutes</option>
    <option value="1200">20 minutes</option>
    <option value="3600">1 hour</option>
</select>
</label>

<button id="resetBaseline">Reset baseline</button>
<button id="settingsButton">Settings</button>

</div>

<div id="settingsPanel" class="settings-panel" hidden>

<div class="settings-header">
<h2 class="settings-title">Settings</h2>
<button id="closeSettings" class="settings-close" type="button">Close</button>
</div>

<div class="settings-grid">

<div class="field">
<label for="probeType">Probe type</label>
<select id="probeType">
    <option value="btt_eddy">BTT Eddy / Klipper LDC1612</option>
    <option value="cartographer">Cartographer V3 / Scanner plugin</option>
</select>
</div>

<div class="field">
<label for="moonrakerHost">Moonraker host</label>
<input id="moonrakerHost" type="text" placeholder="127.0.0.1">
</div>

<div class="field">
<label for="moonrakerPort">Moonraker port</label>
<input id="moonrakerPort" type="number" min="1" max="65535" value="7125">
</div>

<div class="field">
<label for="eddySensorName">
Eddy sensor name (BTT Eddy only)
</label>
<input id="eddySensorName" type="text" placeholder="btt_eddy">
</div>

<div class="field">
<label for="temperatureProbeName">
Temperature probe name (BTT Eddy only)
</label>
<input id="temperatureProbeName" type="text" placeholder="btt_eddy">
</div>

</div>

<div class="calibration-field" id="bttCalibrationField">
<label for="calibrationText">
BTT Eddy calibration data
</label>

<textarea id="calibrationText"
placeholder="Paste the calibrate = section here. Example:

calibrate =
#*#    0.050000:678437.389,0.090000:678275.407,
#*#    0.130000:678111.331,0.170000:677946.422,"></textarea>
</div>

<div class="calibration-field" id="cartographerModelField" hidden>
<label for="cartographerModelText">
Cartographer scan model
</label>

<textarea id="cartographerModelText"
placeholder="Paste the full [cartographer scan_model default] SAVE_CONFIG block here. Example:

#*# [cartographer scan_model default]
#*# coefficients = 1.414...,1.885...,0.860...
#*# domain = 3.190766648414659e-07,3.338151076700032e-07
#*# z_offset = 0
#*# reference_temperature = 28.82"></textarea>
</div>

<div class="settings-actions">
<button id="saveSettings">Save settings</button>
<span id="settingsMessage" class="message"></span>
</div>

<div class="note">
BTT Eddy uses the pasted <code>calibrate =</code> table.
Cartographer3D Plugin v1.x is read from the Klipper <code>cartographer</code>
status object. Paste the <code>[cartographer scan_model default]</code>
block so the dashboard can convert live frequency into model distance.
The dashboard uses the model coefficients/domain directly; optional
Cartographer coil temperature-compensation calibration is not reproduced.
</div>

</div>

<div class="dashboard-main">

<div class="metrics">

<div class="metric">
    <div class="metric-label">Frequency</div>
    <div class="metric-value" id="frequency">--</div>
    <div class="metric-small" id="frequencyDelta">Δ --</div>
</div>

<div class="metric">
    <div class="metric-label">Probe temperature</div>
    <div class="metric-value" id="temperature">--</div>
    <div class="metric-small" id="temperatureDelta">Δ --</div>
</div>

<div class="metric">
    <div class="metric-label" id="zMetricLabel">Z / probe distance</div>
    <div class="metric-value" id="z">--</div>
    <div class="metric-small" id="zDelta">Δ --</div>
</div>

<div class="metric">
    <div class="metric-label">Frequency drift</div>
    <div class="metric-value" id="ppm">--</div>
    <div class="metric-small">Relative to baseline</div>
</div>

</div>

<div class="charts-grid">

<div class="chart-panel">
<div class="chart-title">Frequency (Hz)</div>
<canvas id="frequencyChart"></canvas>
</div>

<div class="chart-panel">
<div class="chart-title">Probe temperature (°C)</div>
<canvas id="temperatureChart"></canvas>
</div>

<div class="chart-panel">
<div class="chart-title" id="zChartTitle">Z / probe distance (mm)</div>
<canvas id="zChart"></canvas>
</div>

</div>

</div>

<div class="note">
BTT Eddy: Z is interpolated from the calibration pasted into Settings.
Cartographer: Z uses the active Cartographer model's streamed <code>dist</code>
value directly.
</div>

</div>

<script>
const points = [];

let baseline = {
    frequency: null,
    temperature: null,
    z: null
};

let windowSeconds = 300;

const frequencyEl = document.getElementById("frequency");
const temperatureEl = document.getElementById("temperature");
const zEl = document.getElementById("z");

const frequencyDeltaEl = document.getElementById("frequencyDelta");
const temperatureDeltaEl = document.getElementById("temperatureDelta");
const zDeltaEl = document.getElementById("zDelta");
const ppmEl = document.getElementById("ppm");

const statusEl = document.getElementById("connectionStatus");

const settingsPanel = document.getElementById("settingsPanel");
const settingsMessage = document.getElementById("settingsMessage");


function updateMetrics(point) {

    if (!point) return;

    frequencyEl.textContent =
        point.frequency.toFixed(2) + " Hz";

    if (point.temperature !== null) {
        temperatureEl.textContent =
            point.temperature.toFixed(3) + " °C";
    } else {
        temperatureEl.textContent = "--";
    }

    if (point.z !== null) {
        zEl.textContent =
            point.z.toFixed(5) + " mm";
    } else {
        zEl.textContent = "Out of calibration";
    }

    if (baseline.frequency !== null) {

        const df =
            point.frequency - baseline.frequency;

        frequencyDeltaEl.textContent =
            "Δ " + df.toFixed(2) + " Hz";

        const ppm =
            df / baseline.frequency * 1000000;

        ppmEl.textContent =
            ppm.toFixed(1) + " ppm";
    }

    if (
        point.temperature !== null
        && baseline.temperature !== null
    ) {

        const dt =
            point.temperature - baseline.temperature;

        temperatureDeltaEl.textContent =
            "Δ " + dt.toFixed(3) + " °C";
    } else {
        temperatureDeltaEl.textContent = "Δ --";
    }

    if (
        point.z !== null
        && baseline.z !== null
    ) {

        const dz =
            point.z - baseline.z;

        zDeltaEl.textContent =
            "Δ " + (dz * 1000).toFixed(1) + " µm";
    } else {
        zDeltaEl.textContent = "Δ --";
    }
}


function trimPoints() {

    const cutoff =
        Date.now() / 1000 - windowSeconds - 10;

    while (
        points.length > 0
        && points[0].wall_time < cutoff
    ) {
        points.shift();
    }
}


function visiblePoints() {

    const cutoff =
        Date.now() / 1000 - windowSeconds;

    return points.filter(
        p => p.wall_time >= cutoff
    );
}


function resizeCanvas(canvas) {

    const ratio =
        window.devicePixelRatio || 1;

    const rect =
        canvas.getBoundingClientRect();

    const width =
        Math.max(100, rect.width);

    const height =
        Math.max(100, rect.height);

    if (
        canvas.width !== Math.floor(width * ratio)
        ||
        canvas.height !== Math.floor(height * ratio)
    ) {

        canvas.width =
            Math.floor(width * ratio);

        canvas.height =
            Math.floor(height * ratio);
    }

    const ctx =
        canvas.getContext("2d");

    ctx.setTransform(
        ratio, 0, 0, ratio, 0, 0
    );

    return {ctx, width, height};
}


function drawChart(
    canvas,
    data,
    field,
    color,
    digits
) {

    const {ctx, width, height} =
        resizeCanvas(canvas);

    ctx.clearRect(0, 0, width, height);

    const left = 75;
    const right = 15;
    const top = 10;
    const bottom = 28;

    const plotWidth =
        width - left - right;

    const plotHeight =
        height - top - bottom;

    const valid =
        data.filter(
            p =>
                p[field] !== null
                && p[field] !== undefined
        );

    if (valid.length < 2) {

        ctx.fillStyle = "#8b96a2";
        ctx.font = "14px Arial";
        ctx.fillText(
            "Waiting for data...",
            left,
            top + 30
        );

        return;
    }

    const now =
        Date.now() / 1000;

    const start =
        now - windowSeconds;

    let min =
        Math.min(...valid.map(p => p[field]));

    let max =
        Math.max(...valid.map(p => p[field]));

    let span = max - min;

    if (span === 0) {
        span = Math.max(
            Math.abs(max) * 0.00001,
            0.001
        );
    }

    min -= span * 0.10;
    max += span * 0.10;

    ctx.strokeStyle = "#28313a";
    ctx.lineWidth = 1;

    ctx.fillStyle = "#8f9aa6";
    ctx.font = "11px Arial";

    const ySteps = 5;

    for (let i = 0; i <= ySteps; i++) {

        const y =
            top + plotHeight * i / ySteps;

        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(left + plotWidth, y);
        ctx.stroke();

        const value =
            max - (max - min) * i / ySteps;

        ctx.fillText(
            value.toFixed(digits),
            5,
            y + 4
        );
    }

    const xSteps = 5;

    for (let i = 0; i <= xSteps; i++) {

        const x =
            left + plotWidth * i / xSteps;

        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, top + plotHeight);
        ctx.stroke();

        const secondsAgo =
            windowSeconds
            - windowSeconds * i / xSteps;

        let label;

        if (windowSeconds <= 300) {
            label =
                "-" + Math.round(secondsAgo) + "s";
        } else {
            label =
                "-"
                + (secondsAgo / 60).toFixed(0)
                + "m";
        }

        ctx.fillText(
            label,
            x - 12,
            height - 6
        );
    }

    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();

    let started = false;

    for (const p of valid) {

        if (p.wall_time < start) continue;

        const x =
            left
            + ((p.wall_time - start) / windowSeconds)
            * plotWidth;

        const y =
            top
            + (
                1
                - ((p[field] - min) / (max - min))
            )
            * plotHeight;

        if (!started) {
            ctx.moveTo(x, y);
            started = true;
        } else {
            ctx.lineTo(x, y);
        }
    }

    ctx.stroke();
}


const frequencyCanvas =
    document.getElementById("frequencyChart");

const temperatureCanvas =
    document.getElementById("temperatureChart");

const zCanvas =
    document.getElementById("zChart");


function redraw() {

    trimPoints();

    const data =
        visiblePoints();

    drawChart(
        frequencyCanvas,
        data,
        "frequency",
        "#4f8cff",
        1
    );

    drawChart(
        temperatureCanvas,
        data,
        "temperature",
        "#ff6262",
        3
    );

    drawChart(
        zCanvas,
        data,
        "z",
        "#58d17b",
        4
    );

    requestAnimationFrame(redraw);
}


function updateProbeTypeUI() {

    const type =
        document.getElementById("probeType").value;

    const isCartographer =
        type === "cartographer";

    document.getElementById(
        "eddySensorName"
    ).disabled = isCartographer;

    document.getElementById(
        "temperatureProbeName"
    ).disabled = isCartographer;

    document.getElementById(
        "bttCalibrationField"
    ).hidden = isCartographer;

    document.getElementById(
        "cartographerModelField"
    ).hidden = !isCartographer;

    document.getElementById(
        "zMetricLabel"
    ).textContent =
        isCartographer
        ? "Cartographer distance"
        : "Calibration-equivalent Z";

    document.getElementById(
        "zChartTitle"
    ).textContent =
        isCartographer
        ? "Cartographer model distance (mm)"
        : "Calibration-equivalent Z (mm)";
}


async function loadConfigForm() {

    const response =
        await fetch("/api/config");

    const cfg =
        await response.json();

    document.getElementById(
        "probeType"
    ).value = cfg.probe_type || "btt_eddy";

    updateProbeTypeUI();

    document.getElementById(
        "moonrakerHost"
    ).value = cfg.moonraker_host || "";

    document.getElementById(
        "moonrakerPort"
    ).value = cfg.moonraker_port || 7125;

    document.getElementById(
        "eddySensorName"
    ).value = cfg.eddy_sensor_name || "";

    document.getElementById(
        "temperatureProbeName"
    ).value =
        cfg.temperature_probe_name || "";

    document.getElementById(
        "calibrationText"
    ).value =
        cfg.calibration_text || "";

    document.getElementById(
        "cartographerModelText"
    ).value =
        cfg.cartographer_model_text || "";

    if (cfg.probe_type === "cartographer") {
        settingsMessage.textContent =
            cfg.cartographer_model_loaded
            ? "Cartographer scan model loaded"
            : "Paste the Cartographer scan model below";
    } else {
        settingsMessage.textContent =
            cfg.calibration_points
            + " calibration points loaded";
    }
}


async function loadInitialData() {

    const statusResponse =
        await fetch("/api/status");

    const status =
        await statusResponse.json();

    baseline =
        status.baseline;

    if (status.connected) {
        statusEl.textContent = "Connected";
        statusEl.className =
            "status connected";
    } else {
        statusEl.textContent = "Disconnected";
        statusEl.className =
            "status disconnected";
    }

    const historyResponse =
        await fetch(
            "/api/history?seconds="
            + windowSeconds
        );

    const history =
        await historyResponse.json();

    points.length = 0;

    for (const p of history) {
        points.push(p);
    }

    if (points.length) {
        updateMetrics(
            points[points.length - 1]
        );
    }
}


const source =
    new EventSource("/events");

source.onmessage = event => {

    const point =
        JSON.parse(event.data);

    points.push(point);

    updateMetrics(point);

    statusEl.textContent = "Connected";
    statusEl.className =
        "status connected";
};

source.onerror = () => {

    statusEl.textContent =
        "Stream reconnecting...";

    statusEl.className =
        "status disconnected";
};


document.getElementById(
    "windowSelect"
).addEventListener(
    "change",
    async event => {

        windowSeconds =
            Number(event.target.value);

        await loadInitialData();
    }
);


document.getElementById(
    "resetBaseline"
).addEventListener(
    "click",
    async () => {

        await fetch(
            "/api/reset_baseline",
            {method: "POST"}
        );

        const response =
            await fetch("/api/status");

        const status =
            await response.json();

        baseline =
            status.baseline;

        updateMetrics(
            status.current
        );
    }
);


document.getElementById(
    "settingsButton"
).addEventListener(
    "click",
    async () => {

        settingsPanel.hidden =
            !settingsPanel.hidden;

        if (!settingsPanel.hidden) {
            await loadConfigForm();
        }
    }
);


document.getElementById(
    "closeSettings"
).addEventListener(
    "click",
    () => {
        settingsPanel.hidden = true;
    }
);

document.addEventListener(
    "keydown",
    event => {
        if (
            event.key === "Escape"
            && !settingsPanel.hidden
        ) {
            settingsPanel.hidden = true;
        }
    }
);


document.getElementById(
    "probeType"
).addEventListener(
    "change",
    updateProbeTypeUI
);


document.getElementById(
    "saveSettings"
).addEventListener(
    "click",
    async () => {

        settingsMessage.textContent =
            "Saving...";

        settingsMessage.className =
            "message";

        const payload = {
            probe_type:
                document.getElementById(
                    "probeType"
                ).value,

            moonraker_host:
                document.getElementById(
                    "moonrakerHost"
                ).value.trim(),

            moonraker_port:
                Number(
                    document.getElementById(
                        "moonrakerPort"
                    ).value
                ),

            eddy_sensor_name:
                document.getElementById(
                    "eddySensorName"
                ).value.trim(),

            temperature_probe_name:
                document.getElementById(
                    "temperatureProbeName"
                ).value.trim(),

            calibration_text:
                document.getElementById(
                    "calibrationText"
                ).value,

            cartographer_model_text:
                document.getElementById(
                    "cartographerModelText"
                ).value
        };

        const response =
            await fetch(
                "/api/config",
                {
                    method: "POST",
                    headers: {
                        "Content-Type":
                            "application/json"
                    },
                    body: JSON.stringify(payload)
                }
            );

        const result =
            await response.json();

        if (!response.ok) {

            settingsMessage.textContent =
                result.error
                || "Could not save settings";

            settingsMessage.className =
                "message error";

            return;
        }

        if (
            document.getElementById("probeType").value
            === "cartographer"
        ) {
            settingsMessage.textContent =
                "Cartographer settings saved";
        } else {
            settingsMessage.textContent =
                result.calibration_points
                + " calibration points saved";
        }

        settingsMessage.className =
            "message ok";

        points.length = 0;

        await loadInitialData();
    }
);


loadInitialData();
requestAnimationFrame(redraw);
</script>

</body>
</html>
"""


@app.route("/")
def index():
    return HTML


if __name__ == "__main__":
    load_config()

    try:
        rebuild_calibration()
    except Exception as e:
        print("Saved calibration is invalid:", e)
        cal_by_freq = []

    try:
        rebuild_cartographer_model()
    except Exception as e:
        print("Saved Cartographer model is invalid:", e)
        carto_model = None

    threading.Thread(
        target=temperature_worker,
        daemon=True
    ).start()

    threading.Thread(
        target=klipper_worker,
        daemon=True
    ).start()

    print()
    print(
        "Eddy dashboard starting on port",
        config["web_port"]
    )
    print()

    app.run(
        host=config["web_host"],
        port=int(config["web_port"]),
        threaded=True,
        debug=False
    )
