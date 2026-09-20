from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

from ..config import LightingWindowConfig
from ..runtime.commands import CommandTimeoutError
from ..runtime.supervisor import Supervisor


MAX_PATTERN_SOURCE_LEDS = 1024


class RgbPixelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: str
    brightness: float = Field(default=1.0, ge=0, le=1)

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str) -> str:
        return _validate_color(value)


class PatternSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: str | None = None
    pixels: list[str | RgbPixelUpdate] | None = Field(default=None, max_length=MAX_PATTERN_SOURCE_LEDS)

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        return _validate_color(value) if value is not None else None


class SolidPatternUpdate(PatternSourceUpdate):
    type: Literal["solid"] = "solid"


class BreathingPatternUpdate(PatternSourceUpdate):
    type: Literal["breathing"] = "breathing"
    period_ms: int = Field(default=2000, ge=100)
    min_intensity: float = Field(default=0, ge=0, le=1)
    max_intensity: float = Field(default=1, ge=0, le=1)


class FlowPatternUpdate(PatternSourceUpdate):
    type: Literal["flow"] = "flow"
    speed: float = Field(default=1, ge=0)
    direction: Literal["forward", "reverse"] = "forward"
    virtual_led_count: int | None = Field(default=None, ge=1, le=MAX_PATTERN_SOURCE_LEDS)


class StepperPatternUpdate(PatternSourceUpdate):
    type: Literal["stepper"] = "stepper"
    step_leds: int = Field(default=1, ge=1)
    step_duration_ms: int = Field(default=1000, ge=100)
    direction: Literal["forward", "reverse"] = "forward"


class GradientPatternUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["gradient"] = "gradient"
    colors: list[str] = Field(min_length=2, max_length=MAX_PATTERN_SOURCE_LEDS)
    speed: float = Field(default=1, ge=0)
    direction: Literal["forward", "reverse"] = "forward"
    mode: Literal["loop", "ping_pong"] = "loop"
    virtual_led_count: int | None = Field(default=None, ge=1, le=MAX_PATTERN_SOURCE_LEDS)

    @field_validator("colors")
    @classmethod
    def validate_colors(cls, values: list[str]) -> list[str]:
        return [_validate_color(value) for value in values]


class RainbowPatternUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["rainbow"] = "rainbow"
    speed: float = Field(default=0.1, ge=0)
    direction: Literal["forward", "reverse"] = "forward"
    saturation: float = Field(default=1, ge=0, le=1)
    brightness: float = Field(default=1, ge=0, le=1)
    virtual_led_count: int | None = Field(default=None, ge=1, le=MAX_PATTERN_SOURCE_LEDS)


class HueCyclePatternUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hue_cycle"] = "hue_cycle"
    speed: float = Field(default=0.1, ge=0)
    saturation: float = Field(default=1, ge=0, le=1)
    brightness: float = Field(default=1, ge=0, le=1)


PatternUpdate = Annotated[
    SolidPatternUpdate | BreathingPatternUpdate | FlowPatternUpdate | StepperPatternUpdate | GradientPatternUpdate |
    RainbowPatternUpdate | HueCyclePatternUpdate,
    Field(discriminator="type"),
]


class RgbUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    color: str | None = None
    brightness: float | None = Field(default=None, ge=0, le=1)
    led_count: int | None = Field(default=None, ge=1, le=1024)
    clear: bool = False
    pattern: PatternUpdate | None = None

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        return _validate_color(value) if value is not None else None


class FanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["off", "on", "auto"] | None = None
    on_temperature_c: float | None = Field(default=None, ge=0, le=120)
    off_temperature_c: float | None = Field(default=None, ge=0, le=120)
    led_mode: Literal["off", "on", "follow"] | None = None


class OledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    sleep_timeout_seconds: float | None = Field(default=None, ge=0, le=3600)


class LightingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    timezone: str | None = None
    windows: list[LightingWindowConfig] | None = None


def _validate_color(value: str) -> str:
    if len(value) != 7 or not value.startswith("#"):
        raise ValueError("color must be in #RRGGBB format")
    try:
        int(value[1:], 16)
    except ValueError:
        raise ValueError("color must be in #RRGGBB format") from None
    return value


