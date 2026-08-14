#!/usr/bin/env python3
import atexit
import csv
import json
import logging
import socket
import math
import os
import statistics
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

import websocket
from flask import Flask, Response, jsonify, request, send_from_directory

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "eddy_dashboard_config.json")
RECORDINGS_DIR = os.path.join(APP_DIR, "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

DEFAULT_CONFIG = {
    "moonraker_host": "127.0.0.1",
    "moonraker_port": 7125,
    "probe_type": "btt_eddy",
    "eddy_sensor_name": "btt_eddy",
    "temperature_probe_name": "btt_eddy",
    "cartographer_model_text": "",
    "auto_detect": True,
    "average_samples": 1,
    "frequency_color": "#4f8cff",
    "temperature_color": "#ff6262",
    "z_color": "#58d17b",
    "web_host": "0.0.0.0",
    "web_port": 8085,
    "calibration_text": ""
}

MAX_HISTORY = 50000

app = Flask(__name__)

# The browser polls several lightweight status endpoints. Werkzeug logs every
# successful request by default, which makes the terminal extremely noisy.
# Keep warnings/errors but suppress normal GET/POST access lines.
logging.getLogger("werkzeug").setLevel(logging.ERROR)
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

raw_average_buffer = deque(maxlen=500)

recording_lock = threading.Lock()
recording_active = False
recording_file = None
recording_writer = None
recording_filename = None
last_recording_filename = None
recording_started_at = None

test_active = False
test_started_at = None
test_stopped_at = None


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


def moonraker_get_json(path, timeout=3):
    with urllib.request.urlopen(
        moonraker_http_url(path),
        timeout=timeout
    ) as response:
        return json.loads(response.read().decode())


def get_loaded_objects():
    data = moonraker_get_json("/printer/objects/list")
    return data.get("result", {}).get("objects", [])


def get_configfile_raw():
    encoded = urllib.parse.quote("configfile", safe="")
    data = moonraker_get_json(
        f"/printer/objects/query?{encoded}"
    )
    return (
        data.get("result", {})
            .get("status", {})
            .get("configfile", {})
            .get("config", {})
    )


def make_btt_calibration_text(section_name, section):
    calibrate = section.get("calibrate")
    if calibrate is None:
        return ""

    if isinstance(calibrate, list):
        calibrate = ",".join(str(x) for x in calibrate)
    else:
        calibrate = str(calibrate)

    return (
        f"#*# [{section_name}]\n"
        "#*# calibrate =\n"
        + "\n".join(
            "#*#   " + line
            for line in calibrate.splitlines()
        )
    )


def make_cartographer_model_text(section_name, section):
    keys = [
        "coefficients",
        "domain",
        "z_offset",
        "reference_temperature",
        "software_version",
        "mcu_version"
    ]

    lines = [f"#*# [{section_name}]"]

    for key in keys:
        if key not in section:
            continue

        value = section[key]

        if isinstance(value, list):
            value = ",".join(str(x) for x in value)

        lines.append(f"#*# {key} = {value}")

    return "\n".join(lines)


def auto_detect_configuration(save=True):
    """
    Detect supported probes from Klipper objects and pull raw calibration/model
    data from the configfile object when available.

    Cartographer is preferred if both are present because only one probe mode
    can be displayed at a time.
    """
    global carto_model
    global cal_by_freq

    objects = get_loaded_objects()
    raw_config = {}

    try:
        raw_config = get_configfile_raw()
    except Exception as e:
        print("Configfile discovery warning:", e)

    detected = {
        "found": False,
        "probe_type": None,
        "message": "No supported probe detected."
    }

    if "cartographer" in objects:
        config["probe_type"] = "cartographer"
        detected["found"] = True
        detected["probe_type"] = "cartographer"
        detected["message"] = "Detected Cartographer."

        # Prefer the active scan model if Klipper reports one.
        current_model = "default"
        try:
            cdata = moonraker_get_json(
                "/printer/objects/query?cartographer"
            )
            current_model = (
                cdata["result"]["status"]["cartographer"]
                     .get("scan", {})
                     .get("current_model")
                or "default"
            )
        except Exception:
            pass

        preferred = f"cartographer scan_model {current_model}"
        model_sections = [
            name for name in raw_config
            if name.startswith("cartographer scan_model ")
        ]

        section_name = None

        if preferred in raw_config:
            section_name = preferred
        elif model_sections:
            section_name = sorted(model_sections)[0]

        if section_name:
            config["cartographer_model_text"] = (
                make_cartographer_model_text(
                    section_name,
                    raw_config[section_name]
                )
            )

            try:
                carto_model = parse_cartographer_model_text(
                    config["cartographer_model_text"]
                )
                detected["model"] = section_name
            except Exception as e:
                detected["model_error"] = str(e)

    else:
        eddy_objects = sorted(
            obj for obj in objects
            if obj.startswith("probe_eddy_current ")
        )

        if eddy_objects:
            object_name = eddy_objects[0]
            sensor_name = object_name.split(" ", 1)[1]

            config["probe_type"] = "btt_eddy"
            config["eddy_sensor_name"] = sensor_name
            detected["found"] = True
            detected["probe_type"] = "btt_eddy"
            detected["sensor_name"] = sensor_name
            detected["message"] = f"Detected BTT/Klipper Eddy: {sensor_name}"

            temp_object = f"temperature_probe {sensor_name}"

            if temp_object in objects:
                config["temperature_probe_name"] = sensor_name
            else:
                config["temperature_probe_name"] = ""

            section = raw_config.get(object_name, {})

            if section.get("calibrate") is not None:
                config["calibration_text"] = (
                    make_btt_calibration_text(
                        object_name,
                        section
                    )
                )

                try:
                    cal_by_freq = parse_calibration_text(
                        config["calibration_text"]
                    )
                    detected["calibration_points"] = len(cal_by_freq)
                except Exception as e:
                    detected["calibration_error"] = str(e)

    if save and detected["found"]:
        save_config()

    return detected


def finite_values(values):
    return [
        float(v) for v in values
        if v is not None and math.isfinite(float(v))
    ]


def linear_slope_per_minute(points, field):
    pairs = [
        (p["wall_time"], p.get(field))
        for p in points
        if p.get(field) is not None
    ]

    if len(pairs) < 2:
        return None

    t0 = pairs[0][0]
    xs = [(t - t0) / 60.0 for t, _ in pairs]
    ys = [float(v) for _, v in pairs]

    xmean = sum(xs) / len(xs)
    ymean = sum(ys) / len(ys)

    denom = sum((x - xmean) ** 2 for x in xs)

    if denom == 0:
        return None

    return (
        sum(
            (x - xmean) * (y - ymean)
            for x, y in zip(xs, ys)
        )
        / denom
    )


def stats_for_points(points):
    if not points:
        return {}

    freqs = finite_values(
        [p.get("frequency") for p in points]
    )
    temps = finite_values(
        [p.get("temperature") for p in points]
    )
    zs = finite_values(
        [p.get("z") for p in points]
    )

    f_slope = linear_slope_per_minute(
        points,
        "frequency"
    )
    z_slope = linear_slope_per_minute(
        points,
        "z"
    )

    baseline = freqs[0] if freqs else None

    def group(values):
        if not values:
            return None

        return {
            "min": min(values),
            "max": max(values),
            "mean": sum(values) / len(values),
            "stddev": (
                statistics.pstdev(values)
                if len(values) > 1
                else 0.0
            )
        }

    return {
        "count": len(points),
        "duration_seconds": (
            points[-1]["wall_time"]
            - points[0]["wall_time"]
        ),
        "frequency": group(freqs),
        "temperature": group(temps),
        "z": group(zs),
        "frequency_hz_per_min": f_slope,
        "frequency_ppm_per_min": (
            f_slope / baseline * 1_000_000
            if f_slope is not None
            and baseline not in (None, 0)
            else None
        ),
        "z_um_per_min": (
            z_slope * 1000.0
            if z_slope is not None
            else None
        )
    }


def safe_recording_name(value):
    value = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        value or ""
    ).strip("._")

    return value[:80]



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

    raw_point = {
        "sensor_time": sensor_time,
        "wall_time": time.time(),
        "frequency": float(frequency),
        "temperature": (
            float(temp)
            if temp is not None
            else None
        ),
        "z": (
            float(z)
            if z is not None
            else None
        )
    }

    raw_average_buffer.append(raw_point)

    try:
        average_samples = int(
            config.get("average_samples", 1)
        )
    except Exception:
        average_samples = 1

    average_samples = max(
        1,
        min(200, average_samples)
    )

    selected = list(raw_average_buffer)[-average_samples:]

    def mean_available(key):
        values = [
            p[key]
            for p in selected
            if p.get(key) is not None
        ]

        if not values:
            return None

        return sum(values) / len(values)

    point = {
        "sensor_time": raw_point["sensor_time"],
        "wall_time": raw_point["wall_time"],
        "frequency": mean_available("frequency"),
        "temperature": mean_available("temperature"),
        "z": mean_available("z")
    }

    with lock:
        if baseline_frequency is None:
            baseline_frequency = point["frequency"]

        if (
            baseline_temperature is None
            and point["temperature"] is not None
        ):
            baseline_temperature = point["temperature"]

        if baseline_z is None and point["z"] is not None:
            baseline_z = point["z"]

        history.append(point)

    with recording_lock:
        if recording_active and recording_writer is not None:
            recording_writer.writerow([
                time.strftime(
                    "%Y-%m-%dT%H:%M:%S",
                    time.localtime(point["wall_time"])
                ),
                f'{point["wall_time"]:.6f}',
                f'{point["sensor_time"]:.6f}',
                f'{point["frequency"]:.6f}',
                (
                    f'{point["temperature"]:.6f}'
                    if point["temperature"] is not None
                    else ""
                ),
                (
                    f'{point["z"]:.9f}'
                    if point["z"] is not None
                    else ""
                ),
                config.get("probe_type", "")
            ])

            try:
                recording_file.flush()
            except Exception:
                pass


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
    Start the Cartographer plugin's diagnostic stream.

    The current Cartographer3D plugin only keeps mcu.last_sample updating
    continuously while a stream session is active.
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
        message = str(e)

        if (
            "Stream is already active" in message
            or "already active" in message.lower()
        ):
            # Most commonly this is a stream left behind when an earlier
            # dashboard process was killed before cleanup could run. The
            # current plugin explicitly instructs ACTION=STOP in this case.
            try:
                run_gcode_script("CARTOGRAPHER_STREAM ACTION=STOP")
                time.sleep(0.10)
                run_gcode_script("CARTOGRAPHER_STREAM ACTION=START")
                cartographer_stream_owned = True
                print("Recovered existing Cartographer stream")
                return True
            except Exception as recovery_error:
                print(
                    "Could not recover Cartographer stream:",
                    recovery_error
                )
                return False

        print("Could not start Cartographer stream:", e)
        return False


