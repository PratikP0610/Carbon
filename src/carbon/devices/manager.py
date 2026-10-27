"""Device management engine for local host and future connected nodes."""

import asyncio
import logging
import os
import platform
from typing import Any

from carbon.interfaces.protocols import IDeviceEngine

logger = logging.getLogger(__name__)


class DeviceManager(IDeviceEngine):
    """Manages local macOS node and provides interfaces for future connected Android nodes."""

    def __init__(self):
        self._connected_nodes: list[dict[str, Any]] = []

    async def get_host_status(self) -> dict[str, Any]:
        """Query local macOS host system metrics and device state."""
        mem_bytes = 0
        try:
            proc = await asyncio.create_subprocess_exec(
                "sysctl",
                "-n",
                "hw.memsize",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            mem_bytes = int(stdout.decode().strip() or "0")
        except Exception:
            pass

        return {
            "device_id": "mac-local",
            "name": platform.node(),
            "os": platform.system(),
            "version": platform.mac_ver()[0] or platform.version(),
            "arch": platform.machine(),
            "cpu_count": os.cpu_count() or 1,
            "unified_memory_bytes": mem_bytes,
            "unified_memory_gb": round(mem_bytes / (1024**3), 2),
            "status": "online",
        }

    async def list_connected_nodes(self) -> list[dict[str, Any]]:
        """List active connected nodes (Mac host + future paired Android devices)."""
        host = await self.get_host_status()
        return [host] + self._connected_nodes
