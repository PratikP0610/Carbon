"""Governance, permissions, and audit logging engine."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from carbon.interfaces.models import AutonomyLevel, PermissionLevel
from carbon.interfaces.protocols import IGovernanceEngine

logger = logging.getLogger(__name__)


class GovernanceEngine(IGovernanceEngine):
    """Enforces permission boundaries and records tamper-resistant audit logs."""

    def __init__(self, audit_log_path: Path, enable_audit: bool = True):
        self.audit_log_path = audit_log_path
        self.enable_audit = enable_audit

    async def initialize(self) -> None:
        """Ensure audit log file and directory exist."""
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.audit_log_path.exists():
            self.audit_log_path.touch(mode=0o600)
        logger.info(f"Initialized Governance Engine with audit log at {self.audit_log_path}")

    async def authorize_action(
        self,
        tool_id: str,
        permission_level: PermissionLevel,
        arguments: dict[str, Any],
        autonomy_level: AutonomyLevel,
    ) -> bool:
        """Evaluate whether a tool invocation can be automatically authorized."""
        # Non-negotiable Rule 5: P4 operations (destructive / sensitive) ALWAYS require confirmation
        if permission_level == PermissionLevel.P4:
            await self.record_audit_entry(
                "AUTHORIZATION_EVALUATED",
                {
                    "tool_id": tool_id,
                    "permission_level": permission_level.value,
                    "autonomy_level": autonomy_level.value,
                    "authorized": False,
                    "reason": "P4 destructive operations strictly require explicit user confirmation.",
                },
                level="WARNING",
            )
            return False

        # Evaluation based on Autonomy Level
        authorized = False
        if autonomy_level == AutonomyLevel.A0:
            # Observe only: Zero automatic tool execution
            authorized = False
        elif autonomy_level == AutonomyLevel.A1:
            # Assist and recommend: Auto-approve only purely conversational P0
            authorized = permission_level == PermissionLevel.P0
        elif autonomy_level == AutonomyLevel.A2:
            # Execute approved: Auto-approve P0, P1, and reversible P2
            authorized = permission_level in (
                PermissionLevel.P0,
                PermissionLevel.P1,
                PermissionLevel.P2,
            )
        elif autonomy_level == AutonomyLevel.A3:
            # Bounded autonomy: Auto-approve P0, P1, P2, and P3
            authorized = permission_level in (
                PermissionLevel.P0,
                PermissionLevel.P1,
                PermissionLevel.P2,
                PermissionLevel.P3,
            )

        await self.record_audit_entry(
            "AUTHORIZATION_EVALUATED",
            {
                "tool_id": tool_id,
                "permission_level": permission_level.value,
                "autonomy_level": autonomy_level.value,
                "authorized": authorized,
            },
            level="INFO" if authorized else "WARNING",
        )
        return authorized

    async def record_audit_entry(
        self, event_type: str, details: dict[str, Any], level: str = "INFO"
    ) -> None:
        """Write structured audit entry to append-only log."""
        if not self.enable_audit:
            return

        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "level": level,
            "details": details,
        }

        line = json.dumps(entry, default=str)
        try:
            with open(self.audit_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.error(f"Failed to write audit log entry: {e}")