def stop_cartographer_stream():
    """Stop only the Cartographer stream session owned by this dashboard."""
    global cartographer_stream_owned

    if not cartographer_stream_owned:
        return

    try:
        run_gcode_script("CARTOGRAPHER_STREAM ACTION=STOP")
        print("Cartographer live stream stopped")
    except Exception as e:
        print("Could not stop Cartographer stream:", e)
    finally:
        cartographer_stream_owned = False



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
    consecutive_read_errors = 0

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

                consecutive_read_errors = 0

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
                consecutive_read_errors += 1

                # A single HTTP timeout can occur while Moonraker/Klipper is
                # briefly busy. Do not spam the console for isolated events.
                # Report only when the problem persists.
                if consecutive_read_errors == 5:
                    print(
                        "Cartographer status read issue persists:",
                        e
                    )
                elif (
                    consecutive_read_errors > 5
                    and consecutive_read_errors % 20 == 0
                ):
                    print(
                        "Cartographer status still unavailable:",
                        e
                    )

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
                "cartographer_model_loaded": carto_model is not None,
                "average_samples": int(config.get("average_samples", 1)),
                "frequency_color": config.get("frequency_color", "#4f8cff"),
                "temperature_color": config.get("temperature_color", "#ff6262"),
                "z_color": config.get("z_color", "#58d17b")
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
        "calibration_points": len(cal_by_freq),
        "cartographer_model_loaded": carto_model is not None
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

    if "auto_detect" in data:
        new_config["auto_detect"] = bool(data["auto_detect"])

    if "average_samples" in data:
        try:
            avg = int(data["average_samples"])
        except Exception:
            avg = 1
        new_config["average_samples"] = max(1, min(200, avg))

    color_pattern = re.compile(r"^#[0-9a-fA-F]{6}$")

    for key, default in (
        ("frequency_color", "#4f8cff"),
        ("temperature_color", "#ff6262"),
        ("z_color", "#58d17b")
    ):
        if key in data:
            color = str(data[key]).strip()
            new_config[key] = (
                color
                if color_pattern.match(color)
                else default
            )

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
        raw_average_buffer.clear()
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


@app.route("/api/detect", methods=["POST"])
def api_detect():
    global baseline_frequency
    global baseline_temperature
    global baseline_z

    try:
        result = auto_detect_configuration(save=True)

        # Rebuild whichever model was detected.
        if config.get("probe_type") == "cartographer":
            rebuild_cartographer_model()
        else:
            rebuild_calibration()

        with lock:
            history.clear()
            raw_average_buffer.clear()
            baseline_frequency = None
            baseline_temperature = None
            baseline_z = None

        try:
            if active_klipper_ws is not None:
                active_klipper_ws.close()
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "detected": result,
            "config": config
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@app.route("/api/stats")
def api_stats():
    try:
        seconds = float(
            request.args.get("seconds", 300)
        )
    except ValueError:
        seconds = 300

    cutoff = time.time() - seconds

    with lock:
        points = [
            dict(p)
            for p in history
            if p["wall_time"] >= cutoff
        ]

    return jsonify(stats_for_points(points))


@app.route("/api/recording/status")
def recording_status():
    with recording_lock:
        return jsonify({
            "active": recording_active,
            "filename": recording_filename,
            "last_filename": last_recording_filename,
            "started_at": recording_started_at
        })


