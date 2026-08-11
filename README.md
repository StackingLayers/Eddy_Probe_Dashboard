# Eddy Live Dashboard

A lightweight, read-only live dashboard for Klipper eddy-current probes using the LDC1612 sensor interface.

The dashboard connects to Klipper through Moonraker, reads the live LDC1612 frequency stream, reads the probe temperature, and optionally converts the measured frequency into a calibration-equivalent Z position using the probe's existing Klipper calibration data.

It is intended as a diagnostic and development tool for investigating probe drift, thermal behavior, repeatability, sensor stability, and other eddy-current probe behavior.

## Features

- Live scrolling frequency graph
- Live probe temperature graph
- Live calibration-equivalent Z graph
- Current frequency display
- Current probe temperature display
- Current calibration-equivalent Z display
- Frequency drift in ppm
- Frequency change from baseline
- Temperature change from baseline
- Z change from baseline in microns
- Resettable baseline
- Selectable graph windows:
  - 1 minute
  - 5 minutes
  - 20 minutes
  - 1 hour
- User-configurable Eddy sensor name
- User-configurable temperature probe name
- User-configurable Moonraker host and port
- Paste-in Klipper calibration data
- Calibration data is saved between restarts
- Automatically reconnects to Klipper if the connection is interrupted
- Read-only operation
- No Klipper source modifications required
- No printer configuration changes required

## Screenshot

![Eddy Live Dashboard](images/dashboard.png)

## How it works

The application uses Moonraker's Klipper socket interface to subscribe to the LDC1612 diagnostic stream:

```text
ldc1612/dump_ldc1612
```

Klipper provides raw sensor data containing:

```text
time, frequency, z
```

The dashboard averages each incoming batch before plotting it. This reduces the amount of browser data while still producing a smooth live graph.

Probe temperature is read separately from Moonraker using the configured:

```ini
[temperature_probe ...]
```

object.

The dashboard itself does not write to Klipper and does not modify probe calibration, Z offset, configuration files, firmware, or printer state.

## Requirements

A Klipper installation with:

- Moonraker
- An eddy-current probe using Klipper's `probe_eddy_current`
- LDC1612 diagnostic stream support
- Python 3
- Network access to the printer

The application was developed around a BTT Eddy-style LDC1612 probe, but the sensor name is configurable and it may work with other Klipper probes using the same LDC1612 interface.

### Python packages

The dashboard requires:

```text
Flask
websocket-client
```

## Installation

SSH into the Klipper host.

Create a directory for the dashboard:

```bash
mkdir -p ~/eddy-dashboard
cd ~/eddy-dashboard
```

Create a Python virtual environment:

```bash
python3 -m venv venv
```

If the `venv` module is not installed:

```bash
sudo apt update
sudo apt install python3-venv
```

Activate the environment:

```bash
source venv/bin/activate
```

Install the required packages:

```bash
pip install Flask websocket-client
```

Copy the dashboard script into the directory and name it:

```text
eddy_dashboard.py
```

The resulting directory should look similar to:

```text
~/eddy-dashboard/
├── eddy_dashboard.py
└── venv/
```

## Running manually

Activate the virtual environment:

```bash
cd ~/eddy-dashboard
source venv/bin/activate
```

Start the dashboard:

```bash
python3 eddy_dashboard.py
```

You should see output similar to:

```text
Eddy dashboard starting on port 8085

Connected to Klipper LDC stream
 * Running on http://127.0.0.1:8085
 * Running on http://<printer-ip>:8085
```

Open a browser and go to:

```text
http://PRINTER_IP:8085
```

For example:

```text
http://192.168.1.100:8085
```

If your local hostname resolves correctly, you may also be able to use:

```text
http://voron:8085
```

## First-time setup

Open the dashboard and click **Settings**.

Configure the following fields.

### Moonraker host

If the dashboard runs on the same machine as Moonraker, use:

```text
127.0.0.1
```

### Moonraker port

The normal Moonraker port is:

```text
7125
```

### Eddy sensor name

Enter the name used after `probe_eddy_current` in your Klipper configuration.

For example:

```ini
[probe_eddy_current btt_eddy]
```

would use:

```text
btt_eddy
```

### Temperature probe name

Enter the name used after `temperature_probe`.

For example:

```ini
[temperature_probe btt_eddy]
```

would use:

```text
btt_eddy
```

If your probe does not expose a temperature probe, leave this field blank.

