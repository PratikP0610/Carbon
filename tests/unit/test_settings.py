"""Unit tests for Carbon settings and configuration."""

import pytest
from pydantic import ValidationError

from carbon.config.settings import Settings


def test_default_settings():
    """Verify safe defaults."""
    settings = Settings()
    assert settings.api_host in ("127.0.0.1", "localhost")
    assert settings.api_port == 8765
    assert settings.primary_model == "qwen3.5:9b"
    assert settings.autonomy_level == "A1"
    assert settings.max_auto_permission_level == "P1"


def test_loopback_only_security_validator():
    """Enforce Non-Negotiable Rule 6: Never allow binding to public addresses."""
    # Disallowed addresses
    for invalid_host in ["0.0.0.0", "192.168.1.10", "10.0.0.1", "example.com"]:
        with pytest.raises(ValidationError):
            Settings(api_host=invalid_host)

    # Allowed loopback variants
    for valid_host in ["127.0.0.1", "localhost", "::1"]:
        s = Settings(api_host=valid_host)
        assert s.api_host == valid_host