@app.route("/api/recording/start", methods=["POST"])
def recording_start():
    global recording_active
    global recording_file
    global recording_writer
    global recording_filename
    global recording_started_at

    data = request.get_json(silent=True) or {}
    label = safe_recording_name(
        data.get("label", "")
    )

    stamp = time.strftime(
        "%Y%m%d_%H%M%S"
    )

    filename = (
        f"{stamp}_{label}.csv"
        if label
        else f"{stamp}.csv"
    )

    path = os.path.join(
        RECORDINGS_DIR,
        filename
    )

    with recording_lock:
        if recording_active:
            return jsonify({
                "ok": False,
                "error": "Recording is already active."
            }), 409

        recording_file = open(
            path,
            "w",
            newline="",
            encoding="utf-8"
        )
        recording_writer = csv.writer(
            recording_file
        )
        recording_writer.writerow([
            "wall_time_iso",
            "wall_time_unix",
            "sensor_time",
            "frequency_hz",
            "temperature_c",
            "z_or_distance_mm",
            "probe_type"
        ])

        recording_filename = filename
        recording_started_at = time.time()
        recording_active = True

    return jsonify({
        "ok": True,
        "filename": filename
    })


@app.route("/api/recording/stop", methods=["POST"])
def recording_stop():
    global recording_active
    global recording_file
    global recording_writer
    global recording_filename
    global last_recording_filename
    global recording_started_at

    with recording_lock:
        if not recording_active:
            return jsonify({
                "ok": False,
                "error": "No recording is active."
            }), 409

        finished = recording_filename

        try:
            recording_file.flush()
            recording_file.close()
        except Exception:
            pass

        last_recording_filename = finished
        recording_active = False
        recording_file = None
        recording_writer = None
        recording_filename = None
        recording_started_at = None

    return jsonify({
        "ok": True,
        "filename": finished
    })


@app.route("/api/recordings")
def recordings_list():
    runs = []

    for filename in sorted(
        os.listdir(RECORDINGS_DIR),
        reverse=True
    ):
        if not filename.lower().endswith(".csv"):
            continue

        path = os.path.join(
            RECORDINGS_DIR,
            filename
        )

        try:
            with open(
                path,
                "r",
                encoding="utf-8",
                newline=""
            ) as f:
                rows = list(csv.DictReader(f))

            if not rows:
                continue

            start = float(
                rows[0]["wall_time_unix"]
            )
            end = float(
                rows[-1]["wall_time_unix"]
            )

            runs.append({
                "filename": filename,
                "points": len(rows),
                "duration_seconds": end - start,
                "probe_type": rows[0].get(
                    "probe_type",
                    ""
                ),
                "start_iso": rows[0].get(
                    "wall_time_iso",
                    ""
                )
            })

        except Exception as e:
            runs.append({
                "filename": filename,
                "error": str(e)
            })

    return jsonify(runs)


@app.route("/api/recordings/download/<path:filename>")
def recording_download(filename):
    filename = os.path.basename(filename)

    return send_from_directory(
        RECORDINGS_DIR,
        filename,
        as_attachment=True
    )


@app.route("/api/recordings/data/<path:filename>")
def recording_data(filename):
    filename = os.path.basename(filename)
    path = os.path.join(
        RECORDINGS_DIR,
        filename
    )

    if not os.path.isfile(path):
        return jsonify({
            "error": "Recording not found."
        }), 404

    with open(
        path,
        "r",
        encoding="utf-8",
        newline=""
    ) as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return jsonify([])

    # Keep comparison payloads practical for the browser.
    stride = max(
        1,
        math.ceil(len(rows) / 1500)
    )

    selected = rows[::stride]

    if selected[-1] != rows[-1]:
        selected.append(rows[-1])

    f0 = float(rows[0]["frequency_hz"])
    z0 = (
        float(rows[0]["z_or_distance_mm"])
        if rows[0].get("z_or_distance_mm")
        else None
    )
    t0 = (
        float(rows[0]["temperature_c"])
        if rows[0].get("temperature_c")
        else None
    )
    wall0 = float(rows[0]["wall_time_unix"])

    result = []

    for row in selected:
        wall = float(row["wall_time_unix"])
        freq = float(row["frequency_hz"])
        z = (
            float(row["z_or_distance_mm"])
            if row.get("z_or_distance_mm")
            else None
        )
        temp = (
            float(row["temperature_c"])
            if row.get("temperature_c")
            else None
        )

        result.append({
            "minutes": (wall - wall0) / 60.0,
            "frequency_delta_hz": freq - f0,
            "ppm": (
                (freq - f0) / f0 * 1_000_000
                if f0 != 0
                else None
            ),
            "z_delta_um": (
                (z - z0) * 1000.0
                if z is not None
                and z0 is not None
                else None
            ),
            "temperature_delta_c": (
                temp - t0
                if temp is not None
                and t0 is not None
                else None
            )
        })

    return jsonify(result)


@app.route("/api/test/status")
def test_status():
    now = time.time()

    return jsonify({
        "active": test_active,
        "started_at": test_started_at,
        "stopped_at": test_stopped_at,
        "elapsed_seconds": (
            now - test_started_at
            if test_active and test_started_at
            else (
                test_stopped_at - test_started_at
                if test_started_at and test_stopped_at
                else 0
            )
        )
    })


@app.route("/api/test/start", methods=["POST"])
def test_start():
    global test_active
    global test_started_at
    global test_stopped_at
    global baseline_frequency
    global baseline_temperature
    global baseline_z

    with lock:
        if history:
            current = history[-1]
            baseline_frequency = current["frequency"]
            baseline_temperature = current["temperature"]
            baseline_z = current["z"]

    test_started_at = time.time()
    test_stopped_at = None
    test_active = True

    return jsonify({"ok": True})


@app.route("/api/test/stop", methods=["POST"])
def test_stop():
    global test_active
    global test_stopped_at

    if test_active:
        test_stopped_at = time.time()

    test_active = False

    return jsonify({"ok": True})


@app.route("/api/test/reset", methods=["POST"])
def test_reset():
    global test_active
    global test_started_at
    global test_stopped_at

    test_active = False
    test_started_at = None
    test_stopped_at = None

    return jsonify({"ok": True})



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

* { box-sizing: border-box; }

body {
    margin: 0;
    background: #101318;
    color: #e8edf2;
    font-family: Arial, Helvetica, sans-serif;
    overflow: hidden;
}

button,
select,
input,
textarea {
    font: inherit;
}

button,
select,
input {
    background: #1d242d;
    color: #e8edf2;
    border: 1px solid #3a4653;
    border-radius: 7px;
}

button,
select {
    padding: 7px 10px;
}

button {
    cursor: pointer;
}

button:hover {
    background: #27313d;
}

button.danger {
    border-color: #7d3f3f;
}

.container {
    width: min(1700px, calc(100vw - 20px));
    height: calc(100vh - 16px);
    margin: 0 auto;
    padding: 8px 0;
    display: grid;
    grid-template-rows: auto auto minmax(0, 1fr) auto;
    gap: 7px;
}

.topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}

.title-wrap {
    min-width: 0;
}

h1 {
    margin: 0;
    font-size: 23px;
    line-height: 1.05;
}

.subtitle {
    margin-top: 2px;
    color: #9ca8b5;
    font-size: 12px;
}

.toolbar {
    display: flex;
    align-items: center;
    gap: 7px;
    flex-wrap: wrap;
    justify-content: flex-end;
}

