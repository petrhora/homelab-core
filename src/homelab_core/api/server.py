from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

from ..runtime.supervisor import Supervisor


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
    pixels: list[str | RgbPixelUpdate] | None = None

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


class RainbowPatternUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["rainbow"] = "rainbow"
    speed: float = Field(default=0.1, ge=0)
    direction: Literal["forward", "reverse"] = "forward"
    saturation: float = Field(default=1, ge=0, le=1)
    brightness: float = Field(default=1, ge=0, le=1)


class HueCyclePatternUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hue_cycle"] = "hue_cycle"
    speed: float = Field(default=0.1, ge=0)
    saturation: float = Field(default=1, ge=0, le=1)
    brightness: float = Field(default=1, ge=0, le=1)


PatternUpdate = Annotated[
    SolidPatternUpdate | BreathingPatternUpdate | FlowPatternUpdate | RainbowPatternUpdate | HueCyclePatternUpdate,
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
    async def processes() -> list[dict]:
        return await supervisor.managers["system"].processes()  # type: ignore[attr-defined]

    @app.get("/api/v1/rgb")
    async def rgb() -> dict:
        return await supervisor.managers["rgb"].snapshot()

    @app.post("/api/v1/rgb")
    async def update_rgb(update: RgbUpdate) -> dict:
        try:
            return await supervisor.commands.dispatch("rgb.update", update.model_dump(exclude_none=True))
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
        for name, status in (await supervisor.health()).items():
            manager_up.labels(name).set(1 if status["status"] == "ready" else 0)
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app
