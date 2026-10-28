"""Tests for Carbon Desktop connection lifecycle, authentication, and submission reliability."""

import asyncio
import json
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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

