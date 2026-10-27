import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from carbon.config.settings import Settings
from carbon.core.logging import TokenSanitizingFilter
from carbon.main import create_app


@pytest.fixture
def test_app(tmp_path: Path):
    """Create test application instance with dedicated temporary token directory."""
    token_file = tmp_path / ".carbon_token"
    token_file.write_text("test-secret-token-12345", encoding="utf-8")
    os.chmod(token_file, 0o600)

    settings = Settings(
        api_token="test-secret-token-12345",
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        audit_log_path=tmp_path / "audit.log",
    )

    app = create_app()
    app.state.api_token = "test-secret-token-12345"
    return app, settings


def test_rest_unauthorized_access_rejected(test_app):
    """Verify endpoints reject requests lacking valid authorization."""
    app, _ = test_app
    client = TestClient(app)

    # 1. No Authorization header -> 401 Unauthorized
    res = client.get("/api/v1/models")
    assert res.status_code == 401
    assert "Invalid or missing" in res.json()["detail"]

    # 2. Invalid token -> 401 Unauthorized
    res2 = client.get("/api/v1/models", headers={"Authorization": "Bearer wrong-token"})
    assert res2.status_code == 401


def test_rest_authorized_access_succeeds(test_app):
    """Verify endpoints accept requests with valid bearer token."""
    app, _ = test_app
    client = TestClient(app)

    headers = {"Authorization": "Bearer test-secret-token-12345"}
    res = client.get("/api/v1/models", headers=headers)
    assert res.status_code == 200
    assert "registered_models" in res.json()


def test_origin_validation_and_cors(test_app):
    """Verify strict Origin validation rejecting unauthorized origins and null origins."""
    app, _ = test_app
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-secret-token-12345"}

    # 1. Allowed origin (loopback) -> 200 OK
    res_allowed = client.get(
        "/api/v1/models",
        headers={**headers, "Origin": "http://127.0.0.1:8765"},
    )
    assert res_allowed.status_code == 200

    # 2. External untrusted origin -> 403 Forbidden
    res_forbidden = client.get(
        "/api/v1/models",
        headers={**headers, "Origin": "https://malicious-site.com"},
    )
    assert res_forbidden.status_code == 403
    assert "Origin not allowed" in res_forbidden.json()["detail"]

    # 3. Origin: null (sandboxed iframe / browser CSRF attempt) -> 403 Forbidden
    res_null = client.get(
        "/api/v1/models",
        headers={**headers, "Origin": "null"},
    )
    assert res_null.status_code == 403
    assert "Origin not allowed" in res_null.json()["detail"]


def test_token_rotation_and_file_permissions(test_app):
    """Verify token rotation endpoint updates token and persists file with 0600 permissions."""
    app, settings = test_app
    client = TestClient(app)

    old_token = "test-secret-token-12345"
    rotate_res = client.post(
        "/api/v1/auth/rotate-token",
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert rotate_res.status_code == 200
    assert rotate_res.json()["status"] == "rotated"

    new_token = app.state.api_token
    assert new_token != old_token

    # Verify old token is now rejected
    res_old = client.get("/api/v1/models", headers={"Authorization": f"Bearer {old_token}"})
    assert res_old.status_code == 401

    # Verify new token succeeds
    res_new = client.get("/api/v1/models", headers={"Authorization": f"Bearer {new_token}"})
    assert res_new.status_code == 200

    # Verify file permissions on disk are 0600
    token_file = settings.data_dir / ".carbon_token"
    assert token_file.exists()
    file_mode = oct(token_file.stat().st_mode & 0o777)
    assert file_mode == "0o600"


def test_websocket_query_string_token_rejected(test_app):
    """Verify that passing tokens in the query string is rejected and does not authenticate."""
    app, _ = test_app
    client = TestClient(app)

    # Attempting to connect with query parameter token
    # Since query parameter auth was removed, the socket accepts only if followed by auth frame or header.
    # If the client immediately sends a submit action thinking it is authenticated, it fails!
    with client.websocket_connect("/ws/chat?token=test-secret-token-12345") as ws:
        # Prompt command sent without explicit in-socket auth frame
        ws.send_json({"action": "submit", "prompt": "Hello"})
        msg = ws.receive_json()
        # Must fail because query-string authentication is eliminated
        assert msg.get("event") == "error"
        assert "In-socket auth frame required" in msg.get("message", "")


def test_websocket_handshake_header_auth(test_app):
    """Verify WebSocket handshake succeeds with standard Authorization Bearer header."""
    app, _ = test_app
    client = TestClient(app)

    with client.websocket_connect(
        "/ws/chat",
        headers={"Authorization": "Bearer test-secret-token-12345"},
    ) as ws:
        # Client is already authenticated via header, so submit action works immediately!
        ws.send_json({"action": "cancel", "task_id": "nonexistent"})
        msg = ws.receive_json()
        assert msg.get("event") == "cancelled"


def test_websocket_origin_and_auth_rejection(test_app):
    """Verify WebSocket rejects connections with untrusted origin or invalid token."""
    app, _ = test_app
    client = TestClient(app)

    # 1. WebSocket with unauthorized origin -> rejected with 1008 policy violation
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/chat",
            headers={"Origin": "https://evil.com"},
        ):
            pass

    # 2. WebSocket with invalid auth frame closes connection
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "auth", "token": "invalid-token"})
        msg = ws.receive_json()
        assert msg.get("event") == "error"


def test_token_sanitization_in_logging():
    """Verify TokenSanitizingFilter masks bearer tokens and credentials in logs."""
    log_filter = TokenSanitizingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Authenticated user with Bearer mysecrettoken1234567890abc",
        args=(),
        exc_info=None,
    )
    log_filter.filter(record)
    assert "mysecrettoken1234567890abc" not in record.msg
    assert "Bearer [REDACTED_TOKEN]" in record.msg
