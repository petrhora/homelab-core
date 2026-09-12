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
safe defaults. Hardware managers are intentionally not enabled yet.

Run tests with `pytest`.
