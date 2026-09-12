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
led_count = 60
brightness = 0.25
color = "#00FF00"
spi_speed_hz = 2400000
```

Install the adapter dependency with `pip install -e '.[hardware]'`. Ensure SPI is
enabled and the service account can access `/dev/spidev0.0`. Do not run another
process that owns the same RGB hardware at the same time.

Run tests with `pytest`.
