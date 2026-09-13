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
- `GET /api/v1/system/processes`
- `GET /api/v1/rgb`
- `POST /api/v1/rgb`
- `GET /api/v1/fans`
- `POST /api/v1/fans`
- `GET /metrics`

Configuration defaults to `/etc/homelab-core/config.toml`. Use `HOMELAB_CORE_CONFIG`
to select another file during development. Missing configuration is valid and uses
safe defaults. RGB is disabled unless explicitly enabled.

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

Run tests with `pytest`.

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

Animated pattern types are `breathing`, `flow`, `rainbow`, and `hue_cycle`.
Animated patterns run at 30 frames per second and are replaced when a new RGB
command is applied. The existing flat `color` field remains shorthand for a
uniform `solid` pattern.

On Pironman 5 hardware, the external fan motor is controlled by BCM GPIO6 and
GPIO5 gates the RGB fan lighting. The RGB pixels remain part of the same WS2812
chain configured above, so no separate fan RGB configuration is needed:

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
fan output is on. FAN API updates are runtime-only and reset to these values on
restart. Install `.[hardware]`, which provides Pi 5-compatible `RPi.GPIO`
compatibility through `rpi-lgpio`, grant the service account GPIO access, and do
not run the official Pironman service concurrently. `rpi-lgpio` cannot coexist
with the legacy `RPi.GPIO` package in one virtual environment.