.status-chip {
    padding: 6px 9px;
    border-radius: 7px;
    font-size: 12px;
    border: 1px solid #3a4653;
    white-space: nowrap;
}

.connected {
    background: #12351f;
    color: #79e29a;
}

.disconnected {
    background: #441b1b;
    color: #ff9090;
}

.recording {
    background: #481818;
    color: #ff9b9b;
}

.timer-chip {
    min-width: 78px;
    text-align: center;
}

.menu {
    position: relative;
}

.menu summary {
    list-style: none;
    cursor: pointer;
    user-select: none;
    background: #1d242d;
    color: #e8edf2;
    border: 1px solid #3a4653;
    border-radius: 7px;
    padding: 7px 10px;
}

.menu summary::-webkit-details-marker {
    display: none;
}

.menu-panel {
    position: absolute;
    right: 0;
    top: calc(100% + 5px);
    z-index: 30;
    min-width: 190px;
    background: #181d24;
    border: 1px solid #35404c;
    border-radius: 9px;
    padding: 6px;
}

.menu-panel button {
    width: 100%;
    text-align: left;
    border: 0;
    background: transparent;
    padding: 8px 9px;
}

.window-control {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    color: #9ca8b5;
}

.dashboard-main {
    min-height: 0;
    display: grid;
    grid-template-columns: 245px minmax(0, 1fr);
    gap: 9px;
}

.sidebar {
    min-height: 0;
    display: grid;
    grid-template-rows: repeat(4, minmax(0, 1fr)) auto;
    gap: 7px;
}

.metric {
    min-height: 0;
    background: #181d24;
    border: 1px solid #2b333d;
    border-radius: 9px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    justify-content: center;
}

.metric-label {
    color: #98a4b2;
    font-size: 11px;
}

.metric-value {
    font-size: 21px;
    font-weight: bold;
    margin-top: 4px;
    white-space: nowrap;
}

.metric-small {
    color: #98a4b2;
    margin-top: 3px;
    font-size: 11px;
}

.quick-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
}

.quick-actions button {
    padding: 6px 5px;
    font-size: 11px;
}

.charts-grid {
    min-height: 0;
    display: grid;
    grid-template-rows: repeat(3, minmax(0, 1fr));
    gap: 7px;
}

.chart-panel {
    min-height: 0;
    background: #181d24;
    border: 1px solid #2b333d;
    border-radius: 9px;
    padding: 6px 9px 4px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.chart-title {
    font-size: 12px;
    color: #d8dee5;
    margin-bottom: 1px;
}

canvas {
    display: block;
    width: 100%;
    height: 100%;
    min-height: 0;
    flex: 1 1 auto;
}

.footer-note {
    color: #7f8a96;
    font-size: 10px;
    line-height: 1.1;
}

.modal {
    position: fixed;
    inset: 0;
    z-index: 50;
    background: rgba(0,0,0,.58);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 18px;
}

.modal[hidden] {
    display: none;
}

.modal-card {
    width: min(980px, 96vw);
    max-height: 92vh;
    overflow: auto;
    background: #181d24;
    border: 1px solid #3a4653;
    border-radius: 11px;
    padding: 15px;
}

.modal-card.wide {
    width: min(1250px, 97vw);
}

.modal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 13px;
}

.modal-head h2 {
    margin: 0;
    font-size: 18px;
}

.settings-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(180px, 1fr));
    gap: 11px;
}

.field label {
    display: block;
    color: #aab4bf;
    font-size: 12px;
    margin-bottom: 4px;
}

.field input,
.field select {
    width: 100%;
    padding: 8px 9px;
}

.checkbox-field {
    display: flex;
    align-items: center;
    gap: 7px;
    padding-top: 20px;
    font-size: 12px;
}

.calibration-field {
    margin-top: 12px;
}

.calibration-field textarea {
    width: 100%;
    min-height: 170px;
    resize: vertical;
    background: #12161b;
    color: #e8edf2;
    border: 1px solid #3a4653;
    border-radius: 7px;
    padding: 9px;
    font-family: monospace;
}

.settings-actions,
.modal-actions {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    margin-top: 12px;
}

.message {
    color: #9ca8b5;
    font-size: 12px;
}

.message.ok { color: #79e29a; }
.message.error { color: #ff9090; }

.stats-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 9px;
}

.stat-group {
    background: #12161b;
    border: 1px solid #2b333d;
    border-radius: 8px;
    padding: 10px;
}

.stat-group h3 {
    margin: 0 0 8px;
    font-size: 13px;
}

.stat-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    font-size: 12px;
    margin: 5px 0;
}

.runs-layout {
    display: grid;
    grid-template-columns: 320px minmax(0,1fr);
    gap: 12px;
}

.run-list {
    max-height: 520px;
    overflow: auto;
    border: 1px solid #2b333d;
    border-radius: 8px;
    padding: 6px;
}

.run-item {
    border-bottom: 1px solid #29313a;
    padding: 8px 4px;
    font-size: 12px;
}

.run-item:last-child {
    border-bottom: 0;
}

.run-meta {
    color: #8e99a5;
    font-size: 11px;
    margin-top: 3px;
}

.compare-wrap {
    min-height: 420px;
    display: flex;
    flex-direction: column;
}

.compare-toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 7px;
}

#comparisonChart {
    width: 100%;
    min-height: 360px;
    flex: 1 1 auto;
}

@media (max-width: 900px) {
    body { overflow: auto; }

    .container {
        height: auto;
        min-height: 100vh;
    }

    .topbar {
        align-items: flex-start;
        flex-direction: column;
    }

    .toolbar {
        justify-content: flex-start;
    }

    .dashboard-main {
        grid-template-columns: 1fr;
    }

    .sidebar {
        grid-template-columns: repeat(2,1fr);
        grid-template-rows: auto;
    }

    .charts-grid {
        display: block;
    }

    .chart-panel {
        height: 240px;
        margin-bottom: 7px;
    }

    .settings-grid,
    .stats-grid,
    .runs-layout {
        grid-template-columns: 1fr;
    }
}
</style>
</head>

<body>
<div class="container">

<div class="topbar">
    <div class="title-wrap">
        <h1>Eddy / Cartographer Live Dashboard</h1>
        <div class="subtitle">
            Live frequency, temperature, Z/distance, statistics and test recording
        </div>
    </div>

    <div class="toolbar">
        <span id="connectionStatus" class="status-chip disconnected">Disconnected</span>
        <span id="recordingStatus" class="status-chip" hidden>REC</span>
        <span id="testTimer" class="status-chip timer-chip">00:00</span>

        <label class="window-control">
            Window
            <select id="windowSelect">
                <option value="60">1 min</option>
                <option value="300" selected>5 min</option>
                <option value="1200">20 min</option>
                <option value="3600">1 hour</option>
            </select>
        </label>

        <details class="menu" id="sessionMenu">
            <summary>Session ▾</summary>
            <div class="menu-panel">
                <button id="startTest">Start test</button>
                <button id="stopTest">Stop test</button>
                <button id="resetTest">Reset timer</button>
                <button id="resetBaseline">Reset baseline</button>
                <button id="startRecording">Start CSV recording</button>
                <button id="stopRecording">Stop CSV recording</button>
                <button id="downloadLast">Download last CSV</button>
            </div>
        </details>

        <details class="menu" id="dataMenu">
            <summary>Data ▾</summary>
            <div class="menu-panel">
                <button id="statsButton">Statistics</button>
                <button id="runsButton">Recorded runs / Compare</button>
            </div>
        </details>

        <button id="settingsButton">Settings</button>
    </div>
