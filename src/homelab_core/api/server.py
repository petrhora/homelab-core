from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

from ..runtime.supervisor import Supervisor


class RgbUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    color: str | None = None
    brightness: float | None = Field(default=None, ge=0, le=1)
    led_count: int | None = Field(default=None, ge=1, le=1024)
    clear: bool = False

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 7 or not value.startswith("#")):
            raise ValueError("color must be in #RRGGBB format")
        if value is not None:
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
