"""Security and API integration tests for Batch 3 authenticated memory & task endpoints."""

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import carbon.main as main_module
from carbon.config.settings import get_settings
from carbon.governance.governance_engine import DecisionSource, verify_audit_log_chain
from carbon.interfaces.models import MemoryRecord, MemoryType, PersistedTaskRecord, TaskState


@pytest.fixture
def isolated_api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide a FastAPI TestClient wired strictly to isolated temporary DB and audit logs."""
    token = "batch3-secret-token-987654"
    token_file = tmp_path / ".carbon_token"
    token_file.write_text(token, encoding="utf-8")
    os.chmod(token_file, 0o600)

    db_path = tmp_path / "api_test.db"
    audit_path = tmp_path / "audit.log"

    monkeypatch.setenv("CARBON_API_TOKEN", token)
    monkeypatch.setenv("CARBON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CARBON_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("CARBON_AUDIT_LOG_PATH", str(audit_path))
    get_settings.cache_clear()
    main_module.core = None

    app = main_module.create_app()
    with TestClient(app) as client:
        yield client, {"Authorization": f"Bearer {token}"}, db_path, audit_path

    main_module.core = None
    get_settings.cache_clear()


def test_memory_and_conversation_endpoints_require_authentication(isolated_api_client) -> None:
    """Verify every memory/conversation endpoint fails closed without bearer auth."""
    client, _, _, _ = isolated_api_client
    bad_headers = {"Authorization": "Bearer wrong-token"}

    checks = [
        ("GET", "/api/v1/memory", None),
        (
            "POST",
            "/api/v1/memory/procedural/requests",
            {"key": "workflow:x", "content": "step 1"},
        ),
        ("POST", "/api/v1/memory", {"memory_type": "semantic", "key": "k", "content": "v"}),
        ("PATCH", "/api/v1/memory/rec-1", {"content": "updated"}),
        ("DELETE", "/api/v1/memory/rec-1", None),
        ("POST", "/api/v1/memory/forget", {"query": "coffee"}),
        ("DELETE", "/api/v1/conversations/conv-1", None),
    ]
    for method, url, json_body in checks:
        res_no_auth = client.request(method, url, json=json_body)
        res_bad_auth = client.request(method, url, headers=bad_headers, json=json_body)
        assert res_no_auth.status_code == 401
        assert res_bad_auth.status_code == 401


def test_procedural_two_step_governance_and_negative_controls(isolated_api_client) -> None:
    """Verify procedural writes require server-issued GovernanceEngine approval bound by digest."""
    client, headers, _, _ = isolated_api_client

    # 1. Attempting direct POST without approval_id (even with ignored boolean) fails 403
    direct_attempt = client.post(
        "/api/v1/memory",
        headers=headers,
        json={
            "memory_type": "procedural",
            "key": "workflow:release",
            "content": "Always run full test suite.",
            "explicit_user_approval": True,
        },
    )
    assert direct_attempt.status_code == 403

    # 2. Propose procedural memory via challenge endpoint (nothing persisted yet)
    proposal = client.post(
        "/api/v1/memory/procedural/requests",
        headers=headers,
        json={
            "key": "workflow:release",
            "content": "Always run full test suite.",
        },
    )
    assert proposal.status_code == 202
    approval_id = proposal.json()["approval_id"]
    assert client.get("/api/v1/memory", headers=headers).json()["total"] == 0

    # 3. Attempting save BEFORE user approves fails 403
    pre_approval = client.post(
        "/api/v1/memory",
        headers=headers,
        json={
            "memory_type": "procedural",
            "key": "workflow:release",
            "content": "Always run full test suite.",
            "approval_id": approval_id,
        },
    )
    assert pre_approval.status_code == 403

    # 4. Human resolves approval explicitly through Governance API
    decision = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        headers=headers,
        json={"approved": True},
    )
    assert decision.status_code == 200

    # 5. Tampering with content relative to approved digest fails 403 without consuming approval
    tampered = client.post(
        "/api/v1/memory",
        headers=headers,
        json={
            "memory_type": "procedural",
            "key": "workflow:release",
            "content": "Skip tests and force push.",
            "approval_id": approval_id,
        },
    )
    assert tampered.status_code == 403

    # 6. Model-originated source metadata fails 403 even with valid approval_id
    model_spoof = client.post(
        "/api/v1/memory",
        headers=headers,
        json={
            "memory_type": "procedural",
            "key": "workflow:release",
            "content": "Always run full test suite.",
            "approval_id": approval_id,
            "source": "model_output",
        },
    )
    assert model_spoof.status_code == 403

    # 7. Exact approved payload succeeds (201) and consumes approval single-use
    ok_save = client.post(
        "/api/v1/memory",
        headers=headers,
        json={
            "memory_type": "procedural",
            "key": "workflow:release",
            "content": "Always run full test suite.",
            "conversation_id": "conv-proc-api",
            "approval_id": approval_id,
        },
    )
    assert ok_save.status_code == 201
    proc_id = ok_save.json()["record_id"]

    # 8. Replay with same approval_id fails 403
    replay = client.post(
        "/api/v1/memory",
        headers=headers,
        json={
            "memory_type": "procedural",
            "key": "workflow:release",
            "content": "Always run full test suite.",
            "approval_id": approval_id,
        },
    )
    assert replay.status_code == 403

    # 9. Updating procedural record without a fresh record-bound approval fails 403
    upd_denied = client.patch(
        f"/api/v1/memory/{proc_id}",
        headers=headers,
        json={"content": "Updated workflow without approval"},
    )
    assert upd_denied.status_code == 403


def test_memory_crud_update_audit_pagination_and_conversation_wipe(
    isolated_api_client,
) -> None:
    """Verify PATCH updates audit without leaking content, pagination, forget, and FTS wipe."""
    client, headers, db_path, audit_path = isolated_api_client

    secret_phrase = "UltraSecretPrivateMemoryValue999"
    updated_secret = "UpdatedSecretPrivateCoffeeNote888"

    # 1. Store 3 semantic memories linked to conversations
    created_ids: list[str] = []
    for idx in range(3):
        resp = client.post(
            "/api/v1/memory",
            headers=headers,
            json={
                "memory_type": "semantic",
                "key": f"note:coffee:{idx}",
                "content": f"{secret_phrase} coffee blend #{idx}",
                "conversation_id": "conv-api-1" if idx < 2 else "conv-api-2",
            },
        )
        assert resp.status_code == 201
        created_ids.append(resp.json()["record_id"])

    # 2. Explicit update via PATCH audits MEMORY_UPDATED without leaking secrets
    patch_res = client.patch(
        f"/api/v1/memory/{created_ids[2]}",
        headers=headers,
        json={"content": updated_secret, "metadata": {"conversation_id": "conv-api-2"}},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["status"] == "updated"

    # 3. Paginated listing + filters
    page1 = client.get(
        "/api/v1/memory",
        headers=headers,
        params={"memory_type": "semantic", "limit": 2, "offset": 0},
    )
    assert page1.status_code == 200
    assert page1.json()["total"] == 3
    assert len(page1.json()["items"]) == 2

    # 4. Ambiguous forget returns confirmation_required and deletes nothing
    ambiguous = client.post(
        "/api/v1/memory/forget",
        headers=headers,
        json={"query": "coffee blend", "confirm": False},
    )
    assert ambiguous.status_code == 200
    assert ambiguous.json()["status"] == "confirmation_required"
    assert ambiguous.json()["deleted_count"] == 0
    assert len(ambiguous.json()["candidates"]) == 2

    # 5. Selective forget via selected_ids + single DELETE
    assert (
        client.post(
            "/api/v1/memory/forget",
            headers=headers,
            json={"selected_ids": [created_ids[0]]},
        ).json()["deleted_count"]
        == 1
    )
    assert client.delete(f"/api/v1/memory/{created_ids[1]}", headers=headers).status_code == 200
    assert client.delete(f"/api/v1/memory/{created_ids[1]}", headers=headers).status_code == 404

    # 6. Delete entire conv-api-2 removes semantic record & FTS index cleanly
    conv_del = client.delete("/api/v1/conversations/conv-api-2", headers=headers)
    assert conv_del.status_code == 200
    assert conv_del.json()["conversations_deleted"] == 1
    assert conv_del.json()["memories_deleted"] == 1
    assert client.delete("/api/v1/conversations/conv-api-2", headers=headers).status_code == 404

    with sqlite3.connect(db_path) as sconn:
        fts_hits = sconn.execute(
            "SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?",
            (updated_secret,),
        ).fetchall()
        assert fts_hits == []

    # 7. Audit log chain valid AND includes MEMORY_UPDATED without private content
    ok, count, err = verify_audit_log_chain(audit_path)
    assert ok and count > 0, err
    audit_text = audit_path.read_text(encoding="utf-8")
    event_types = [json.loads(line)["event_type"] for line in audit_text.splitlines() if line]
    assert "MEMORY_UPDATED" in event_types
    assert secret_phrase not in audit_text
    assert updated_secret not in audit_text


@pytest.mark.asyncio
async def test_expired_memory_not_exposed_and_expired_task_payloads_masked(
    isolated_api_client,
) -> None:
    """Verify GET /api/v1/memory ignores ?include_expired=true and expired task payloads stay masked."""
    client, headers, _, _ = isolated_api_client
    core = await main_module.get_core()

    await core.memory.store(
        MemoryRecord(
            id="expired-via-api",
            memory_type=MemoryType.EPISODIC,
            key="old:turn",
            content="expired turn content",
            expires_at=datetime.now(UTC) - timedelta(seconds=5),
        )
    )
    now = datetime.now(UTC)
    await core.memory.save_task(
        PersistedTaskRecord(
            task_id="task-api-7d-masked",
            conversation_id="conv-persisted",
            prompt="Hidden secret prompt",
            state=TaskState.COMPLETED,
            result="Hidden secret answer",
            error="Hidden secret error",
            intent={"category": "GENERAL_QA", "summary": "Hidden summary"},
            created_at=now - timedelta(days=8),
            payload_expires_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(days=22),
        )
    )
    core._tasks.pop("task-api-7d-masked", None)

    # Even if client passes ?include_expired=true, expired rows are NEVER returned
    mem_res = client.get("/api/v1/memory", headers=headers, params={"include_expired": "true"})
    assert mem_res.status_code == 200
    assert mem_res.json()["total"] == 0

    task_res = client.get("/api/v1/tasks/task-api-7d-masked", headers=headers)
    assert task_res.status_code == 200
    body = task_res.json()
    assert body["state"] == "completed"
    assert body["prompt"] is None
    assert body["result"] is None
    assert body["error"] is None
    assert body["intent"] is None

    # Full conversation wipe removes persisted tasks and returns 404 on subsequent lookup
    assert client.delete("/api/v1/conversations/conv-persisted", headers=headers).status_code == 200
    assert client.get("/api/v1/tasks/task-api-7d-masked", headers=headers).status_code == 404
    assert json.loads(json.dumps(DecisionSource.USER_API.value)) == "user_api"