</div>

<div class="dashboard-main">

<div class="sidebar">

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

<div class="quick-actions">
    <button id="quickTest">Start Test</button>
    <button id="quickRecord">Record CSV</button>
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

<div class="footer-note">
BTT Eddy uses its saved calibration table. Cartographer uses its scan-model coefficients/domain. Dashboard values are diagnostic and do not replace Klipper probing or Z-offset logic.
</div>

</div>

<!-- Settings modal -->
<div id="settingsModal" class="modal" hidden>
<div class="modal-card">
    <div class="modal-head">
        <h2>Settings</h2>
        <button class="close-modal" data-modal="settingsModal">Close</button>
    </div>

    <div class="settings-grid">
        <div class="field">
            <label for="probeType">Probe type</label>
            <select id="probeType">
                <option value="btt_eddy">BTT Eddy / Klipper LDC1612</option>
                <option value="cartographer">Cartographer V3 / Cartographer3D</option>
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
            <label for="eddySensorName">Eddy sensor name</label>
            <input id="eddySensorName" type="text" placeholder="btt_eddy">
        </div>

        <div class="field">
            <label for="temperatureProbeName">Temperature probe name</label>
            <input id="temperatureProbeName" type="text" placeholder="btt_eddy">
        </div>

        <div class="field">
            <label for="averageSamples">Rolling sample average</label>
            <input id="averageSamples" type="number" min="1" max="200" value="1">
        </div>

        <div class="field">
            <label for="frequencyColor">Frequency graph color</label>
            <input id="frequencyColor" type="color">
        </div>

        <div class="field">
            <label for="temperatureColor">Temperature graph color</label>
            <input id="temperatureColor" type="color">
        </div>

        <div class="field">
            <label for="zColor">Z/distance graph color</label>
            <input id="zColor" type="color">
        </div>

        <label class="checkbox-field">
            <input id="autoDetect" type="checkbox">
            Auto-detect probe and calibration/model at startup
        </label>
    </div>

    <div class="calibration-field" id="bttCalibrationField">
        <label for="calibrationText">BTT Eddy calibration data</label>
        <textarea id="calibrationText"
placeholder="Paste calibrate = data here, or use Auto Detect."></textarea>
    </div>

    <div class="calibration-field" id="cartographerModelField" hidden>
        <label for="cartographerModelText">Cartographer scan model</label>
        <textarea id="cartographerModelText"
placeholder="Paste [cartographer scan_model default] here, or use Auto Detect."></textarea>
    </div>

    <div class="settings-actions">
        <button id="detectButton">Auto Detect Now</button>
        <button id="saveSettings">Save settings</button>
        <span id="settingsMessage" class="message"></span>
    </div>
</div>
</div>

<!-- Statistics modal -->
<div id="statsModal" class="modal" hidden>
<div class="modal-card">
    <div class="modal-head">
        <h2>Live statistics</h2>
        <button class="close-modal" data-modal="statsModal">Close</button>
    </div>

    <div class="stats-grid">
        <div class="stat-group">
            <h3>Frequency</h3>
            <div class="stat-row"><span>Minimum</span><span id="statFMin">--</span></div>
            <div class="stat-row"><span>Maximum</span><span id="statFMax">--</span></div>
            <div class="stat-row"><span>Std. deviation</span><span id="statFStd">--</span></div>
            <div class="stat-row"><span>Drift rate</span><span id="statFSlope">--</span></div>
            <div class="stat-row"><span>PPM rate</span><span id="statPpmSlope">--</span></div>
        </div>

        <div class="stat-group">
            <h3>Temperature</h3>
            <div class="stat-row"><span>Minimum</span><span id="statTMin">--</span></div>
            <div class="stat-row"><span>Maximum</span><span id="statTMax">--</span></div>
            <div class="stat-row"><span>Std. deviation</span><span id="statTStd">--</span></div>
        </div>

        <div class="stat-group">
            <h3>Z / distance</h3>
            <div class="stat-row"><span>Minimum</span><span id="statZMin">--</span></div>
            <div class="stat-row"><span>Maximum</span><span id="statZMax">--</span></div>
            <div class="stat-row"><span>Std. deviation</span><span id="statZStd">--</span></div>
            <div class="stat-row"><span>Drift rate</span><span id="statZSlope">--</span></div>
        </div>
    </div>

    <div class="message" id="statsFooter"></div>
</div>
</div>

<!-- Runs / comparison modal -->
<div id="runsModal" class="modal" hidden>
<div class="modal-card wide">
    <div class="modal-head">
        <h2>Recorded runs</h2>
        <button class="close-modal" data-modal="runsModal">Close</button>
    </div>

    <div class="runs-layout">
        <div>
            <div class="run-list" id="runList">No recordings yet.</div>
        </div>

        <div class="compare-wrap">
            <div class="compare-toolbar">
                <label>
                    Compare
                    <select id="compareMetric">
                        <option value="frequency_delta_hz">Frequency Δ (Hz)</option>
                        <option value="ppm">Frequency drift (ppm)</option>
                        <option value="z_delta_um">Z/distance Δ (µm)</option>
                        <option value="temperature_delta_c">Temperature Δ (°C)</option>
                    </select>
                </label>
                <button id="compareSelected">Compare selected</button>
            </div>
            <canvas id="comparisonChart"></canvas>
        </div>
    </div>
</div>
</div>

<script>
const points = [];
let baseline = {frequency:null, temperature:null, z:null};
let windowSeconds = 300;
let currentConfig = {};
let recordingState = {active:false, last_filename:null};
let testState = {active:false, elapsed_seconds:0};

const frequencyEl = document.getElementById("frequency");
const temperatureEl = document.getElementById("temperature");
const zEl = document.getElementById("z");
const frequencyDeltaEl = document.getElementById("frequencyDelta");
const temperatureDeltaEl = document.getElementById("temperatureDelta");
const zDeltaEl = document.getElementById("zDelta");
const ppmEl = document.getElementById("ppm");
const statusEl = document.getElementById("connectionStatus");
const recordingStatusEl = document.getElementById("recordingStatus");
const testTimerEl = document.getElementById("testTimer");
const settingsMessage = document.getElementById("settingsMessage");

const frequencyCanvas = document.getElementById("frequencyChart");
const temperatureCanvas = document.getElementById("temperatureChart");
const zCanvas = document.getElementById("zChart");
const comparisonCanvas = document.getElementById("comparisonChart");

function fmt(value, digits=2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return "--";
    }
    return Number(value).toFixed(digits);
}

function closeMenus() {
    document.querySelectorAll("details.menu").forEach(m => m.removeAttribute("open"));
}

