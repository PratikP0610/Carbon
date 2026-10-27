"""Unit tests for Carbon Core Orchestrator."""

from pathlib import Path

import pytest

from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import TaskRequest, TaskState


@pytest.fixture
async def core(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    c = CarbonCore(settings=settings)
    await c.initialize()
    yield c
    await c.shutdown()


@pytest.mark.asyncio
async def test_task_creation_and_cancellation(core: CarbonCore):
    """Test task lifecycle management and cancellation."""
    req = TaskRequest(prompt="Hello Carbon, how are you?")
    task = await core.create_task(req)

    assert task.task_id.startswith("task-")
    assert task.state == TaskState.PENDING
    assert core.get_task(task.task_id) is not None

    # Cancel task
    cancelled = core.cancel_task(task.task_id)
    assert cancelled is True
    assert task.state == TaskState.CANCELLED
    assert task.cancel_event.is_set()


@pytest.mark.asyncio
async def test_system_health_telemetry(core: CarbonCore):
    """Test health telemetry aggregation."""
    health = await core.get_system_health()
    assert health["core_running"] is True
    assert "host" in health
    assert health["host"]["unified_memory_gb"] > 0
    assert "autonomy_level" in health
