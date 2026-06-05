"""Tests for per-request token forwarding.

The MCP server carries no stored Mealie credentials. Instead, each client
passes its Mealie API token in the Authorization header and the server
forwards it transparently to every Mealie API call.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.utilities.tests import run_server_async
from starlette.datastructures import Headers

from config import Settings
from server import TokenForwardMiddleware, _ForwardedBearerAuth, _request_token, build_server


# ---------------------------------------------------------------------------
# Unit: _ForwardedBearerAuth
# ---------------------------------------------------------------------------


def test_auth_sets_bearer_header_from_contextvar():
    token = "mealie-token-abc"
    _token = _request_token.set(token)
    try:
        auth = _ForwardedBearerAuth()
        request = httpx.Request("GET", "http://mealie.example.com/api/recipes")
        (modified,) = list(auth.auth_flow(request))
        assert modified.headers["Authorization"] == f"Bearer {token}"
    finally:
        _request_token.reset(_token)


def test_auth_omits_header_when_contextvar_empty_and_no_fallback():
    _token = _request_token.set("")
    try:
        auth = _ForwardedBearerAuth(fallback_token="")
        request = httpx.Request("GET", "http://mealie.example.com/api/recipes")
        (modified,) = list(auth.auth_flow(request))
        assert "Authorization" not in modified.headers
    finally:
        _request_token.reset(_token)


def test_auth_uses_fallback_when_contextvar_empty():
    _token = _request_token.set("")
    try:
        auth = _ForwardedBearerAuth(fallback_token="fallback-token")
        request = httpx.Request("GET", "http://mealie.example.com/api/recipes")
        (modified,) = list(auth.auth_flow(request))
        assert modified.headers["Authorization"] == "Bearer fallback-token"
    finally:
        _request_token.reset(_token)


def test_auth_contextvar_takes_precedence_over_fallback():
    _token = _request_token.set("per-request-token")
    try:
        auth = _ForwardedBearerAuth(fallback_token="fallback-token")
        request = httpx.Request("GET", "http://mealie.example.com/api/recipes")
        (modified,) = list(auth.auth_flow(request))
        assert modified.headers["Authorization"] == "Bearer per-request-token"
    finally:
        _request_token.reset(_token)


# ---------------------------------------------------------------------------
# Unit: TokenForwardMiddleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_middleware_extracts_bearer_token():
    mw = TokenForwardMiddleware()
    call_next = AsyncMock(return_value="ok")
    ctx = MagicMock(spec=MiddlewareContext)

    fake_request = MagicMock()
    fake_request.headers = Headers({"authorization": "Bearer my-secret-token"})

    with patch("server.get_http_request", return_value=fake_request):
        await mw.on_message(ctx, call_next)

    assert _request_token.get() == "my-secret-token"
    call_next.assert_awaited_once()


@pytest.mark.asyncio
async def test_middleware_ignores_non_bearer_auth():
    mw = TokenForwardMiddleware()
    call_next = AsyncMock(return_value="ok")
    ctx = MagicMock(spec=MiddlewareContext)

    fake_request = MagicMock()
    fake_request.headers = Headers({"authorization": "Basic dXNlcjpwYXNz"})

    _token = _request_token.set("previous-value")
    try:
        with patch("server.get_http_request", return_value=fake_request):
            await mw.on_message(ctx, call_next)
        # Non-Bearer auth should not overwrite the contextvar
        assert _request_token.get() == "previous-value"
    finally:
        _request_token.reset(_token)


@pytest.mark.asyncio
async def test_middleware_handles_missing_http_request_gracefully():
    """Middleware must not crash when there is no HTTP request (e.g. stdio transport)."""
    mw = TokenForwardMiddleware()
    call_next = AsyncMock(return_value="ok")
    ctx = MagicMock(spec=MiddlewareContext)

    with patch("server.get_http_request", side_effect=RuntimeError("No active HTTP request")):
        result = await mw.on_message(ctx, call_next)

    assert result == "ok"
    call_next.assert_awaited_once()


# ---------------------------------------------------------------------------
# Integration: token forwarded end-to-end over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def tokenless_settings(mealie_base_url):
    """Settings with no embedded API token — token must come from the client."""
    return Settings(
        mealie_base_url=mealie_base_url,
        mealie_api_token="",
        host="127.0.0.1",
        port=8000,
        transport="http",
        log_level="DEBUG",
    )


@pytest.mark.asyncio
async def test_token_forwarded_via_http_header(mealie_base_url, tokenless_settings):
    """Client-supplied Authorization header reaches Mealie and returns real data."""
    # Obtain a real token the same way conftest does.
    resp = httpx.post(
        f"{mealie_base_url}/api/auth/token",
        data={"username": "changeme@example.com", "password": "MyPassword"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]

    server = build_server(tokenless_settings)
    async with run_server_async(server) as url:
        transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
        async with Client(transport) as client:
            tools = await client.list_tools()

    assert tools, "Expected at least one tool to be registered"


@pytest.mark.asyncio
async def test_no_token_returns_error(tokenless_settings):
    """Requests without a token should fail — Mealie rejects unauthenticated calls."""
    server = build_server(tokenless_settings)
    async with run_server_async(server) as url:
        async with Client(StreamableHttpTransport(url)) as client:
            with pytest.raises(Exception):
                await client.call_tool("get_all_api_recipes_get", {})