## Calibration data

The calibration-equivalent Z display uses the probe's existing Klipper calibration table.

The calibration can be copied directly from the `SAVE_CONFIG` section at the bottom of `printer.cfg`.

For example:

```ini
#*# [probe_eddy_current btt_eddy]
#*# reg_drive_current = 27
#*# calibrate =
#*#   0.050000:678437.389,0.090000:678275.407,0.130000:678111.331,
#*#   0.170000:677946.422,0.210000:677783.829,0.250000:677620.523,
#*#   0.290000:677460.811,0.330000:677298.268,0.370000:677139.817,
#*#   0.410000:676981.183,0.450000:676828.249,0.490000:676671.691
```

You do not need to clean the text before pasting it.

The parser extracts numeric pairs in the form:

```text
Z:frequency
```

For example:

```text
0.050000:678437.389
```

The parser ignores surrounding text such as:

```text
calibrate =
#*#
```

and line breaks.

After pasting the calibration, click **Save settings**.

The dashboard will report how many calibration points were detected.

## Calibration-equivalent Z

The green Z graph is calculated by linearly interpolating between the calibration points supplied by Klipper.

It should be treated as:

> the Z position that corresponds to the currently measured frequency according to the stored probe calibration.

It is useful for observing apparent probe movement and drift.

It is not necessarily the true physical distance between the probe and the target.

This is especially important if the probe is positioned outside the distance range represented by the calibration table.

The dashboard deliberately does not extrapolate beyond the supplied calibration range.

If the measured frequency is outside that range, it displays:

```text
Out of calibration
```

## Reset baseline

The **Reset baseline** button stores the current values as the new reference point.

After resetting:

```text
Frequency change = 0 Hz
Temperature change = 0 °C
Z change = 0 µm
Frequency drift = 0 ppm
```

This does not change anything in Klipper.

It only changes the reference used by the dashboard.

This is useful when testing:

- probe warm-up
- thermal drift
- different `reg_drive_current` settings
- different probe heights
- bed temperature changes
- electronics temperature changes
- repeatability
- long-duration stability

## Frequency drift in ppm

The dashboard reports relative frequency movement in parts per million:

```text
ppm = (current_frequency - baseline_frequency)
      / baseline_frequency
      × 1,000,000
```

For example, if a probe starts at:

```text
675000 Hz
```

and increases by:

```text
337.5 Hz
```

the drift is:

```text
500 ppm
```

Using ppm makes it easier to compare drift between tests performed at different sensor frequencies.

## Graph windows

The graph window can be changed from the dashboard.

Available options are:

```text
1 minute
5 minutes
20 minutes
1 hour
```

The graphs scroll continuously as new data arrives.

The server stores recent samples in memory so changing the graph window does not immediately discard previously collected data.

## Automatic startup with systemd

After confirming that the dashboard works correctly, create a systemd service.

Create:

```bash
sudo nano /etc/systemd/system/eddy-dashboard.service
```

Example configuration:

```ini
[Unit]
Description=Eddy Live Dashboard
After=network.target klipper.service moonraker.service
Wants=klipper.service moonraker.service

[Service]
Type=simple
User=voron
WorkingDirectory=/home/voron/eddy-dashboard
ExecStart=/home/voron/eddy-dashboard/venv/bin/python /home/voron/eddy-dashboard/eddy_dashboard.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Change:

```text
User=voron
```

and:

```text
/home/voron/
```

if your Klipper host uses a different username or home directory.

Reload systemd:

```bash
sudo systemctl daemon-reload
```

Enable and start the dashboard:

```bash
sudo systemctl enable --now eddy-dashboard
```

Check its status:

```bash
systemctl status eddy-dashboard --no-pager
```

View live logs:

```bash
journalctl -u eddy-dashboard -f
```

Restart the service after updating the script:

```bash
sudo systemctl restart eddy-dashboard
```

## Configuration storage

User settings are stored in:

```text
eddy_dashboard_config.json
```

in the same directory as the Python script.

An example installation may therefore look like:

```text
~/eddy-dashboard/
├── eddy_dashboard.py
├── eddy_dashboard_config.json
└── venv/
```

The configuration file is created automatically when settings are saved.

You normally do not need to edit it manually.

## Example Klipper configuration

A probe may look similar to:

```ini
[probe_eddy_current btt_eddy]
sensor_type: ldc1612
# additional probe configuration...

