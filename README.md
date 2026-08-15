# Eddy / Cartographer Live Dashboard

A lightweight diagnostic dashboard for Klipper eddy-current probes. It runs as a
small Flask web app and shows live probe frequency, temperature, and calculated
Z/distance so you can watch drift, warm-up behavior, repeatability, and sensor
stability in real time.

Supported probe types:

- BTT Eddy and other Klipper probes using the LDC1612 interface
- Cartographer V3 using the current Cartographer3D plugin

The dashboard is read-only with respect to normal printer operation. It does not
command motion, heaters, probing, homing, calibration, or firmware changes.

## Screenshot

![Eddy / Cartographer Live Dashboard](images/dashboard.png)

## What It Is For

Use this dashboard when you want to investigate:

- probe drift during warm-up or long idle periods
- frequency stability and noise
- thermal behavior
- repeatability between tests
- apparent Z movement from frequency changes
- Cartographer scan-model behavior
- effects of probe height, bed temperature, or target material
- changes caused by different BTT Eddy LDC drive-current settings
- hardware, wiring, or electronics changes

It is intended as a diagnostic and development tool, not as a background service.

## Features

- Live scrolling graphs for frequency, temperature, and Z/distance
- Current frequency, probe temperature, Z/distance, and drift readouts
- Resettable baseline for comparing changes during a test
- Frequency drift shown in ppm
- Drift-rate statistics in Hz/min, ppm/min, and µm/min
- Selectable graph windows: 1 minute, 5 minutes, 20 minutes, and 1 hour
- Automatic BTT Eddy / Cartographer probe detection
- Automatic BTT Eddy calibration-table discovery
- Automatic Cartographer scan-model discovery
- Paste-in Klipper Eddy `calibrate =` tables
- Paste-in Cartographer `[cartographer scan_model default]` blocks
- Configurable rolling sample averaging
- Configurable graph colors
- Start/stop test timer
- Start/stop CSV recording
- Download recorded CSV files from the browser
- Recorded-run comparison
- Settings saved between restarts
- Automatic reconnect after interruptions
- Quiet terminal output with useful startup URLs
- No Klipper source modification required
- No firmware modification required
- No permanent printer configuration changes required

## Requirements

You need a Klipper host with:

- Klipper
- Moonraker
- Python 3
- network access to the printer from the machine running the dashboard

You also need one supported probe setup.

### BTT Eddy / LDC1612

For BTT Eddy-style probes, Klipper must have a probe using the
`[probe_eddy_current ...]` implementation with the LDC1612 diagnostic stream
available.

### Cartographer V3

For Cartographer, the printer must be running the current Cartographer3D plugin
and expose the `cartographer` object through Klipper/Moonraker.

## Installation

SSH into the Klipper host and create a directory for the dashboard:

```bash
mkdir -p ~/eddy-dashboard
cd ~/eddy-dashboard
```

Copy or download these files into that directory:

```text
eddy_dashboard.py
requirements.txt
```

The directory should look similar to this:

```text
~/eddy-dashboard/
|-- eddy_dashboard.py
|-- requirements.txt
```

Create a virtual environment:

```bash
python3 -m venv venv
```

If the `venv` module is missing, install it first:

```bash
sudo apt update
sudo apt install python3-venv
python3 -m venv venv
```

Activate the environment:

```bash
source venv/bin/activate
```

Install the Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

The requirements file contains pinned versions of:

```text
Flask
websocket-client
```

## Running The Dashboard

From the dashboard directory, activate the virtual environment and run the app:

```bash
cd ~/eddy-dashboard
source venv/bin/activate
python3 eddy_dashboard.py
```

You should see output similar to:

```text
Probe auto-detection: Detected Cartographer.
Using Cartographer scanner status stream
Using Cartographer object: cartographer

Eddy dashboard starting
  Port:       8085
  Local:      http://127.0.0.1:8085
```

Open the local dashboard URL on the same machine:

```text
http://127.0.0.1:8085
```

