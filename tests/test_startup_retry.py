"""Tests that fetch_openapi_spec retries transient connection failures at startup
instead of crashing the process (e.g. Mealie not yet reachable on container boot)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from config import Settings
from server import fetch_openapi_spec


def _settings() -> Settings:
    return Settings(
        mealie_base_url="http://meals:9000",
        mealie_api_token="tok",
        host="0.0.0.0",
        port=8000,
        transport="http",
        log_level="INFO",
    )


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"paths": {}}
    return resp


def test_fetch_openapi_spec_succeeds_first_try():
    with patch("server.httpx.get", return_value=_ok_response()) as mock_get:
        spec = fetch_openapi_spec(_settings())

    assert spec == {"paths": {}}
    assert mock_get.call_count == 1


def test_fetch_openapi_spec_retries_on_connection_error_then_succeeds():
    mock_get = MagicMock(
        side_effect=[
            httpx.ConnectError("No route to host"),
            httpx.ConnectError("No route to host"),
            _ok_response(),
        ]
    )

    with (
        patch("server.httpx.get", mock_get),
        patch("server.time.sleep") as mock_sleep,
    ):
        spec = fetch_openapi_spec(_settings())

    assert spec == {"paths": {}}
    assert mock_get.call_count == 3
    assert mock_sleep.call_count == 2


def test_fetch_openapi_spec_gives_up_after_max_retries():
    mock_get = MagicMock(side_effect=httpx.ConnectError("No route to host"))

    with (
        patch("server.httpx.get", mock_get),
        patch("server.time.sleep"),
    ):
        with pytest.raises(httpx.ConnectError):
            fetch_openapi_spec(_settings())

    assert mock_get.call_count == server_max_attempts()


def server_max_attempts() -> int:
    from server import STARTUP_MAX_ATTEMPTS

    return STARTUP_MAX_ATTEMPTS


def test_fetch_openapi_spec_does_not_retry_on_http_status_error():
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401 Unauthorized", request=MagicMock(), response=MagicMock(status_code=401)
    )
    mock_get = MagicMock(return_value=resp)

    with (
        patch("server.httpx.get", mock_get),
        patch("server.time.sleep") as mock_sleep,
    ):
        with pytest.raises(httpx.HTTPStatusError):
            fetch_openapi_spec(_settings())

    assert mock_get.call_count == 1
    mock_sleep.assert_not_called()
