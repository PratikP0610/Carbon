"""Comprehensive unit and pipeline tests for Carbon's memory subsystem.

Verifies:
1. Immediate store and retrieve across SQLite tiers.
2. Store in turn 1, retrieve in turn 2 within same session.
3. Persistence across backend/core reinitialization (simulated restart).
4. Multiple independent preferences without cross-contamination.
5. Transactional storage: no false acknowledgements on persistence failure.
6. Honest negative response for queries regarding missing memories.
7. Conversation history isolation between distinct conversation IDs.
8. Dual spelling normalization (UK 'favourite' vs US 'favorite').
9. Safe in-place updates avoiding FTS5 rowid desync corruption.
"""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import (
    MemoryRecord,
    MemoryType,
    TaskRequest,
    TaskState,
)
from carbon.memory.sqlite_memory import SQLiteMemoryEngine


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """Provide isolated SQLite database settings."""
    db_file = tmp_path / "test_carbon.db"
    audit_file = tmp_path / "test_audit.log"
    return Settings(
        data_dir=tmp_path,
        database_path=db_file,
        audit_log_path=audit_file,
        enable_audit_log=True,
    )


@pytest.fixture
async def memory_engine(test_settings: Settings) -> AsyncGenerator[SQLiteMemoryEngine, None]:
    """Isolated initialized SQLite memory engine."""
    engine = SQLiteMemoryEngine(db_path=test_settings.database_path)
    await engine.initialize()
    yield engine
    await engine.close()


@pytest.fixture
async def orchestrator_core(test_settings: Settings) -> AsyncGenerator[CarbonCore, None]:
    """Isolated initialized CarbonCore orchestrator with mocked LLM provider."""
    core = CarbonCore(settings=test_settings)
    await core.initialize()
    yield core
    await core.shutdown()


@pytest.mark.asyncio
async def test_immediate_store_and_retrieve_semantic_preference(memory_engine: SQLiteMemoryEngine):
    """Test 1: Immediate store and retrieve of user preference."""
    rec = MemoryRecord(
        id="mem-pref-1",
        memory_type=MemoryType.SEMANTIC,
        key="user:preference:favorite_programming_language",
        content="User preference: my favourite programming language is Java. (favorite Java)",
        metadata={"category": "programming"},
        source="user_explicit",
    )
    rec_id = await memory_engine.store(rec)
    assert rec_id == "mem-pref-1"

    # Retrieve by direct key
    retrieved = await memory_engine.retrieve("user:preference:favorite_programming_language")
    assert retrieved is not None
    assert "Java" in retrieved.content
    assert retrieved.memory_type == MemoryType.SEMANTIC

    # Retrieve by FTS query with question phrasing
    search_results = await memory_engine.search(
        "What is my favourite programming language?",
        memory_type=MemoryType.SEMANTIC,
    )
    assert len(search_results) >= 1
    assert any("Java" in r.content for r in search_results)


@pytest.mark.asyncio
async def test_store_turn_1_and_retrieve_turn_2(orchestrator_core: CarbonCore):
    """Test 2: Store in turn 1, retrieve in turn 2 within the same session."""
    captured_llm_calls: list[dict[str, Any]] = []

    async def mock_generate_stream_events(model_id, prompt, system=None, **_kwargs):
        captured_llm_calls.append({"model_id": model_id, "prompt": prompt, "system": system})
        yield {"response": "Your favourite programming language is Java."}

    with patch.object(orchestrator_core.provider, "generate_stream_events", side_effect=mock_generate_stream_events):
        conv_id = "conv-session-test-01"

        # Turn 1: Store preference
        req1 = TaskRequest(
            prompt="Remember this: my favourite programming language is Java.",
            conversation_id=conv_id,
        )
        task1 = await orchestrator_core.create_task(req1)
        tokens1: list[str] = []
        async for event in orchestrator_core.execute_task_stream(task1):
            if event.get("event") == "token":
                tokens1.append(event["text"])

        response1 = "".join(tokens1)
        assert task1.state == TaskState.COMPLETED
        assert "Understood. I have remembered: my favourite programming language is Java." in response1
        # Model should not even need to be called for storage
        assert len(captured_llm_calls) == 0

        # Turn 2: Query preference
        req2 = TaskRequest(
            prompt="What is my favourite programming language?",
            conversation_id=conv_id,
        )
        task2 = await orchestrator_core.create_task(req2)
        tokens2: list[str] = []
        async for event in orchestrator_core.execute_task_stream(task2):
            if event.get("event") == "token":
                tokens2.append(event["text"])

        response2 = "".join(tokens2)
        assert task2.state == TaskState.COMPLETED
        assert "Java" in response2

        # Verify context injection into system prompt
        assert len(captured_llm_calls) == 1
        system_prompt = captured_llm_calls[0]["system"]
        assert system_prompt is not None
        assert "Relevant information from user's saved persistent memory:" in system_prompt
        assert "User preference: my favourite programming language is Java." in system_prompt


