import pytest
from httpx import ASGITransport, AsyncClient

from homelab_core.api.server import create_app
from homelab_core.config import AppConfig
from homelab_core.runtime.supervisor import Supervisor


@pytest.mark.asyncio
async def test_read_only_api():
    supervisor = Supervisor(AppConfig())
    await supervisor.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app(supervisor)), base_url="http://test") as client:
            response = await client.get("/api/v1/health")
            assert response.status_code == 200
            assert response.json()["managers"]["system"]["status"] == "ready"
            assert response.json()["managers"]["rgb"]["status"] == "degraded"
            assert (await client.get("/api/v1/overview")).status_code == 200
            rgb = await client.get("/api/v1/rgb")
            assert rgb.status_code == 200
            update = await client.post("/api/v1/rgb", json={"enabled": False, "color": "#112233"})
            assert update.status_code == 503
            assert (await client.post("/api/v1/rgb", json={"color": "blue"})).status_code == 422
            assert "homelab_cpu_usage_ratio" in (await client.get("/metrics")).text
    finally:
        await supervisor.stop()
