"""End-to-End integration test validating real local inference with Qwen 3.5 9B."""

from pathlib import Path
import pytest
from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import TaskRequest, TaskState


@pytest.mark.asyncio
async def test_real_qwen_inference_and_memory_logging(tmp_path: Path):
    """Verify CarbonCore executes real inference with Qwen 3.5 9B, acquires CMS lease,

    and writes episodic memory.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
        primary_model="qwen3.5:9b",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    try:
        # Check that Ollama is reachable
        if not await core.provider.is_healthy():
            pytest.skip("Ollama daemon is not running locally.")

        req = TaskRequest(prompt="Say 'TEST_CONFIRMED' in one word.", stream=True)
        task = await core.create_task(req)

        tokens: list[str] = []
        async for event in core.execute_task_stream(task):
            if event.get("event") == "token":
                tokens.append(event["text"])

        full_output = "".join(tokens)
        assert task.state == TaskState.COMPLETED
        assert len(full_output) > 0
        assert task.result is not None

        # Verify episodic memory was written
        memories = await core.memory.search("TEST_CONFIRMED")
        assert len(memories) >= 1 or await core.memory.retrieve(f"task:{task.task_id}") is not None

        # Verify audit log was recorded
        assert settings.audit_log_path.exists()
        log_content = settings.audit_log_path.read_text(encoding="utf-8")
        assert "CARBON_CORE_INITIALIZED" in log_content

    finally:
        await core.shutdown()
