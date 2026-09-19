from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
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
    registry = CollectorRegistry()
    cpu = Gauge("homelab_cpu_usage_ratio", "Overall CPU usage ratio", registry=registry)
    memory_used = Gauge("homelab_memory_used_bytes", "Used memory", registry=registry)
    memory_available = Gauge("homelab_memory_available_bytes", "Available memory", registry=registry)
    rgb_enabled = Gauge("homelab_rgb_enabled", "Whether RGB output is enabled", registry=registry)
    rgb_brightness = Gauge("homelab_rgb_brightness", "RGB brightness ratio", registry=registry)
    fan_state = Gauge("homelab_fan_state", "Whether an external fan output is enabled", ["fan"], registry=registry)
    manager_up = Gauge("homelab_manager_up", "Whether a manager is ready", ["manager"], registry=registry)

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

    @app.post("/api/v1/rgb")
    async def update_rgb(update: RgbUpdate) -> dict:
        try:
            return await supervisor.commands.dispatch("rgb.update", update.model_dump(exclude_none=True))
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
            return await supervisor.commands.dispatch("lighting.update", update.model_dump(exclude_unset=True))
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
            return await supervisor.commands.dispatch("fans.update", update.model_dump(exclude_none=True))
        except CommandTimeoutError as error:
            raise HTTPException(status_code=504, detail=str(error)) from error
        except (ValueError, TimeoutError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        if not metrics_enabled:
            return Response(status_code=404)
        snapshot = await supervisor.managers["system"].snapshot()
        memory = snapshot.get("memory", {})
        cpu.set(snapshot.get("cpu_usage_ratio", 0))
        memory_used.set(memory.get("used_bytes", 0))
        memory_available.set(memory.get("available_bytes", 0))
        rgb_snapshot = await supervisor.managers["rgb"].snapshot()
        desired_rgb = rgb_snapshot.get("desired", {})
        rgb_enabled.set(1 if desired_rgb.get("enabled") else 0)
        rgb_brightness.set(desired_rgb.get("brightness", 0))
        fan_snapshot = await supervisor.managers["fans"].snapshot()
        fan_state.labels("external").set(1 if fan_snapshot.get("actual", {}).get("external", {}).get("output_enabled") else 0)
        for name, status in (await supervisor.health()).items():
            manager_up.labels(name).set(1 if status["status"] == "ready" else 0)
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app