def create_app(supervisor: Supervisor, metrics_enabled: bool = True) -> FastAPI:
    app = FastAPI(title="homelab-core", version="0.1.0")

    @app.get("/api/v1/health")
    async def health() -> dict:
        return {"managers": await supervisor.health()}

    @app.get("/api/v1/overview")
    async def overview() -> dict:
        return {"health": await supervisor.health(), "state": await supervisor.state.snapshot()}

    @app.get("/api/v1/system")
    async def system() -> dict:
        return await supervisor.managers["system"].snapshot()

    @app.get("/api/v1/system/processes")
    async def processes(offset: int = Query(default=0, ge=0, le=10_000),
                       limit: int = Query(default=100, ge=1, le=500)) -> dict:
        return await supervisor.managers["system"].processes(offset, limit)  # type: ignore[attr-defined]

    @app.get("/api/v1/rgb")
    async def rgb() -> dict:
        return await supervisor.managers["rgb"].snapshot()

    @app.get("/api/v1/oled")
    async def oled() -> dict:
        return await supervisor.managers["oled"].snapshot()

    @app.post("/api/v1/oled")
    async def update_oled(update: OledUpdate) -> dict:
        try:
            result = await supervisor.commands.dispatch("oled.update", update.model_dump(exclude_none=True))
            await supervisor.persist_result("oled", result)
            return result
        except CommandTimeoutError as error:
            raise HTTPException(status_code=504, detail=str(error)) from error
        except (ValueError, TimeoutError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/v1/rgb")
    async def update_rgb(update: RgbUpdate) -> dict:
        try:
            result = await supervisor.commands.dispatch("rgb.update", update.model_dump(exclude_none=True))
            await supervisor.persist_result("rgb", result)
            return result
        except CommandTimeoutError as error:
            raise HTTPException(status_code=504, detail=str(error)) from error
        except (ValueError, TimeoutError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/v1/fans")
    async def fans() -> dict:
        return await supervisor.managers["fans"].snapshot()

    @app.get("/api/v1/lighting")
    async def lighting() -> dict:
        return await supervisor.managers["lighting"].snapshot()

    @app.post("/api/v1/lighting")
    async def update_lighting(update: LightingUpdate) -> dict:
        try:
            result = await supervisor.commands.dispatch("lighting.update", update.model_dump(exclude_unset=True))
            await supervisor.persist_result("lighting", result)
            return result
        except CommandTimeoutError as error:
            raise HTTPException(status_code=504, detail=str(error)) from error
        except (ValueError, TimeoutError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/v1/power-button")
    async def power_button() -> dict:
        return await supervisor.managers["power_button"].snapshot()

    @app.post("/api/v1/fans")
    async def update_fans(update: FanUpdate) -> dict:
        try:
            result = await supervisor.commands.dispatch("fans.update", update.model_dump(exclude_none=True))
            await supervisor.persist_result("fans", result)
            return result
        except CommandTimeoutError as error:
            raise HTTPException(status_code=504, detail=str(error)) from error
        except (ValueError, TimeoutError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.delete("/api/v1/state")
    async def clear_state() -> dict:
        await supervisor.clear_persisted_state()
        return {"cleared": True, "effective_after_restart": True}

    @app.get("/metrics")
    async def metrics() -> Response:
        if not metrics_enabled:
            return Response(status_code=404)
        # A registry per scrape ensures labels for detached mounts and network
        # interfaces disappear as soon as they are absent from the snapshot.
        registry = CollectorRegistry()
        cpu = Gauge("homelab_cpu_usage_ratio", "CPU usage ratio by mode", ["mode", "cpu"], registry=registry)
        memory_used = Gauge("homelab_memory_used_bytes", "Used memory", registry=registry)
        memory_available = Gauge("homelab_memory_available_bytes", "Available memory", registry=registry)
        memory_total = Gauge("homelab_memory_total_bytes", "Total memory", registry=registry)
        memory_cached = Gauge("homelab_memory_cached_bytes", "Cached memory", registry=registry)
        swap_used = Gauge("homelab_swap_used_bytes", "Used swap", registry=registry)
        swap_total = Gauge("homelab_swap_total_bytes", "Total swap", registry=registry)
        load_average = Gauge("homelab_load_average", "System load average", ["window"], registry=registry)
        storage_used = Gauge("homelab_storage_used_bytes", "Used filesystem space", ["mountpoint"], registry=registry)
        storage_free = Gauge("homelab_storage_free_bytes", "Free filesystem space", ["mountpoint"], registry=registry)
        disk_read = Gauge("homelab_disk_read_bytes_total", "Disk bytes read", ["device"], registry=registry)
        disk_write = Gauge("homelab_disk_write_bytes_total", "Disk bytes written", ["device"], registry=registry)
        network_receive = Gauge("homelab_network_receive_bytes_total", "Network bytes received", ["interface"], registry=registry)
        network_transmit = Gauge("homelab_network_transmit_bytes_total", "Network bytes transmitted", ["interface"], registry=registry)
        rgb_enabled = Gauge("homelab_rgb_enabled", "Whether RGB output is enabled", registry=registry)
        rgb_brightness = Gauge("homelab_rgb_brightness", "RGB brightness ratio", registry=registry)
        fan_state = Gauge("homelab_fan_state", "Whether an external fan output is enabled", ["fan"], registry=registry)
        manager_up = Gauge("homelab_manager_up", "Whether a manager is ready", ["manager"], registry=registry)
        manager_errors = Gauge("homelab_manager_error_count", "Manager error count since process start", ["manager"], registry=registry)

        snapshot = await supervisor.managers["system"].snapshot()
        memory = snapshot.get("memory", {})
        cpu_modes = snapshot.get("cpu_usage_ratio_by_mode", {})
        for cpu_name, modes in cpu_modes.items():
            for mode, value in modes.items():
                cpu.labels(str(mode), str(cpu_name)).set(value)
        if (temperature := snapshot.get("cpu_temperature_celsius")) is not None:
            cpu_temperature = Gauge("homelab_cpu_temperature_celsius", "CPU temperature in Celsius", registry=registry)
            cpu_temperature.set(temperature)
        if (frequency := snapshot.get("cpu_frequency_hertz")) is not None:
            cpu_frequency = Gauge("homelab_cpu_frequency_hertz", "Current CPU frequency in Hertz", registry=registry)
            cpu_frequency.set(frequency)
        memory_used.set(memory.get("used_bytes", 0))
        memory_available.set(memory.get("available_bytes", 0))
        memory_total.set(memory.get("total_bytes", 0))
        memory_cached.set(memory.get("cached_bytes", 0))
        swap = snapshot.get("swap", {})
        swap_used.set(swap.get("used_bytes", 0))
        swap_total.set(swap.get("total_bytes", 0))
        for window, value in zip(("1m", "5m", "15m"), snapshot.get("load_average", [])):
            load_average.labels(window).set(value)
        for filesystem in snapshot.get("storage", []):
            mountpoint = filesystem.get("mountpoint")
            if not isinstance(mountpoint, str):
                continue
            storage_used.labels(mountpoint).set(filesystem.get("used_bytes", 0))
            storage_free.labels(mountpoint).set(filesystem.get("free_bytes", 0))
        for device, counters in snapshot.get("disk_io", {}).items():
            disk_read.labels(device).set(counters.get("read_bytes", 0))
            disk_write.labels(device).set(counters.get("write_bytes", 0))
        for interface, counters in snapshot.get("network_io", {}).items():
            network_receive.labels(interface).set(counters.get("receive_bytes", 0))
            network_transmit.labels(interface).set(counters.get("transmit_bytes", 0))
        rgb_snapshot = await supervisor.managers["rgb"].snapshot()
        desired_rgb = rgb_snapshot.get("desired", {})
        rgb_enabled.set(1 if desired_rgb.get("enabled") else 0)
        rgb_brightness.set(desired_rgb.get("brightness", 0))
        fan_snapshot = await supervisor.managers["fans"].snapshot()
        fan_state.labels("external").set(1 if fan_snapshot.get("actual", {}).get("external", {}).get("output_enabled") else 0)
        for name, status in (await supervisor.health()).items():
            manager_up.labels(name).set(1 if status["status"] == "ready" else 0)
            manager_errors.labels(name).set(status.get("error_count", 0))
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app