function updateMetrics(point) {
    if (!point) return;

    frequencyEl.textContent = fmt(point.frequency, 2) + " Hz";
    temperatureEl.textContent =
        point.temperature !== null ? fmt(point.temperature, 3) + " °C" : "--";
    zEl.textContent =
        point.z !== null ? fmt(point.z, 5) + " mm" : "No distance";

    if (baseline.frequency !== null) {
        const df = point.frequency - baseline.frequency;
        frequencyDeltaEl.textContent = "Δ " + fmt(df, 2) + " Hz";
        ppmEl.textContent = fmt(df / baseline.frequency * 1000000, 1) + " ppm";
    }

    if (point.temperature !== null && baseline.temperature !== null) {
        temperatureDeltaEl.textContent =
            "Δ " + fmt(point.temperature - baseline.temperature, 3) + " °C";
    } else {
        temperatureDeltaEl.textContent = "Δ --";
    }

    if (point.z !== null && baseline.z !== null) {
        zDeltaEl.textContent =
            "Δ " + fmt((point.z - baseline.z) * 1000, 1) + " µm";
    } else {
        zDeltaEl.textContent = "Δ --";
    }
}

function trimPoints() {
    const cutoff = Date.now()/1000 - windowSeconds - 10;
    while (points.length && points[0].wall_time < cutoff) points.shift();
}

function visiblePoints() {
    const cutoff = Date.now()/1000 - windowSeconds;
    return points.filter(p => p.wall_time >= cutoff);
}

function resizeCanvas(canvas) {
    const ratio = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(100, rect.width);
    const height = Math.max(100, rect.height);

    if (
        canvas.width !== Math.floor(width * ratio) ||
        canvas.height !== Math.floor(height * ratio)
    ) {
        canvas.width = Math.floor(width * ratio);
        canvas.height = Math.floor(height * ratio);
    }

    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio,0,0,ratio,0,0);
    return {ctx,width,height};
}

function drawChart(canvas, data, field, color, digits) {
    const {ctx,width,height} = resizeCanvas(canvas);
    ctx.clearRect(0,0,width,height);

    const left=72, right=12, top=7, bottom=24;
    const plotWidth=width-left-right;
    const plotHeight=height-top-bottom;

    const valid = data.filter(
        p => p[field] !== null && p[field] !== undefined
    );

    if (valid.length < 2) {
        ctx.fillStyle="#8b96a2";
        ctx.font="12px Arial";
        ctx.fillText("Waiting for data...",left,top+25);
        return;
    }

    const now=Date.now()/1000;
    const start=now-windowSeconds;

    let min=Math.min(...valid.map(p=>p[field]));
    let max=Math.max(...valid.map(p=>p[field]));
    let span=max-min;

    if (span===0) span=Math.max(Math.abs(max)*0.00001,0.001);
    min-=span*0.10;
    max+=span*0.10;

    ctx.strokeStyle="#28313a";
    ctx.lineWidth=1;
    ctx.fillStyle="#8f9aa6";
    ctx.font="10px Arial";

    for(let i=0;i<=4;i++){
        const y=top+plotHeight*i/4;
        ctx.beginPath();
        ctx.moveTo(left,y);
        ctx.lineTo(left+plotWidth,y);
        ctx.stroke();

        const value=max-(max-min)*i/4;
        ctx.fillText(value.toFixed(digits),4,y+3);
    }

    for(let i=0;i<=5;i++){
        const x=left+plotWidth*i/5;
        ctx.beginPath();
        ctx.moveTo(x,top);
        ctx.lineTo(x,top+plotHeight);
        ctx.stroke();

        const secondsAgo=windowSeconds-windowSeconds*i/5;
        const label=windowSeconds<=300
            ? "-" + Math.round(secondsAgo) + "s"
            : "-" + (secondsAgo/60).toFixed(0) + "m";

        ctx.fillText(label,x-12,height-5);
    }

    ctx.strokeStyle=color;
    ctx.lineWidth=2;
    ctx.beginPath();
    let started=false;

    for(const p of valid){
        if(p.wall_time<start) continue;

        const x=left+((p.wall_time-start)/windowSeconds)*plotWidth;
        const y=top+(1-(p[field]-min)/(max-min))*plotHeight;

        if(!started){
            ctx.moveTo(x,y);
            started=true;
        }else{
            ctx.lineTo(x,y);
        }
    }

    ctx.stroke();
}

function redraw() {
    trimPoints();
    const data=visiblePoints();

    drawChart(
        frequencyCanvas,data,"frequency",
        currentConfig.frequency_color || "#4f8cff",1
    );

    drawChart(
        temperatureCanvas,data,"temperature",
        currentConfig.temperature_color || "#ff6262",3
    );

    drawChart(
        zCanvas,data,"z",
        currentConfig.z_color || "#58d17b",4
    );

    requestAnimationFrame(redraw);
}

function updateProbeTypeUI() {
    const isCartographer =
        document.getElementById("probeType").value === "cartographer";

    document.getElementById("eddySensorName").disabled=isCartographer;
    document.getElementById("temperatureProbeName").disabled=isCartographer;
    document.getElementById("bttCalibrationField").hidden=isCartographer;
    document.getElementById("cartographerModelField").hidden=!isCartographer;

    document.getElementById("zMetricLabel").textContent =
        isCartographer ? "Cartographer distance" : "Calibration-equivalent Z";

    document.getElementById("zChartTitle").textContent =
        isCartographer
        ? "Cartographer model distance (mm)"
        : "Calibration-equivalent Z (mm)";
}

async function loadConfigForm() {
    const response=await fetch("/api/config");
    const cfg=await response.json();
    currentConfig=cfg;

    document.getElementById("probeType").value=cfg.probe_type || "btt_eddy";
    document.getElementById("moonrakerHost").value=cfg.moonraker_host || "127.0.0.1";
    document.getElementById("moonrakerPort").value=cfg.moonraker_port || 7125;
    document.getElementById("eddySensorName").value=cfg.eddy_sensor_name || "";
    document.getElementById("temperatureProbeName").value=cfg.temperature_probe_name || "";
    document.getElementById("averageSamples").value=cfg.average_samples || 1;
    document.getElementById("frequencyColor").value=cfg.frequency_color || "#4f8cff";
    document.getElementById("temperatureColor").value=cfg.temperature_color || "#ff6262";
    document.getElementById("zColor").value=cfg.z_color || "#58d17b";
    document.getElementById("autoDetect").checked=cfg.auto_detect !== false;
    document.getElementById("calibrationText").value=cfg.calibration_text || "";
    document.getElementById("cartographerModelText").value=cfg.cartographer_model_text || "";

    updateProbeTypeUI();

    settingsMessage.textContent =
        cfg.probe_type === "cartographer"
        ? (cfg.cartographer_model_loaded ? "Cartographer model loaded" : "Cartographer model not loaded")
        : (cfg.calibration_points + " calibration points loaded");
}

async function loadInitialData() {
    const statusResponse=await fetch("/api/status");
    const status=await statusResponse.json();

    baseline=status.baseline;
    currentConfig={...currentConfig,...status.config};

    if(status.connected){
        statusEl.textContent="Connected";
        statusEl.className="status-chip connected";
    }else{
        statusEl.textContent="Disconnected";
        statusEl.className="status-chip disconnected";
    }

    const historyResponse=await fetch("/api/history?seconds="+windowSeconds);
    const history=await historyResponse.json();

    points.length=0;
    history.forEach(p=>points.push(p));

    if(points.length) updateMetrics(points[points.length-1]);
}

