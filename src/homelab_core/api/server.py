from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

from ..runtime.supervisor import Supervisor


def create_app(supervisor: Supervisor, metrics_enabled: bool = True) -> FastAPI:
    app = FastAPI(title="homelab-core", version="0.1.0")
    registry = CollectorRegistry()
    cpu = Gauge("homelab_cpu_usage_ratio", "Overall CPU usage ratio", registry=registry)
    memory_used = Gauge("homelab_memory_used_bytes", "Used memory", registry=registry)
    memory_available = Gauge("homelab_memory_available_bytes", "Available memory", registry=registry)
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

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        if not metrics_enabled:
            return Response(status_code=404)
        snapshot = await supervisor.managers["system"].snapshot()
        memory = snapshot.get("memory", {})
        cpu.set(snapshot.get("cpu_usage_ratio", 0))
        memory_used.set(memory.get("used_bytes", 0))
        memory_available.set(memory.get("available_bytes", 0))
        for name, status in (await supervisor.health()).items():
            manager_up.labels(name).set(1 if status["status"] == "ready" else 0)
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app
