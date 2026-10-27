"""Unit tests for Carbon Governance and Permission Engine."""

from pathlib import Path

import pytest

from carbon.governance.governance_engine import GovernanceEngine
from carbon.interfaces.models import AutonomyLevel, PermissionLevel


@pytest.fixture
async def governance(tmp_path: Path):
    audit_file = tmp_path / "test_audit.log"
    engine = GovernanceEngine(audit_log_path=audit_file, enable_audit=True)
    await engine.initialize()
    return engine


@pytest.mark.asyncio
async def test_p4_always_requires_confirmation(governance: GovernanceEngine):
    """P4 (destructive/high-impact) operations MUST NOT be auto-authorized under ANY autonomy level."""
    for autonomy in [AutonomyLevel.A0, AutonomyLevel.A1, AutonomyLevel.A2, AutonomyLevel.A3]:
        authorized = await governance.authorize_action(
            tool_id="delete_file",
            permission_level=PermissionLevel.P4,
            arguments={"path": "/tmp/test"},
            autonomy_level=autonomy,
        )
        assert authorized is False, f"P4 action was unexpectedly authorized under {autonomy}!"


@pytest.mark.asyncio
async def test_autonomy_level_a0(governance: GovernanceEngine):
    """A0 (Observe only) never auto-authorizes any action."""
    for perm in [PermissionLevel.P0, PermissionLevel.P1, PermissionLevel.P2, PermissionLevel.P3]:
        authorized = await governance.authorize_action(
            tool_id="test_tool",
            permission_level=perm,
            arguments={},
            autonomy_level=AutonomyLevel.A0,
        )
        assert authorized is False


@pytest.mark.asyncio
async def test_autonomy_level_a1(governance: GovernanceEngine):
    """A1 (Assist & recommend) auto-authorizes only P0 conversational."""
    assert (
        await governance.authorize_action("p0_tool", PermissionLevel.P0, {}, AutonomyLevel.A1)
        is True
    )
    assert (
        await governance.authorize_action("p1_tool", PermissionLevel.P1, {}, AutonomyLevel.A1)
        is False
    )
    assert (
        await governance.authorize_action("p2_tool", PermissionLevel.P2, {}, AutonomyLevel.A1)
        is False
    )


@pytest.mark.asyncio
async def test_autonomy_level_a2(governance: GovernanceEngine):
    """A2 (Execute approved) auto-authorizes P0, P1, and P2."""
    assert (
        await governance.authorize_action("p0_tool", PermissionLevel.P0, {}, AutonomyLevel.A2)
        is True
    )
    assert (
        await governance.authorize_action("p1_tool", PermissionLevel.P1, {}, AutonomyLevel.A2)
        is True
    )
    assert (
        await governance.authorize_action("p2_tool", PermissionLevel.P2, {}, AutonomyLevel.A2)
        is True
    )
    assert (
        await governance.authorize_action("p3_tool", PermissionLevel.P3, {}, AutonomyLevel.A2)
        is False
    )


@pytest.mark.asyncio
async def test_audit_log_generation(governance: GovernanceEngine):
    """Verify append-only audit entries are recorded."""
    await governance.record_audit_entry("TEST_EVENT", {"detail": "verification"})
    assert governance.audit_log_path.exists()
    content = governance.audit_log_path.read_text(encoding="utf-8")
    assert "TEST_EVENT" in content
    assert "verification" in content
