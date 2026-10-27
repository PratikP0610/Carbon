"""Integration tests for Carbon FastAPI loopback API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from carbon.main import app


@pytest.mark.asyncio
async def test_root_endpoint():
    """Verify root status endpoint."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        res = await client.get("/")
        assert res.status_code == 200
        data = res.json()
        assert data["name"] == "Carbon"
        assert data["version"] == "1.0.0"
        assert data["loopback_only"] is True


@pytest.mark.asyncio
async def test_health_endpoint():
    """Verify detailed health check endpoint."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        res = await client.get("/api/v1/health")
        assert res.status_code == 200
        data = res.json()
        assert "status" in data


@pytest.mark.asyncio
async def test_task_submission_and_query():
    """Verify submitting a task and checking its state."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        # Submit a non-streaming task
        payload = {"prompt": "Quick status test", "stream": False}
        res = await client.post("/api/v1/tasks", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert "task_id" in data
        task_id = data["task_id"]

        # Query status
        status_res = await client.get(f"/api/v1/tasks/{task_id}")
        assert status_res.status_code == 200
        task_data = status_res.json()
        assert task_data["task_id"] == task_id
        assert task_data["prompt"] == "Quick status test"
