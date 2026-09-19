# homelab-core Implementation Plan

## 1. Goal

Build `homelab-core`, a general-purpose local agent for managing and observing a homelab node.

The first target is a Raspberry Pi with a Pironman 5 Max case, but the architecture must not depend on that product name or hardware. The service should provide one process that owns local system resources, exposes a small API, exports Prometheus metrics, and supports future integrations such as WiFi-connected RGB devices.

## 2. Initial Scope

### Required managers

- System resources and telemetry
- CPU and external fan control
- RGB lighting
- OLED display
- Physical power button
- Read-only Docker container overview

### Required platform features

- One long-running process
- One asyncio event loop
- One owner for each local hardware resource
- Configuration persistence for API-controlled desired state
- Structured, rotating logs
- Graceful shutdown and hardware cleanup
- systemd service integration
- Prometheus `/metrics` endpoint

### Explicitly deferred

- Process killing, restarting, or other process management
- InfluxDB or application-managed historical metrics
- Web dashboard
- Remote configuration synchronization
- Full compatibility with the official Pironman CLI
- PiPower5 UPS functionality
- NAS-specific MCU functionality
- IR receiver behavior
- Automatic discovery of external devices
- Advanced RGB effects and complex OLED page systems

## 3. Architecture

```text
homelab-core process
├── Supervisor
├── Configuration store
├── State store
├── Event bus
├── Command bus
├── FastAPI server
├── Prometheus exporter
├── System manager
├── Fan manager
├── RGB manager
├── OLED manager
├── Power-button manager
├── Docker manager
└── Optional integration managers
```

All managers run in one Python process and share one asyncio event loop. FastAPI is the transport layer for the same process; it is not a separate service.

The API must communicate with managers through commands and queries, not through direct hardware access.

```text
HTTP request
  -> API validation
  -> command queue
  -> manager
  -> device adapter
  -> hardware
  -> state update
```

## 4. Repository Structure

```text
homelab-core/
├── pyproject.toml
├── README.md
├── PLAN.md
├── src/
│   └── homelab_core/
│       ├── main.py
│       ├── config.py
│       ├── logging.py
│       ├── runtime/
│       │   ├── supervisor.py
│       │   ├── state.py
│       │   ├── events.py
│       │   └── commands.py
│       ├── api/
│       │   ├── server.py
│       │   ├── auth.py
│       │   └── routes/
│       ├── managers/
│       │   ├── base.py
│       │   ├── system.py
│       │   ├── fans.py
│       │   ├── rgb.py
│       │   ├── oled.py
│       │   ├── power_button.py
│       │   └── docker.py
│       ├── devices/
│       │   ├── interfaces.py
│       │   ├── pm_auto.py
│       │   ├── ws2812.py
│       │   ├── ssd1306.py
│       │   ├── gpio_fan.py
│       │   └── power_button.py
│       ├── integrations/
│       └── pages/
└── tests/
```

The `managers` layer owns behavior and policy. The `devices` layer owns hardware protocols. The API must depend on manager interfaces, never on `pm_auto` or GPIO libraries directly.

## 5. Manager Contract

Every manager should have a consistent lifecycle and health model.

```python
class Manager:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> dict: ...
    async def snapshot(self) -> dict: ...
```

Managers that accept changes should additionally expose typed commands, for example:

```python
class RgbManager(Manager):
    async def set_state(self, state: RgbState) -> RgbState: ...
```

Manager requirements:

- Validate inputs at the manager boundary.
- Own their background tasks.
- Report unavailable hardware without crashing the whole service.
- Clean up resources on shutdown.
- Publish state changes and errors through the event bus.
- Avoid direct dependencies on API request objects.

Required manager states:

- `starting`
- `ready`
- `degraded`
- `stopped`
- `failed`

## 6. Core Runtime

### Supervisor

The supervisor will:

- Start managers in dependency order.
- Start the API after the state and managers are initialized.
- Stop managers in reverse dependency order.
- Handle `SIGTERM`, `SIGINT`, and service cancellation.
- Report manager health.
- Prevent duplicate hardware ownership through a lock or process check.
- Restart optional integrations only when explicitly configured.

### State store

The state store will maintain the latest desired state, actual state, health, errors, and timestamps.

```json
{
  "system": {},
  "fans": {},
  "rgb": {},
  "oled": {},
  "power_button": {},
  "docker": {},
  "integrations": {}
}
```

The state store is the source for API responses and Prometheus gauges. Prometheus is not the source of truth.

### Command bus

Hardware-changing operations must be serialized. API handlers enqueue commands rather than writing to devices directly.

Each command should have:

- Command type
- Validated payload
- Request ID
- Creation timestamp
- Timeout
- Result or error

Use per-resource locks or queues so RGB, OLED, GPIO, and power operations cannot race.

## 7. System Manager

The first version will be read-only.

Collect:

- Raspberry Pi model and OS information
- Kernel version and architecture
- CPU model and core count
- Overall CPU usage
- Per-core CPU usage
- CPU frequency
- CPU temperature
- Load averages
- Uptime
- Memory total, used, available, and percentage
- Storage devices, mounts, capacity, and usage
- Disk temperatures where available
- Network interfaces, addresses, and MAC addresses
- Network receive and transmit rates
- Process summary

Use `psutil` where practical and `/proc`, `/sys`, `lsblk`, or `ip` where necessary.

Process details should be exposed through the normal API, not as one Prometheus time series per process. PID, command line, and dynamic process-name labels would create high-cardinality metrics.

## 8. Fan Manager

Treat the two fan systems separately.

### CPU cooling fan

- Preserve the Raspberry Pi device-tree overlay.
- Leave primary thermal control to the kernel initially.
- Read cooling-device and hwmon state where available.
- Do not implement replacement thermal control until behavior is verified on the target OS.

### External GPIO fans

- Control BCM GPIO6.
- Treat the two external fans as one electrically coupled output.
- Support `off`, `on`, and temperature-based modes.
- Add the official modes only after the basic behavior is reliable.
- Control the Max fan LED on BCM GPIO5 with `on`, `off`, and `follow` modes.
- Set a safe startup state.
- Set a safe shutdown state.

The manager must not assume that the CPU fan and external GPIO fans are interchangeable.

## 9. RGB Manager

The RGB manager must exclusively own the local RGB device.

Initial capabilities:

- Enable and disable
- Solid color
- Brightness
- LED count
- Clear LEDs

Later capabilities:

- Breathing
- Flow and reverse flow
- Rainbow and reverse rainbow
- Hue cycle
- Animation speed

The standard Max RGB path is WS2812 over SPI, normally `/dev/spidev0.0`, with data on GPIO10 and GRB pixel ordering. Hardware details must be hidden behind a device interface.

```python
class RgbDevice:
    async def set_pixels(self, pixels): ...
    async def clear(self): ...
    async def close(self): ...
```

Do not enable both WS2812 and SunFounder I2C RGB implementations unless the physical hardware has been verified to contain both.

## 10. OLED Manager

Initial capabilities:

- SSD1306 initialization
- I2C address detection/configuration
- 128x64 framebuffer rendering
- One static status page
- Refresh loop
- Display clear
- Display enable/disable
- Rotation by 0 or 180 degrees
- Shutdown screen
- Sleep timeout

Later capabilities:

- Multiple pages
- Page rotation
- Button-driven page navigation
- Custom page plugins

OLED pages should receive a read-only state snapshot and return a framebuffer or image. They should not access hardware directly.

## 11. Power-Button Manager

Preserve the safety-critical behavior:

- Read the Linux input device named `pwr_button`.
- Detect click, double-click, and long press.
- Publish button events rather than embedding OLED or shutdown logic.
- Wake or navigate the OLED through event subscribers.
- Initiate an orderly system shutdown on the configured long-press action.
- Release the input device on exit.

Shutdown and reboot commands require stronger authorization than ordinary settings changes.