[temperature_probe btt_eddy]
sensor_type: Generic 3950
# additional temperature configuration...
```

The exact configuration depends on the probe and hardware.

The dashboard does not require these sections to have the name `btt_eddy`; the names can be entered through the Settings menu.

## Troubleshooting

### Dashboard says Disconnected

Confirm Moonraker is running:

```bash
systemctl status moonraker
```

Check that Moonraker is available:

```text
http://PRINTER_IP:7125
```

Verify that the dashboard's Moonraker host and port are correct.

If the dashboard runs directly on the Klipper host, the recommended host setting is:

```text
127.0.0.1
```

and the normal Moonraker port is:

```text
7125
```

### Klipper LDC stream does not connect

The dashboard uses:

```text
ldc1612/dump_ldc1612
```

Your probe must use Klipper's LDC1612 implementation and expose that diagnostic endpoint.

Check the console output:

```bash
python3 eddy_dashboard.py
```

or, when using systemd:

```bash
journalctl -u eddy-dashboard -f
```

### No temperature is displayed

Verify that your Klipper configuration has a matching temperature object.

For example:

```ini
[temperature_probe btt_eddy]
```

Then enter:

```text
btt_eddy
```

as the temperature probe name in Settings.

If no temperature probe exists, leave the field blank.

### Z shows "Out of calibration"

This means the measured sensor frequency is outside the frequency range contained in the pasted calibration table.

This may happen when:

- the probe is far above the bed
- the probe is near a different metal target
- the calibration belongs to a different probe
- the calibration has not been pasted
- the probe frequency has moved outside the calibrated range

The dashboard intentionally does not extrapolate Z outside the calibration table.

### Calibration data is rejected

At least two valid calibration pairs are required.

Valid pairs look like:

```text
0.050000:678437.389
```

You can normally paste the complete `calibrate =` section directly from `printer.cfg`.

### Port 8085 is already in use

Check what is using the port:

```bash
sudo ss -ltnp | grep 8085
```

Stop the other process or change:

```python
"web_port": 8085
```

in the default configuration before the first run.

If a configuration file already exists, the saved configuration will be used.

## Security

The dashboard is intended for use on a trusted local network.

By default it listens on:

```text
0.0.0.0:8085
```

which allows other devices on the local network to open it.

There is currently no built-in authentication.

Do not expose port `8085` directly to the public Internet.

The dashboard is read-only with respect to Klipper, but the web interface itself should still be treated as a local diagnostic service.

## Does this modify Klipper?

No.

The application does not modify:

- `printer.cfg`
- Klipper source files
- probe calibration
- `reg_drive_current`
- Z offset
- firmware
- MCU configuration
- printer movement
- heater state

It subscribes to diagnostic data and reads temperature information.

The **Reset baseline** button only changes values stored inside the dashboard.

## Performance

The LDC1612 sensor can produce data at a high sample rate.

Rather than sending every raw sample directly to the browser, the dashboard averages each incoming Klipper batch and plots the resulting point.

This keeps the live display responsive while still preserving small frequency changes and long-term drift trends.

The dashboard is intended for diagnostics rather than replacing Klipper's internal probe calculations.

## Known limitations

- Requires a Klipper probe that exposes the `ldc1612/dump_ldc1612` endpoint.
- Calibration-equivalent Z is only valid within the supplied calibration frequency range.
- Temperature support depends on a matching `temperature_probe` object.
- No authentication is currently included.
- Data is currently stored in memory only.
- Closing or restarting the application clears the graph history.
- CSV recording is not currently included.

## Updating

Replace:

```text
eddy_dashboard.py
```

with the newer version and restart the service:

```bash
sudo systemctl restart eddy-dashboard
```

If running manually:

```bash
cd ~/eddy-dashboard
source venv/bin/activate
python3 eddy_dashboard.py
```

## Removing

Stop and disable the service:

```bash
sudo systemctl disable --now eddy-dashboard
```

Remove the service file:

```bash
sudo rm /etc/systemd/system/eddy-dashboard.service
sudo systemctl daemon-reload
```

Remove the dashboard directory if desired:

```bash
rm -rf ~/eddy-dashboard
```

## Disclaimer

This project is a diagnostic tool.

Always verify probe behavior using the normal Klipper calibration and printer setup procedures before relying on measurements for printer operation.

Do not use the dashboard's calibration-equivalent Z value as a replacement for Klipper's normal probing, homing, Z-offset, or safety logic.