If the dashboard is running on a headless Klipper host and you are opening it
from another computer, see [Network Access](#network-access).

Normal Flask request logs are intentionally suppressed so the terminal stays
readable. Meaningful connection, stream, and persistent error messages are still
shown.

Stop the dashboard with `Ctrl+C` in the terminal.

## Network Access

By default, the dashboard binds to `127.0.0.1:8085` and is reachable only from
the machine it runs on.

To allow access from other devices on your local network, start it with:

```bash
python3 eddy_dashboard.py --host 0.0.0.0
```

Then open the dashboard from another device using the Klipper host's IP address:

```text
http://PRINTER_IP:8085
```

For example:

```text
http://192.168.1.100:8085
```

If the printer hostname resolves locally, this may also work:

```text
http://voron:8085
```

The `--host` option applies only to the current run. It is deliberately not saved
in `eddy_dashboard_config.json`, so a forgotten `0.0.0.0` setting cannot
silently re-expose the dashboard later.

You can also change the web port for the current run:

```bash
python3 eddy_dashboard.py --port 8086
```

## Security Notes

This is a **diagnostic tool**, not a service. Run it manually when you need it
and stop it when you're done.

**The dashboard is unauthenticated by design.** There is no login. Access
control relies entirely on the fact that only local/private network clients are
accepted. Anyone who can reach the port can read live probe data, change the
configured Moonraker target, and start or stop CSV recordings.

**Do not place this dashboard behind a reverse proxy.** The local-only check
inspects the real TCP peer address and deliberately ignores `X-Forwarded-For`.
If nginx, Caddy, Traefik, or similar sits in front of it, every request appears
to originate from `127.0.0.1`, the local-only check passes for all clients
including those from the Internet, and the protection is silently defeated.
This matters in practice because Klipper hosts frequently already run nginx for
Mainsail or Fluidd. Do not add a proxy entry for this dashboard.

Only use `--host 0.0.0.0` on a network you trust.

## First-Time Setup

Open the dashboard and select **Settings**.

The Settings window contains:

- probe type
- Moonraker host and port
- automatic detection controls
- rolling sample averaging
- graph colors
- manual BTT Eddy calibration data
- manual Cartographer scan-model data

The Settings window can be closed with the **Close** button or the `Esc` key.

### Auto Detection

By default, the dashboard attempts to detect the installed probe at startup. It
checks Klipper's loaded objects for either:

```text
cartographer
```

or:

```text
probe_eddy_current <name>
```

For BTT Eddy, auto detection also tries to find:

- the Eddy sensor name
- a matching `temperature_probe`
- the saved `calibrate =` table

For Cartographer, auto detection also tries to find:

- the `cartographer` status object
- the active scan model
- the saved Cartographer scan-model coefficients and domain

You can run detection manually at any time:

```text
Settings -> Auto Detect Now
```

Automatic detection can be disabled in Settings if you prefer to configure the
probe manually.

### Moonraker Settings

If the dashboard runs on the same computer as Moonraker, use:

```text
Moonraker host: 127.0.0.1
Moonraker port: 7125
```

The dashboard validates the configured Moonraker target so it stays limited to
local/private network addresses.

## BTT Eddy Setup

Select this probe type in Settings:

```text
BTT Eddy / Klipper LDC1612
```

### Eddy Sensor Name

Use the name after `[probe_eddy_current ...]` in `printer.cfg`.

For example:

```ini
[probe_eddy_current btt_eddy]
```

uses this sensor name:

```text
btt_eddy
```

### Temperature Probe Name

Use the name after `[temperature_probe ...]` in `printer.cfg`.

For example:

```ini
[temperature_probe btt_eddy]
```

uses this temperature probe name:

```text
btt_eddy
```

If there is no separate temperature probe object, leave this field blank.

### Calibration Data

The calibration-equivalent Z graph uses the existing Klipper Eddy calibration
table. The dashboard can usually discover the saved `calibrate =` table
automatically from Klipper.

If automatic discovery is unavailable or you want to override it manually, copy
the `calibrate =` block from the `SAVE_CONFIG` section at the bottom of
`printer.cfg`.

Example:

```ini
#*# [probe_eddy_current btt_eddy]
#*# reg_drive_current = 27
#*# calibrate =
#*#   0.050000:678437.389,0.090000:678275.407,0.130000:678111.331,
#*#   0.170000:677946.422,0.210000:677783.829,0.250000:677620.523,
#*#   0.290000:677460.811,0.330000:677298.268,0.370000:677139.817,
#*#   0.410000:676981.183,0.450000:676828.249,0.490000:676671.691
```

Paste the block directly into the dashboard. The parser extracts `Z:frequency`
pairs, such as:

```text
0.050000:678437.389
```

It ignores surrounding text such as `calibrate =`, `#*#`, and line breaks.

### Calibration-Equivalent Z

The dashboard converts the measured frequency into Z using linear interpolation
between the saved calibration points.

The displayed value means: the Z position that corresponds to the current
measured frequency according to the stored Klipper calibration.

This is useful for measuring apparent sensor drift. It is not necessarily the
true physical distance between the probe and the target, especially outside the
calibrated sensing range.

The dashboard deliberately does not extrapolate beyond the pasted calibration
range. If the current frequency is outside the calibration table, it displays:

```text
No distance
```

## Cartographer V3 Setup

Select this probe type in Settings:

```text
Cartographer V3 / Cartographer3D
```

When Cartographer is selected, the BTT Eddy sensor-name and calibration fields
are not required.

The dashboard reads Cartographer's live data from:

```text
cartographer.mcu.last_sample
```

### Scan Model

The dashboard can usually discover the active Cartographer scan model
automatically from Klipper.

If automatic discovery is unavailable or you want to override it manually, paste
the complete scan-model block from `SAVE_CONFIG`.

Example:

```ini
#*# [cartographer scan_model default]
#*# coefficients = 1.4142270210736863,1.8858165705181558,0.8609574640872374,0.4194478576326323,0.35267133207875817,0.392660166158967,-0.10864201080375517,-0.22252968570372833,0.28264700523301084,0.22721290980476372
#*# domain = 3.190766648414659e-07,3.338151076700032e-07
#*# z_offset = 0
#*# reference_temperature = 28.82
#*# software_version = 1.9.0
#*# mcu_version = CARTOGRAPHER V3 6.1.0
```

The dashboard extracts:

```text
coefficients
domain
z_offset
reference_temperature
```

The important values used for distance conversion are `coefficients`, `domain`,
and `z_offset`.

### Model Distance

Cartographer's scan model is a polynomial evaluated against inverse sensor
frequency:

```text
frequency -> 1 / frequency -> Cartographer polynomial model -> distance
```

The dashboard reproduces the scan-model polynomial from the saved
`coefficients =` and `domain =` data. This allows the third graph to display
Cartographer model distance rather than BTT-style calibration-equivalent Z.

### Temperature Compensation

The Cartographer scan model includes `reference_temperature`. Cartographer can
also use a separate coil temperature-compensation calibration.

The current dashboard does **not** reproduce an optional separate Cartographer
coil temperature-compensation curve.

If no additional coil compensation model is configured, the scan-model conversion
is sufficient. If a separate temperature-compensation model is active, there may
be a difference between the dashboard's calculated distance and Cartographer's
internally compensated result.

## Using The Dashboard

### Main View

The desktop layout is designed to fit on one browser screen. The left side shows
the live values:

```text
Frequency
Probe temperature
Z / Cartographer distance
Frequency drift
```

The right side shows three scrolling graphs:

```text
Frequency
Temperature
Z / distance
```

On smaller screens, the dashboard automatically switches to a stacked layout.

### Graph Windows

Available graph windows:

```text
1 minute
5 minutes
20 minutes
1 hour
```

The graphs continuously scroll as new samples arrive. Recent samples are kept in
memory so switching the graph window can show earlier samples from the current
session.

### Reset Baseline

The **Reset baseline** button stores the current values as the reference.

After resetting:

```text
Frequency change   = 0 Hz
Temperature change = 0 °C
Z change           = 0 µm
Frequency drift    = 0 ppm
```

This does not change anything in Klipper. It only changes the dashboard's
reference values.

Baseline reset is useful when comparing:

- probe warm-up
- different probe heights
- different target materials
- bed-temperature changes
- sensor-temperature changes
- different `reg_drive_current` values
- electronics changes
- long-duration drift

### Frequency Drift In Ppm

Frequency drift is shown in parts per million:

```text
ppm = (current_frequency - baseline_frequency) / baseline_frequency * 1,000,000
```

For example:

```text
Baseline frequency = 675000 Hz
Frequency change   = +337.5 Hz
```

gives:

```text
+500 ppm
```

Using ppm makes it easier to compare drift at different absolute sensor
frequencies.

### Session Menu And Test Timer

The **Session** menu contains controls used during a drift or stability test:

- Start test
- Stop test
- Reset timer
- Reset baseline
- Start CSV recording
- Stop CSV recording
- Download last CSV

The dashboard also provides quick **Start Test** and **Record CSV** buttons
beside the live-value cards.

Starting a test does two things:

1. resets the current measurement baseline
2. starts the elapsed test timer

Stopping a test freezes the timer. Resetting the timer clears the test time
without affecting the probe or printer.

### CSV Recording

Start a recording with:

```text
Session -> Start CSV recording
```

You can optionally enter a recording label.

Recorded files are stored in:

```text
~/eddy-dashboard/recordings/
```

Each CSV contains:

```text
wall_time_iso
wall_time_unix
sensor_time
frequency_hz
temperature_c
z_or_distance_mm
probe_type
```

CSV recording uses the dashboard's configured rolling-average output, not every
raw MCU sample.

Stop recording with:

```text
Session -> Stop CSV recording
```

The most recently completed recording can be downloaded with:

```text
Session -> Download last CSV
```

Recorded files can also be downloaded from the Recorded Runs window.

### Live Statistics

Open:

```text
Data -> Statistics
```

Statistics are calculated over the currently selected graph window.

For frequency, the dashboard displays:

- minimum
- maximum
- standard deviation
- drift rate in Hz/min
- drift rate in ppm/min

For temperature, it displays:

- minimum
- maximum
- standard deviation

For Z / Cartographer distance, it displays:

- minimum
- maximum
- standard deviation
- drift rate in µm/min

The drift rates are calculated using a linear least-squares fit over the
displayed samples.

### Recorded-Run Comparison

Open:

```text
Data -> Recorded runs / Compare
```

The dashboard lists saved CSV recordings and allows multiple runs to be selected.

Comparison options include:

```text
Frequency Δ (Hz)
Frequency drift (ppm)
Z/distance Δ (µm)
Temperature Δ (°C)
```

Each run is normalized to its own starting value and plotted against elapsed
time. This makes it easier to compare tests such as:

- different probe heights
- cold-start vs warmed-up behavior
- different bed temperatures
- different target materials
- different BTT Eddy drive-current values
- hardware or electronics changes

Up to several runs can be displayed together.

## Settings Reference

### Sample Averaging

Open:

```text
Settings -> Rolling sample average
```

Allowed range:

```text
1 to 200 samples
```

A value of `1` disables additional dashboard-side averaging. Higher values
smooth short-term noise but also reduce responsiveness to fast changes.

CSV recordings and graph points use the averaged values.

### Graph Colors

Frequency, temperature, and Z/distance graph colors can be changed from Settings.
The selected colors are saved in `eddy_dashboard_config.json`.

### Saved Files

The dashboard stores local state beside `eddy_dashboard.py`:

```text
eddy_dashboard_config.json
recordings/
```

The config file stores dashboard preferences such as Moonraker settings, probe
settings, averaging, colors, and pasted calibration/model text. It does not save
the web bind host or port.

## How Data Collection Works

A typical data path looks like this:

```text
Probe
  |
  v
Klipper
  |
  v
Moonraker
  |
  v
eddy_dashboard.py
  |-- live frequency
  |-- probe temperature
  |-- calculated Z / Cartographer distance
  |-- baseline drift statistics
  v
Web browser
```

### BTT Eddy Mode

For BTT Eddy-style probes, the application connects to Klipper through
Moonraker's Klipper socket and subscribes to:

```text
ldc1612/dump_ldc1612
```

Klipper returns LDC1612 sensor samples containing:

```text
time
frequency
z
```

The dashboard averages each incoming Klipper batch before plotting it. This
reduces browser load while still providing a smooth live graph.

Temperature is read separately from the configured Klipper
`[temperature_probe ...]` object.

### Cartographer V3 Mode

The current Cartographer3D plugin is handled differently from BTT Eddy.

The dashboard reads the Klipper object:

```text
cartographer
```

and uses:

```text
cartographer.mcu.last_sample
```

The live Cartographer sample contains values such as:

```text
frequency
time
position
temperature
raw_count
```

Example Klipper/Moonraker object structure:

```text
cartographer
`-- mcu
    `-- last_sample
        |-- frequency
        |-- time
        |-- position
        |-- temperature
        `-- raw_count
```

#### Why The Dashboard Starts A Cartographer Stream

The current Cartographer3D plugin does not continuously update `last_sample`
while the sensor is idle.

To obtain continuously updating live data, the dashboard starts:

```text
CARTOGRAPHER_STREAM ACTION=START
```

through Moonraker.

When the dashboard stops or changes modes, it stops the stream with:

```text
CARTOGRAPHER_STREAM ACTION=STOP
```

The dashboard also periodically recycles the stream session. This is intentional
because Cartographer's diagnostic streaming session stores samples during the
active session. Recycling prevents an indefinitely running dashboard from
allowing that sample list to grow without limit.

## Notes On Automatic Startup

Automatic startup is not recommended for normal use. The dashboard is
unauthenticated and is intended to be started manually when you need diagnostic
data, then stopped when you are done.