## 12. Docker Manager

The first version is read-only.

Expose:

- Container ID and name
- Image
- Running state
- Status
- Health-check state
- Restart count
- Published ports
- Optional basic CPU and memory usage

Use the Docker Engine API through `/var/run/docker.sock` or a carefully scoped alternative. Docker socket access is effectively root-equivalent, so the integration should be optional and documented separately from general service permissions.

## 13. Integrations

External devices should implement the same manager/device patterns as local hardware.

```text
RgbManager
├── LocalWs2812Device
└── WifiRgbDevice
```

An integration should declare:

- Stable ID
- Device type
- Transport
- Capabilities
- Configuration schema
- Health status

The core should not care whether an RGB device uses SPI, HTTP, UDP, MQTT, or another transport.

## 14. API

The API will be implemented with FastAPI in the same process as the managers.

Initial endpoints:

```text
GET  /api/v1/overview
GET  /api/v1/system
GET  /api/v1/system/processes
GET  /api/v1/fans
GET  /api/v1/rgb
GET  /api/v1/oled
GET  /api/v1/docker
GET  /api/v1/health

POST /api/v1/fans
POST /api/v1/rgb
POST /api/v1/oled
POST /api/v1/system/shutdown
```

API requirements:

- Pydantic request and response models.
- Explicit validation and bounds checking.
- Request IDs in logs and responses.
- Timeouts for hardware commands.
- Consistent error responses.
- Separate read and write authorization.
- Strong authorization for shutdown and reboot.

Do not expose the API directly to the public internet. Prefer localhost plus Tailscale, a LAN-only bind address, or a reverse proxy.

## 15. Prometheus Metrics

Expose current metrics from:

```text
GET /metrics
```

Use the Python `prometheus-client` library. Prometheus itself will scrape and store historical data; Grafana will query Prometheus and render graphs.

Initial metrics:

```text
homelab_cpu_usage_ratio
homelab_cpu_temperature_celsius
homelab_memory_used_bytes
homelab_memory_available_bytes
homelab_storage_used_bytes
homelab_storage_free_bytes
homelab_network_receive_bytes_total
homelab_network_transmit_bytes_total
homelab_fan_state
homelab_fan_speed_rpm
homelab_rgb_enabled
homelab_rgb_brightness
homelab_docker_containers_running
homelab_manager_up
```

Use stable, low-cardinality labels such as device ID, filesystem, interface, or manager name. Avoid labels containing PIDs, command lines, arbitrary container IDs, or rapidly changing names.

The metrics exporter must remain optional so `homelab-core` can run without a Prometheus dependency.

## 16. `pm_auto` Adoption Strategy

Do not make `PMAuto` the architectural foundation.

Use a staged adapter strategy:

1. Audit and test the relevant `pm_auto` addons independently.
2. Implement `RgbDevice`, `OledDevice`, and fan device interfaces.
3. Use `pm_auto` selectively behind those interfaces where it is reliable.
4. Keep all `pm_auto` imports inside `devices/pm_auto.py` or addon-specific adapters.
5. Replace individual adapters with custom implementations when needed.

Do not run the official `pironman5` service, `pm_dashboard`, and `homelab-core` simultaneously. Only one process should own each GPIO, SPI, I2C, and input resource.

`pm_auto` is an in-process Python package. It does not provide the API, persistence, or remote backend integration required by this project.

## 17. Configuration

Use a versioned configuration file:

```text
/etc/homelab-core/config.toml
```

Configuration areas:

- Enabled managers
- Poll intervals
- Fan modes and thresholds
- RGB defaults
- OLED settings
- Docker integration
- Prometheus exporter
- API bind address and port
- External integrations

Requirements:

- Validate configuration on startup.
- Reject unknown or unsafe values where appropriate.
- Support environment-variable overrides for secrets.
- Write changes atomically using temporary-file-then-rename.
- Keep desired configuration separate from actual device state.

## 18. Logging and Diagnostics

Use structured logs with:

- Timestamp
- Log level
- Manager name
- Request ID where applicable
- Device ID where applicable
- Error details

Provide:

- Rotating file logs
- Console logs for systemd/journalctl
- Manager health status
- Last successful operation timestamp
- Last error and error count
- Startup hardware detection report

Do not log API credentials, tokens, or full Docker socket responses.

## 19. systemd Deployment

The service should:

- Run under a dedicated service account where possible.
- Have access only to required GPIO, SPI, I2C, input, and optionally Docker resources.
- Start after required device and network targets.
- Restart on failure.
- Stop cleanly on `SIGTERM`.
- Avoid requiring root unless hardware permissions make it unavoidable.

The device-tree overlay remains a system-level installation concern and must not be removed as part of replacing the userspace service.

## 20. Testing Strategy

### Unit tests

- Configuration validation
- State transitions
- Command validation
- Event publication
- Fan mode calculations
- RGB color parsing and brightness scaling
- OLED page rendering
- Prometheus metric names and values
- Docker response normalization

### Hardware-independent integration tests

- Fake RGB device
- Fake OLED device
- Fake GPIO device
- Fake power-button event source
- Fake Docker client
- Full supervisor startup and shutdown
- API request-to-command flow

### Hardware tests

- Detect I2C OLED at `0x3C`.
- Render and clear OLED.
- Set RGB solid colors and clear LEDs.
- Toggle external fans.
- Verify fan LED modes.
- Verify power-button event handling.
- Verify cleanup after `SIGTERM`.
- Verify the CPU fan remains kernel-controlled.

No hardware test should require the official dashboard.

## 21. Implementation Milestones

### Milestone 1: Project foundation

- Create Python package and test setup.
- Add configuration models.
- Add structured logging.
- Add supervisor, state store, event bus, and command bus.
- Add a minimal systemd unit for development.

### Milestone 2: Read-only service

- Implement system manager.
- Implement health endpoint.
- Implement overview endpoint.
- Add Prometheus exporter.
- Add fake-device test infrastructure.

### Milestone 3: RGB

- Implement RGB device interface.
- Audit the `pm_auto` WS2812 addon.
- Implement `pm_auto` adapter or custom device.
- Add off, solid color, brightness, and cleanup.
- Add `/api/v1/rgb`.

### Milestone 4: OLED

- Implement SSD1306 device interface.
- Render one status page.
- Add enable, disable, rotation, sleep, and cleanup.
- Add `/api/v1/oled`.

### Milestone 5: Fans

- Confirm kernel CPU-fan behavior.
- Implement external GPIO fan adapter.
- Implement fan LED control.
- Add read and write fan endpoints.

### Milestone 6: Power button

- Implement Linux input event reader.
- Add click and long-press events.
- Integrate OLED wake/navigation.
- Integrate orderly shutdown.

### Milestone 7: Docker overview

- Add optional Docker manager.
- Normalize container data.
- Add read-only endpoint and metrics.

### Milestone 8: Remote integration

- Add network deployment configuration.
- Add backend client or webhook/event mechanism.
- Add WiFi RGB integration using the same device interface.

### Milestone 9: Replace or reduce `pm_auto`

- Review adapter behavior and test coverage.
- Replace unreliable or unnecessary addons individually.
- Remove `pm_auto` only after hardware parity is demonstrated.

## 22. Definition of Done for the First Usable Release

- `homelab-core` runs as one systemd-managed process.
- The service starts with no official Pironman process running.
- System telemetry is available through the API and Prometheus.
- RGB can be enabled, disabled, colored, brightened, and cleared.
- OLED displays a status page and clears on shutdown.
- External fans and the Max fan LED behave safely.
- The physical power button still supports orderly shutdown.
- Docker containers are visible without management operations.
- Hardware commands are serialized.
- Configuration survives restart.
- `/metrics` contains stable metrics suitable for Prometheus.
- Shutdown leaves RGB, OLED, and GPIO resources in safe states.
- Hardware-independent tests pass without physical hardware.
