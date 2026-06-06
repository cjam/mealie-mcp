"""Tests that mcp.run() receives correct kwargs for each transport mode."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from config import Settings
from server import main


def _settings(transport: str) -> Settings:
    return Settings(
        mealie_base_url="https://mealie.example.com",
        mealie_api_token="tok",
        host="0.0.0.0",
        port=8000,
        transport=transport,
        log_level="INFO",
    )


@pytest.mark.parametrize(
    "transport, expect_host_port",
    [
        ("stdio", False),
        ("http", True),
        ("sse", True),
    ],
)
def test_run_kwargs_by_transport(transport, expect_host_port):
    mock_mcp = MagicMock()

    with (
        patch("server.load_settings", return_value=_settings(transport)),
        patch("server.build_server", return_value=mock_mcp),
    ):
        main()

    _, kwargs = mock_mcp.run.call_args
    if expect_host_port:
        assert "host" in kwargs
        assert "port" in kwargs
    else:
        assert "host" not in kwargs
        assert "port" not in kwargs
