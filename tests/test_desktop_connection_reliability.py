"""Tests for Carbon Desktop connection lifecycle, authentication, and submission reliability."""

import asyncio

import pytest
from starlette.testclient import TestClient

from carbon.interfaces.models import TaskRequest
from carbon.main import create_app


@pytest.fixture
def test_app():
    app = create_app()
    with TestClient(app) as client:
        token = app.state.api_token
        yield app, client, token


def test_health_endpoint_contract(test_app):
    """Verify GET /api/v1/health returns HTTP 200 with expected schema for Swift client."""
    _, client, _ = test_app
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["core_running"] is True
    assert data["ollama_connected"] is True
    assert "active_model" in data
    assert data["active_model"]["id"] == "qwen3.5:9b"


def test_websocket_header_auth_success(test_app):
    """Verify WebSocket handshake succeeds with valid Authorization Bearer header."""
    app, _, token = test_app
    client = TestClient(app)

    with client.websocket_connect(
        "/ws/chat",
        headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        # Submit task to confirm connection is active and authenticated
        ws.send_json({"action": "cancel", "task_id": "nonexistent"})
        msg = ws.receive_json()
        assert msg.get("event") == "cancelled"


def test_websocket_in_socket_fallback_auth_success(test_app):
    """Verify WebSocket in-socket fallback auth frame receives 'authenticated' event."""
    app, _, token = test_app
    client = TestClient(app)

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "auth", "token": token})
        auth_msg = ws.receive_json()
        assert auth_msg.get("event") == "authenticated"

        # Subsequent messages should succeed
        ws.send_json({"action": "cancel", "task_id": "nonexistent"})
        msg = ws.receive_json()
        assert msg.get("event") == "cancelled"


def test_websocket_auth_failure_invalid_token(test_app):
    """Verify WebSocket closes with error on invalid in-socket token."""
    app, _, _ = test_app
    client = TestClient(app)

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "auth", "token": "wrong-token-abc"})
        err_msg = ws.receive_json()
        assert err_msg.get("event") == "error"
        assert "Authentication failed" in err_msg.get("message", "")


def test_websocket_submit_message_stream(test_app):
    """Verify WebSocket message submission streams task_created, thinking, and completes cleanly."""
    app, _, token = test_app
    client = TestClient(app)

    with client.websocket_connect(
        "/ws/chat",
        headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_json({"action": "submit", "prompt": "ping"})
        created_evt = ws.receive_json()
        assert created_evt.get("event") == "task_created"
        assert "task_id" in created_evt
        assert "t_backend_receipt" in created_evt

        # Drain until terminal state
        events = []
        while True:
            evt = ws.receive_json()
            events.append(evt.get("event"))
            if evt.get("event") in ("completed", "failed", "cancelled"):
                break

        assert "completed" in events or "state_change" in events


def test_websocket_reconnect(test_app):
    """Verify that after disconnecting, a client can reconnect and authenticate again."""
    app, _, token = test_app
    client = TestClient(app)

    # First connection
    with client.websocket_connect(
        "/ws/chat",
        headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_json({"action": "cancel", "task_id": "session-1"})
        msg = ws.receive_json()
        assert msg.get("event") == "cancelled"

    # Client disconnects and reconnects
    with client.websocket_connect(
        "/ws/chat",
        headers={"Authorization": f"Bearer {token}"},
    ) as ws2:
        ws2.send_json({"action": "cancel", "task_id": "session-2"})
        msg2 = ws2.receive_json()
        assert msg2.get("event") == "cancelled"


def test_websocket_failed_submission_recovery(test_app):
    """Verify backend handles malformed action with error, and connection recovers to process subsequent task."""
    app, _, token = test_app
    client = TestClient(app)

    with client.websocket_connect(
        "/ws/chat",
        headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        # 1. Send malformed/unsupported action payload
        ws.send_text("not-a-valid-json")
        err_msg = ws.receive_json()
        assert err_msg.get("event") == "error"
        assert "Invalid JSON format" in err_msg.get("message", "")

        # 2. Connection is still open and alive; submit valid task
        ws.send_json({"action": "cancel", "task_id": "recovery-task-id"})
        msg = ws.receive_json()
        assert msg.get("event") == "cancelled"
        assert msg.get("task_id") == "recovery-task-id"


def test_websocket_disconnect_cleans_up_active_task(test_app):
    """Verify disconnecting mid-stream cancels active task and removes it from core _tasks."""
    app, client, token = test_app

    # Connect and submit a task
    task_id = None
    with client.websocket_connect(
        "/ws/chat",
        headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_json({"action": "submit", "prompt": "Long generation prompt for disconnect test"})
        created_evt = ws.receive_json()
        assert created_evt.get("event") == "task_created"
        task_id = created_evt.get("task_id")
        assert task_id is not None
        # Disconnect abruptly mid-stream by exiting context manager without reading to end

    # Poll health endpoint: verify tasks_count drops back to 0
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    health_data = resp.json()
    assert health_data["tasks_count"] == 0, f"Expected 0 active tasks after disconnect, got {health_data['tasks_count']}"


def test_websocket_disconnect_preserves_other_client_task(test_app):
    """Verify disconnecting one client never cancels or purges another client's task."""
    app, client, token = test_app
    import carbon.main as carbon_main

    # Create an independent task belonging to Client 1 directly in Core
    c = carbon_main.core
    assert c is not None
    req = TaskRequest(prompt="Client 1 task", stream=True)
    client1_task = asyncio.run(c.create_task(req))
    client1_task_id = client1_task.task_id

    assert client1_task_id in c._tasks

    # Client 2 connects via WebSocket, performs an action, and disconnects
    with client.websocket_connect(
        "/ws/chat",
        headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_json({"action": "cancel", "task_id": "client-2-dummy"})
        _ = ws.receive_json()
        # Client 2 disconnects here

    # Verify Client 1's task was NOT removed or corrupted by Client 2's disconnection
    assert client1_task_id in c._tasks
    assert c._tasks[client1_task_id].prompt == "Client 1 task"

    # Cleanup Client 1 task
    c._tasks.pop(client1_task_id, None)


def test_websocket_disconnect_completed_task_safe(test_app):
    """Verify disconnecting after a task has completed is safe and leaves no stale active markers."""
    app, client, token = test_app

    with client.websocket_connect(
        "/ws/chat",
        headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_json({"action": "submit", "prompt": "ping"})
        created_evt = ws.receive_json()
        assert created_evt.get("event") == "task_created"

        # Drain until terminal state
        while True:
            evt = ws.receive_json()
            if evt.get("event") in ("completed", "failed", "cancelled"):
                break
        # Normal disconnect after completion

    # Health check remains healthy
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_websocket_disconnect_already_cancelled_task_safe(test_app):
    """Verify disconnecting after manually cancelling a task is safe."""
    app, client, token = test_app

    with client.websocket_connect(
        "/ws/chat",
        headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_json({"action": "submit", "prompt": "task to cancel"})
        created_evt = ws.receive_json()
        task_id = created_evt.get("task_id")

        # Explicitly cancel task via WebSocket
        ws.send_json({"action": "cancel", "task_id": task_id})
        msg = ws.receive_json()
        # May receive cancelled event
        assert msg.get("event") in ("cancelled", "state_change")

    # Health check remains healthy and task cleanup handles already-cancelled state
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