async function refreshRecordingState() {
    try {
        const r=await fetch("/api/recording/status");
        recordingState=await r.json();

        recordingStatusEl.hidden=!recordingState.active;

        if(recordingState.active){
            recordingStatusEl.textContent="● REC";
            recordingStatusEl.className="status-chip recording";
            document.getElementById("quickRecord").textContent="Stop Recording";
        }else{
            document.getElementById("quickRecord").textContent="Record CSV";
        }
    } catch {}
}

function formatTimer(seconds) {
    seconds=Math.max(0,Math.floor(seconds || 0));
    const h=Math.floor(seconds/3600);
    const m=Math.floor((seconds%3600)/60);
    const s=seconds%60;

    if(h>0){
        return String(h).padStart(2,"0")+":"+
               String(m).padStart(2,"0")+":"+
               String(s).padStart(2,"0");
    }

    return String(m).padStart(2,"0")+":"+
           String(s).padStart(2,"0");
}

async function refreshTestState() {
    try {
        const r=await fetch("/api/test/status");
        testState=await r.json();
        testTimerEl.textContent=formatTimer(testState.elapsed_seconds);
        document.getElementById("quickTest").textContent =
            testState.active ? "Stop Test" : "Start Test";
    } catch {}
}

function openModal(id) {
    document.getElementById(id).hidden=false;
    closeMenus();
}

function closeModal(id) {
    document.getElementById(id).hidden=true;
}

async function refreshStats() {
    const r=await fetch("/api/stats?seconds="+windowSeconds);
    const s=await r.json();

    function setGroup(prefix,g,unit,digits){
        document.getElementById(prefix+"Min").textContent =
            g ? fmt(g.min,digits)+" "+unit : "--";
        document.getElementById(prefix+"Max").textContent =
            g ? fmt(g.max,digits)+" "+unit : "--";
        document.getElementById(prefix+"Std").textContent =
            g ? fmt(g.stddev,digits)+" "+unit : "--";
    }

    setGroup("statF",s.frequency,"Hz",2);
    setGroup("statT",s.temperature,"°C",3);
    setGroup("statZ",s.z,"mm",5);

    document.getElementById("statFSlope").textContent =
        s.frequency_hz_per_min !== null && s.frequency_hz_per_min !== undefined
        ? fmt(s.frequency_hz_per_min,2)+" Hz/min" : "--";

    document.getElementById("statPpmSlope").textContent =
        s.frequency_ppm_per_min !== null && s.frequency_ppm_per_min !== undefined
        ? fmt(s.frequency_ppm_per_min,2)+" ppm/min" : "--";

    document.getElementById("statZSlope").textContent =
        s.z_um_per_min !== null && s.z_um_per_min !== undefined
        ? fmt(s.z_um_per_min,2)+" µm/min" : "--";

    document.getElementById("statsFooter").textContent =
        (s.count || 0)+" displayed samples across "+
        fmt((s.duration_seconds || 0)/60,2)+" minutes";
}

async function loadRuns() {
    const r=await fetch("/api/recordings");
    const runs=await r.json();
    const list=document.getElementById("runList");

    if(!runs.length){
        list.textContent="No recordings yet.";
        return;
    }

    list.innerHTML="";

    runs.forEach(run=>{
        const div=document.createElement("div");
        div.className="run-item";

        if(run.error){
            div.textContent=run.filename+" — error: "+run.error;
            list.appendChild(div);
            return;
        }

        const label=document.createElement("label");
        const check=document.createElement("input");
        check.type="checkbox";
        check.className="run-check";
        check.value=run.filename;

        label.appendChild(check);
        label.appendChild(document.createTextNode(" "+run.filename));

        const meta=document.createElement("div");
        meta.className="run-meta";
        meta.textContent=
            fmt(run.duration_seconds/60,1)+" min · "+
            run.points+" points · "+(run.probe_type || "unknown");

        const download=document.createElement("button");
        download.textContent="Download";
        download.style.marginTop="5px";
        download.addEventListener("click",()=>{
            window.location="/api/recordings/download/"+encodeURIComponent(run.filename);
        });

        div.appendChild(label);
        div.appendChild(meta);
        div.appendChild(download);
        list.appendChild(div);
    });
}

function drawComparison(series,metric) {
    const {ctx,width,height}=resizeCanvas(comparisonCanvas);
    ctx.clearRect(0,0,width,height);

    const validSeries=series.filter(s=>s.data.some(p=>p[metric]!==null));

    if(!validSeries.length){
        ctx.fillStyle="#8b96a2";
        ctx.font="13px Arial";
        ctx.fillText("Select recorded runs to compare.",30,35);
        return;
    }

    const left=68,right=15,top=16,bottom=32;
    const pw=width-left-right,ph=height-top-bottom;

    let xmax=0;
    const vals=[];

    validSeries.forEach(s=>{
        s.data.forEach(p=>{
            xmax=Math.max(xmax,p.minutes);
            if(p[metric]!==null && p[metric]!==undefined) vals.push(p[metric]);
        });
    });

    let ymin=Math.min(...vals), ymax=Math.max(...vals);
    let span=ymax-ymin;
    if(span===0) span=Math.max(Math.abs(ymax)*.01,1);
    ymin-=span*.08;
    ymax+=span*.08;
    xmax=Math.max(xmax,.01);

    ctx.strokeStyle="#28313a";
    ctx.fillStyle="#8f9aa6";
    ctx.font="10px Arial";

    for(let i=0;i<=5;i++){
        const y=top+ph*i/5;
        ctx.beginPath(); ctx.moveTo(left,y); ctx.lineTo(left+pw,y); ctx.stroke();
        const v=ymax-(ymax-ymin)*i/5;
        ctx.fillText(v.toFixed(2),4,y+3);
    }

    const colors=["#4f8cff","#ff9f43","#58d17b","#d980fa","#ff6262","#52c7d9"];

    validSeries.forEach((s,index)=>{
        ctx.strokeStyle=colors[index%colors.length];
        ctx.lineWidth=2;
        ctx.beginPath();
        let started=false;

        s.data.forEach(p=>{
            const v=p[metric];
            if(v===null || v===undefined) return;
            const x=left+p.minutes/xmax*pw;
            const y=top+(1-(v-ymin)/(ymax-ymin))*ph;

            if(!started){ctx.moveTo(x,y);started=true;}
            else ctx.lineTo(x,y);
        });

        ctx.stroke();
        ctx.fillStyle=colors[index%colors.length];
        ctx.fillText(s.name,left+10,top+14+index*14);
    });

    ctx.fillStyle="#8f9aa6";
    ctx.fillText("Time (min)",left+pw/2-20,height-7);
}

const source=new EventSource("/events");

source.onmessage=event=>{
    const point=JSON.parse(event.data);
    points.push(point);
    updateMetrics(point);
    statusEl.textContent="Connected";
    statusEl.className="status-chip connected";
};

source.onerror=()=>{
    statusEl.textContent="Stream reconnecting...";
    statusEl.className="status-chip disconnected";
};

document.getElementById("windowSelect").addEventListener("change",async e=>{
    windowSeconds=Number(e.target.value);
    await loadInitialData();
    if(!document.getElementById("statsModal").hidden) await refreshStats();
});