@pytest.mark.asyncio
async def test_persistence_across_backend_restart(test_settings: Settings):
    """Test 3: Restart/reinitialize memory engine and retrieve preference."""
    # First instance (Session 1)
    core1 = CarbonCore(settings=test_settings)
    await core1.initialize()

    req = TaskRequest(prompt="Remember this: my favourite programming language is Java.")
    task1 = await core1.create_task(req)
    async for _ in core1.execute_task_stream(task1):
        pass
    assert task1.state == TaskState.COMPLETED
    await core1.shutdown()

    # Second instance simulating backend restart (Session 2)
    core2 = CarbonCore(settings=test_settings)
    await core2.initialize()

    captured_llm_calls: list[dict[str, Any]] = []

    async def mock_generate_stream_events(model_id, prompt, system=None, **_kwargs):
        captured_llm_calls.append({"model_id": model_id, "prompt": prompt, "system": system})
        yield {"response": "Your favourite programming language is Java."}

    with patch.object(core2.provider, "generate_stream_events", side_effect=mock_generate_stream_events):
        req2 = TaskRequest(prompt="What is my favourite programming language?")
        task2 = await core2.create_task(req2)
        async for _ in core2.execute_task_stream(task2):
            pass

        assert task2.state == TaskState.COMPLETED
        assert len(captured_llm_calls) == 1
        assert "Java" in captured_llm_calls[0]["system"]

    await core2.shutdown()


@pytest.mark.asyncio
async def test_multiple_independent_preferences(orchestrator_core: CarbonCore):
    """Test 4: Store and retrieve multiple distinct preferences without cross-contamination."""
    preferences = [
        ("Remember this: my favourite programming language is Java.", "What is my favourite programming language?", "Java"),
        ("Remember this: my preferred editor is Neovim.", "What is my preferred editor?", "Neovim"),
        ("Remember this: my favourite theme is dark mode.", "What is my favourite theme?", "dark mode"),
    ]

    # Store all three preferences
    for store_prompt, _, _ in preferences:
        req = TaskRequest(prompt=store_prompt)
        task = await orchestrator_core.create_task(req)
        async for _ in orchestrator_core.execute_task_stream(task):
            pass
        assert task.state == TaskState.COMPLETED

    # Query each preference independently
    for _, query_prompt, expected_value in preferences:
        captured_calls: list[dict[str, Any]] = []

        def make_mock(expected_val: str, sink: list[dict[str, Any]]):
            async def _mock(model_id=None, prompt=None, system=None, **_kwargs):
                sink.append({"system": system, "model_id": model_id, "prompt": prompt})
                yield {"response": f"Your preference is {expected_val}."}
            return _mock

        with patch.object(orchestrator_core.provider, "generate_stream_events", side_effect=make_mock(expected_value, captured_calls)):
            req = TaskRequest(prompt=query_prompt)
            task = await orchestrator_core.create_task(req)
            async for _ in orchestrator_core.execute_task_stream(task):
                pass
            assert task.state == TaskState.COMPLETED
            assert len(captured_calls) == 1
            # Injected system context must contain the specific expected value
            assert expected_value in captured_calls[0]["system"]


@pytest.mark.asyncio
async def test_persistence_failure_emits_no_false_acknowledgement(orchestrator_core: CarbonCore):
    """Test 5: Simulated persistence failure produces honest error and NO false confirmation."""
    with patch.object(
        orchestrator_core.memory, "store", side_effect=RuntimeError("Simulated disk I/O failure")
    ):
        req = TaskRequest(prompt="Remember this: my favourite database is PostgreSQL.")
        task = await orchestrator_core.create_task(req)
        emitted_events: list[dict[str, Any]] = []

        async for event in orchestrator_core.execute_task_stream(task):
            emitted_events.append(event)

        assert task.state == TaskState.FAILED
        assert "Database persistence failure" in (task.error or "")

        # Verify no token event saying "Understood" or "remembered" was emitted
        token_texts = [e["text"] for e in emitted_events if e.get("event") == "token"]
        assert len(token_texts) == 0

        # Verify error event was emitted
        error_events = [e for e in emitted_events if e.get("event") in ("error", "failed")]
        assert len(error_events) >= 1


