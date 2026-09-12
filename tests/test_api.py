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
            assert (await client.get("/api/v1/overview")).status_code == 200
            assert "homelab_cpu_usage_ratio" in (await client.get("/metrics")).text
    finally:
        await supervisor.stop()
