"""Global pytest isolation safeguards preventing tests from touching live Carbon data."""

from collections.abc import Iterator
from pathlib import Path

import pytest

import carbon.main as main_module
from carbon.config.settings import get_settings


@pytest.fixture(autouse=True)
def _isolate_carbon_runtime_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Ensure every test defaults to an isolated temporary data/DB/audit directory."""
    isolated_dir = tmp_path / "_isolated_data"
    isolated_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("CARBON_DATA_DIR", str(isolated_dir))
    monkeypatch.setenv("CARBON_DATABASE_PATH", str(isolated_dir / "carbon.db"))
    monkeypatch.setenv("CARBON_AUDIT_LOG_PATH", str(isolated_dir / "audit.log"))

    get_settings.cache_clear()
    main_module.core = None
    yield
    main_module.core = None
    get_settings.cache_clear()