document.getElementById("settingsButton").addEventListener("click",async()=>{
    await loadConfigForm();
    openModal("settingsModal");
});

document.querySelectorAll(".close-modal").forEach(btn=>{
    btn.addEventListener("click",()=>closeModal(btn.dataset.modal));
});

document.addEventListener("keydown",e=>{
    if(e.key==="Escape"){
        document.querySelectorAll(".modal").forEach(m=>m.hidden=true);
        closeMenus();
    }
});

document.getElementById("probeType").addEventListener("change",updateProbeTypeUI);

document.getElementById("detectButton").addEventListener("click",async()=>{
    settingsMessage.className="message";
    settingsMessage.textContent="Detecting...";

    const r=await fetch("/api/detect",{method:"POST"});
    const result=await r.json();

    if(!r.ok){
        settingsMessage.className="message error";
        settingsMessage.textContent=result.error || "Detection failed";
        return;
    }

    await loadConfigForm();
    settingsMessage.className="message ok";
    settingsMessage.textContent=result.detected.message;
    await loadInitialData();
});

document.getElementById("saveSettings").addEventListener("click",async()=>{
    settingsMessage.className="message";
    settingsMessage.textContent="Saving...";

    const payload={
        probe_type:document.getElementById("probeType").value,
        moonraker_host:document.getElementById("moonrakerHost").value.trim(),
        moonraker_port:Number(document.getElementById("moonrakerPort").value),
        eddy_sensor_name:document.getElementById("eddySensorName").value.trim(),
        temperature_probe_name:document.getElementById("temperatureProbeName").value.trim(),
        average_samples:Number(document.getElementById("averageSamples").value),
        auto_detect:document.getElementById("autoDetect").checked,
        frequency_color:document.getElementById("frequencyColor").value,
        temperature_color:document.getElementById("temperatureColor").value,
        z_color:document.getElementById("zColor").value,
        calibration_text:document.getElementById("calibrationText").value,
        cartographer_model_text:document.getElementById("cartographerModelText").value
    };

    const r=await fetch("/api/config",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify(payload)
    });

    const result=await r.json();

    if(!r.ok){
        settingsMessage.className="message error";
        settingsMessage.textContent=result.error || "Could not save settings";
        return;
    }

    settingsMessage.className="message ok";
    settingsMessage.textContent="Settings saved";
    currentConfig={...currentConfig,...payload};
    points.length=0;
    await loadInitialData();
});

async function resetBaseline(){
    await fetch("/api/reset_baseline",{method:"POST"});
    await loadInitialData();
}

document.getElementById("resetBaseline").addEventListener("click",resetBaseline);

async function startTest(){
    await fetch("/api/test/start",{method:"POST"});
    await refreshTestState();
    await loadInitialData();
    closeMenus();
}

async function stopTest(){
    await fetch("/api/test/stop",{method:"POST"});
    await refreshTestState();
    closeMenus();
}

document.getElementById("startTest").addEventListener("click",startTest);
document.getElementById("stopTest").addEventListener("click",stopTest);
document.getElementById("resetTest").addEventListener("click",async()=>{
    await fetch("/api/test/reset",{method:"POST"});
    await refreshTestState();
    closeMenus();
});

document.getElementById("quickTest").addEventListener("click",async()=>{
    if(testState.active) await stopTest();
    else await startTest();
});

async function startRecording(){
    const defaultLabel="test_"+new Date().toISOString().slice(0,19).replaceAll(":","-");
    const label=prompt("Recording label:",defaultLabel);
    if(label===null) return;

    const r=await fetch("/api/recording/start",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({label})
    });

    const result=await r.json();

    if(!r.ok){
        alert(result.error || "Could not start recording");
        return;
    }

    await refreshRecordingState();
    closeMenus();
}

async function stopRecording(){
    const r=await fetch("/api/recording/stop",{method:"POST"});
    const result=await r.json();

    if(!r.ok && r.status!==409){
        alert(result.error || "Could not stop recording");
    }

    await refreshRecordingState();
    closeMenus();
}

document.getElementById("startRecording").addEventListener("click",startRecording);
document.getElementById("stopRecording").addEventListener("click",stopRecording);

document.getElementById("quickRecord").addEventListener("click",async()=>{
    if(recordingState.active) await stopRecording();
    else await startRecording();
});

document.getElementById("downloadLast").addEventListener("click",async()=>{
    await refreshRecordingState();
    const name=recordingState.last_filename;

    if(!name){
        alert("No completed recording is available yet.");
        return;
    }

    window.location="/api/recordings/download/"+encodeURIComponent(name);
    closeMenus();
});

document.getElementById("statsButton").addEventListener("click",async()=>{
    await refreshStats();
    openModal("statsModal");
});

document.getElementById("runsButton").addEventListener("click",async()=>{
    await loadRuns();
    openModal("runsModal");
    drawComparison([],"frequency_delta_hz");
});

document.getElementById("compareSelected").addEventListener("click",async()=>{
    const selected=[
        ...document.querySelectorAll(".run-check:checked")
    ].slice(0,6);

    if(!selected.length){
        drawComparison([],document.getElementById("compareMetric").value);
        return;
    }

    const series=[];

    for(const checkbox of selected){
        const r=await fetch(
            "/api/recordings/data/"+
            encodeURIComponent(checkbox.value)
        );

        series.push({
            name:checkbox.value,
            data:await r.json()
        });
    }

    drawComparison(
        series,
        document.getElementById("compareMetric").value
    );
});

setInterval(refreshRecordingState,2000);
setInterval(refreshTestState,1000);

loadConfigForm();
loadInitialData();
refreshRecordingState();
refreshTestState();
requestAnimationFrame(redraw);
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return HTML



def cleanup_dashboard():
    global recording_active
    global recording_file
    global recording_writer

    stop_cartographer_stream()

    with recording_lock:
        if recording_file is not None:
            try:
                recording_file.flush()
                recording_file.close()
            except Exception:
                pass

        recording_active = False
        recording_file = None
        recording_writer = None


atexit.register(cleanup_dashboard)



def get_local_lan_ip():
    """
    Return the primary LAN IP used for outbound local-network traffic.
    Falls back to hostname resolution if the UDP routing trick is unavailable.
    """
    sock = None

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # No packets are actually sent; connect() only asks the OS which local
        # interface/address it would use for this route.
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass



if __name__ == "__main__":
    load_config()

    if config.get("auto_detect", True):
        try:
            detected = auto_detect_configuration(save=True)
            print("Probe auto-detection:", detected.get("message"))
        except Exception as e:
            print("Probe auto-detection warning:", e)

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

    web_port = int(config["web_port"])
    web_host = config["web_host"]
    lan_ip = get_local_lan_ip()

    print("Eddy dashboard starting")
    print(f"  Port:       {web_port}")
    print(f"  Local:      http://127.0.0.1:{web_port}")

    if lan_ip and lan_ip != "127.0.0.1":
        print(f"  Network:    http://{lan_ip}:{web_port}")

    if web_host not in ("0.0.0.0", "127.0.0.1", "::"):
        print(f"  Bind host:  {web_host}")

    print()

    app.run(
        host=config["web_host"],
        port=int(config["web_port"]),
        threaded=True,
        debug=False
    )