@pytest.mark.asyncio
async def test_missing_memory_query_instructs_honest_negative(orchestrator_core: CarbonCore):
    """Test 6: Missing memory queries explicitly instruct model not to fabricate data."""
    captured_calls: list[dict[str, Any]] = []

    async def mock_gen(model_id=None, prompt=None, system=None, **_kwargs):
        captured_calls.append({"system": system, "model_id": model_id, "prompt": prompt})
        yield {"response": "I do not have any record of your favourite dessert in my memory."}

    with patch.object(orchestrator_core.provider, "generate_stream_events", side_effect=mock_gen):
        req = TaskRequest(prompt="What is my favourite dessert?")
        task = await orchestrator_core.create_task(req)
        async for _ in orchestrator_core.execute_task_stream(task):
            pass

        assert task.state == TaskState.COMPLETED
        assert len(captured_calls) == 1
        system_prompt = captured_calls[0]["system"]
        assert "No matching information was found in the user's saved memory." in system_prompt
        assert "Do not guess or fabricate personal information." in system_prompt


@pytest.mark.asyncio
async def test_conversation_history_isolation(orchestrator_core: CarbonCore):
    """Test 7: Unrelated conversation history is isolated across distinct conversation IDs."""
    async def dummy_gen(model_id=None, prompt=None, **_kwargs):
        yield {"response": f"Answer for {model_id} on {prompt}."}

    with patch.object(orchestrator_core.provider, "generate_stream_events", side_effect=dummy_gen):
        # Conversation A turn
        task_a = await orchestrator_core.create_task(
            TaskRequest(prompt="Hello from session A", conversation_id="conv-AAA")
        )
        async for _ in orchestrator_core.execute_task_stream(task_a):
            pass

        # Conversation B turn
        task_b = await orchestrator_core.create_task(
            TaskRequest(prompt="Hello from session B", conversation_id="conv-BBB")
        )
        async for _ in orchestrator_core.execute_task_stream(task_b):
            pass

    # Verify conversation history turns
    turns_a = await orchestrator_core.memory.get_recent_conversation_turns("conv-AAA")
    turns_b = await orchestrator_core.memory.get_recent_conversation_turns("conv-BBB")

    assert len(turns_a) >= 1
    assert all("conv-AAA" in str(t.metadata.get("conversation_id")) for t in turns_a)
    assert any("session A" in t.content for t in turns_a)
    assert not any("session B" in t.content for t in turns_a)

    assert len(turns_b) >= 1
    assert all("conv-BBB" in str(t.metadata.get("conversation_id")) for t in turns_b)
    assert any("session B" in t.content for t in turns_b)
    assert not any("session A" in t.content for t in turns_b)


@pytest.mark.asyncio
async def test_spelling_normalization_uk_vs_us(memory_engine: SQLiteMemoryEngine):
    """Test 8: Store with UK 'favourite', search with US 'favorite' and vice versa."""
    rec = MemoryRecord(
        id="mem-spell-1",
        memory_type=MemoryType.SEMANTIC,
        key="user:preference:favorite_fruit",
        content="User preference: my favourite fruit is mango. (favorite mango)",
        metadata={},
        source="user_explicit",
    )
    await memory_engine.store(rec)

    # Search using US spelling "favorite"
    results_us = await memory_engine.search("What is my favorite fruit?")
    assert len(results_us) >= 1
    assert "mango" in results_us[0].content

    # Search using UK spelling "favourite"
    results_uk = await memory_engine.search("What is my favourite fruit?")
    assert len(results_uk) >= 1
    assert "mango" in results_uk[0].content


@pytest.mark.asyncio
async def test_in_place_semantic_update_prevents_rowid_corruption(memory_engine: SQLiteMemoryEngine):
    """Test 9: Repeated updates to same key update in place and do not cause FTS5 rowid desync."""
    # First save: Java
    rec1 = MemoryRecord(
        id="pref-prog-lang",
        memory_type=MemoryType.SEMANTIC,
        key="user:preference:favorite_programming_language",
        content="User preference: my favourite programming language is Java. (favorite Java)",
        metadata={},
        source="user_explicit",
    )
    await memory_engine.store(rec1)

    # Second save: update to Python (under new record ID or same ID)
    rec2 = MemoryRecord(
        id="pref-prog-lang-new-id",
        memory_type=MemoryType.SEMANTIC,
        key="user:preference:favorite_programming_language",
        content="User preference: my favourite programming language is Python. (favorite Python)",
        metadata={},
        source="user_explicit",
    )
    returned_id = await memory_engine.store(rec2)
    # Reuses existing record ID to preserve rowid
    assert returned_id == "pref-prog-lang"

    # Search should return Python, not Java
    results = await memory_engine.search("What is my favourite programming language?")
    assert len(results) == 1
    assert "Python" in results[0].content
    assert "Java" not in results[0].content

    # Verify directly via retrieve
    direct = await memory_engine.retrieve("user:preference:favorite_programming_language")
    assert direct is not None
    assert "Python" in direct.content
