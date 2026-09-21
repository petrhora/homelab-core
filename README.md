# homelab-core

`homelab-core` is a single-process local agent for homelab telemetry and hardware control.

The initial implementation provides a hardware-independent read-only service:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
homelab-core
```

Endpoints are available at `http://127.0.0.1:8000`:

- `GET /api/v1/health`
- `GET /api/v1/overview`
- `GET /api/v1/system`
- `GET /api/v1/system/instant`
- `GET /api/v1/system/processes`
- `GET /api/v1/rgb`
- `POST /api/v1/rgb`
- `GET /api/v1/oled`
- `POST /api/v1/oled`
- `GET /api/v1/fans`
- `POST /api/v1/fans`
- `GET /api/v1/lighting`
- `POST /api/v1/lighting`
- `GET /api/v1/power-button`
- `GET /metrics`

Configuration defaults to `/etc/homelab-core/config.toml`. Use `HOMELAB_CORE_CONFIG`
to select another file during development. Missing configuration is valid and uses
safe defaults. RGB is disabled unless explicitly enabled.

The OLED is disabled unless explicitly enabled. It uses an SSD1306 128x64 panel
on I2C bus 1 and automatically detects address `0x3C` or `0x3D`:

```toml
[oled]
enabled = true
bus = 1
address = "auto"
refresh_interval_seconds = 1.0
sleep_timeout_seconds = 15
rotation = 0
```

Install `.[hardware]` for OLED support, enable I2C on the Raspberry Pi, and do
not run another service that owns the same I2C device. The initial display page
shows CPU, memory, and root-disk usage, CPU temperature, and the primary IPv4
address. OLED API updates are persisted in `/var/lib/homelab-core/state.json`
and restored after restart; the display is cleared during shutdown.

To enable the local WS2812 strip on a Raspberry Pi, install the hardware dependency
and create `/etc/homelab-core/config.toml`:

```toml
[rgb]
enabled = true
device = "ws2812"
spi_path = "/dev/spidev0.0"
led_count = 4
brightness = 0.25
color = "#00FF00"
spi_speed_hz = 2400000
```

Install the adapter dependency with `pip install -e '.[hardware]'`. On Raspberry
Pi OS, install `liblgpio-dev` first if it is not already present. Ensure SPI is
enabled and the service account can access `/dev/spidev0.0`. Do not run another
process that owns the same RGB hardware at the same time.

Lighting can be limited to configured daily windows. The schedule gates both the
WS2812 strip and the Pironman fan lighting, but never affects the external fan
motor. `timezone` accepts an IANA timezone; omit it to use the server's local
timezone. Windows that end before they start continue into the following day.

```toml
[lighting]
enabled = true
timezone = "Europe/Prague"

[[lighting.windows]]
start = "07:00"
end = "23:30"
days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
```

Use `POST /api/v1/lighting` to change the schedule without restarting. These
updates, like RGB and fan API updates, are persisted in
`/var/lib/homelab-core/state.json` and restored after restart:

```json
{
  "enabled": true,
  "timezone": "Europe/Prague",
  "windows": [{"start": "07:00", "end": "23:30", "days": ["mon", "tue", "wed", "thu", "fri"]}]
}
```

Run tests with `pytest`.

## Prometheus Metrics

`GET /metrics` exports current values from manager snapshots. Its stable labels are
CPU (`cpu`, `mode`), load window (`window`), disk device (`device`), filesystem
mountpoint (`mountpoint`), network interface (`interface`), fan (`fan`), and
manager (`manager`). Available series are:

- `homelab_cpu_usage_ratio{cpu="total|<index>",mode="..."}` with all CPU modes reported by the host
- `homelab_cpu_temperature_celsius` when the host exposes a temperature sensor
- `homelab_cpu_frequency_hertz` when the host reports current CPU frequency
- `homelab_memory_total_bytes`, `homelab_memory_used_bytes`, `homelab_memory_available_bytes`, `homelab_memory_cached_bytes`
- `homelab_swap_used_bytes`, `homelab_swap_total_bytes`
- `homelab_load_average{window="1m|5m|15m"}`
- `homelab_storage_used_bytes`, `homelab_storage_free_bytes`
- `homelab_disk_read_bytes_total{device="..."}`, `homelab_disk_write_bytes_total{device="..."}`
- `homelab_network_receive_bytes_total`, `homelab_network_transmit_bytes_total`
- `homelab_fan_state`, `homelab_rgb_enabled`, `homelab_rgb_brightness`
- `homelab_manager_up`, `homelab_manager_error_count`

Metrics are disabled with `metrics_enabled = false`. Docker and fan-RPM metrics
will be added when those managers provide their underlying measurements.

The physical power button is disabled by default. Enable it after verifying that Linux exposes an evdev device named `pwr_button`:

```toml
[power_button]
enabled = true
device_name = "pwr_button"
long_press_ms = 3000
long_press_action = "shutdown"
```

A single click wakes the OLED display, while a long press requests an orderly system shutdown through `systemctl`. The manager also publishes click, double-click, and long-press events for future consumers. Configure the service account with the required systemd or polkit authorization. Install `.[hardware]`, grant the service account access to the `input` group, and do not let another process own the input device.

RGB patterns can use a uniform color or an independently configured pixel frame:

```json
{
  "enabled": true,
  "brightness": 0.5,
  "pattern": {
    "type": "solid",
    "pixels": [
      "#FF0000",
      {"color": "#00FF00", "brightness": 0.25},
      "#0000FF",
      "#FFFFFF"
    ]
  }
}
```

Animated pattern types are `breathing`, `flow`, `stepper`, `gradient`,
`rainbow`, and `hue_cycle`. Animated patterns run at 30 frames per second and
are replaced when a new RGB command is applied. The existing flat `color` field
remains shorthand for a uniform `solid` pattern.

`flow` smoothly interpolates the supplied pixel colors. Its optional
`virtual_led_count` spreads those colors across a larger virtual chain, so the
physical LEDs show only a section of the transition. Use `stepper` for discrete
color jumps, with `step_leds` positions moved each `step_duration_ms`
milliseconds:

```json
{
  "pattern": {
    "type": "stepper",
    "pixels": ["#FF0000", "#00FF00", "#0000FF", "#000000"],
    "step_leds": 2,
    "step_duration_ms": 500,
    "direction": "forward"
  }
}
```

`gradient` accepts two or more colors. `loop` transitions from the final color
back to the first, while `ping_pong` reverses at each endpoint. Its optional
`virtual_led_count` also limits the physical chain to a section of a larger
gradient:

```json
{
  "pattern": {
    "type": "gradient",
    "colors": ["#FF0000", "#FF8C00", "#FFFF00", "#00FF00"],
    "speed": 2,
    "direction": "forward",
    "mode": "ping_pong",
    "virtual_led_count": 16
  }
}
```

`rainbow.virtual_led_count` makes a physical strip appear to be part of a
larger moving rainbow. It defaults to four times `led_count`; specify a larger
value for a narrower section of the spectrum:

```json
{
  "pattern": {
    "type": "rainbow",
    "speed": 0.1,
    "direction": "forward",
    "virtual_led_count": 16
  }
}
```

On Pironman 5 hardware, the external fan motor is controlled by BCM GPIO6 and
GPIO5 gates the RGB fan lighting. The RGB pixels remain part of the same WS2812
chain configured above, so no separate fan RGB configuration is needed. Set
`rgb.led_count` to the total number of strip and fan pixels so one pattern
frame updates them together. Reduced frames explicitly clear trailing pixels.

```toml
[fans]
enabled = true
poll_interval_seconds = 1

[fans.external]
mode = "auto"
on_temperature_c = 55
off_temperature_c = 50

[fans.led]
mode = "on"
```

`mode = "on"` keeps fan lighting enabled while the fan motor still follows its
temperature thresholds. `follow` enables the lighting only while the external
fan output is on. FAN API updates are persisted and override these values on
restart. Use `DELETE /api/v1/state` to clear persisted overrides; the configured
values take effect after the next restart. Install `.[hardware]`, which provides Pi 5-compatible `RPi.GPIO`
compatibility through `rpi-lgpio`, grant the service account GPIO access, and do
not run the official Pironman service concurrently. `rpi-lgpio` cannot coexist
with the legacy `RPi.GPIO` package in one virtual environment.

Persisted API values take precedence over values in
`/etc/homelab-core/config.toml` for RGB, fan, lighting, and OLED runtime settings.
Hardware paths, pins, and manager enablement remain controlled by the
configuration file. Set top-level `persistence_path` to use a different state
file.
