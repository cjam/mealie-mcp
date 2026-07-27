"""Mealie MCP server.

Generates MCP tools directly from the Mealie OpenAPI spec — no hand-written
endpoint wrappers. Curation lives in `routes.py`; auth + config in `config.py`.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Generator

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.server import Transport
from typing import cast

from config import Settings, load_settings
from routes import build_route_maps
from tools import register_all_tools

logger = logging.getLogger("mealie-mcp")

# Holds the Mealie API token for the duration of a single MCP request.
_request_token: contextvars.ContextVar[str] = contextvars.ContextVar("request_token", default="")


class _ForwardedBearerAuth(httpx.Auth):
    """Attaches a Bearer token to outgoing Mealie requests.

    Priority: per-request contextvar (set by TokenForwardMiddleware from the
    incoming MCP Authorization header) > fallback_token (from MEALIE_API_TOKEN
    env var, used for in-process/stdio transports and private deployments).
    """

    def __init__(self, fallback_token: str = "") -> None:
        self._fallback = fallback_token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        token = _request_token.get() or self._fallback
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request


class TokenForwardMiddleware(Middleware):
    """Extracts the incoming Authorization header and makes it available to the httpx client."""

    async def on_message(self, context: MiddlewareContext, call_next) -> object:
        try:
            request = get_http_request()
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                _request_token.set(auth[7:])
        except RuntimeError:
            pass
        return await call_next(context)


def fetch_openapi_spec(settings: Settings) -> dict:
    """Pull the OpenAPI spec from the running Mealie instance."""
    logger.info("Fetching OpenAPI spec from %s", settings.openapi_url)
    # Use the static token (if configured) just to fetch the spec at startup.
    headers = {}
    if settings.mealie_api_token:
        headers["Authorization"] = f"Bearer {settings.mealie_api_token}"
    resp = httpx.get(settings.openapi_url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def build_client(settings: Settings) -> httpx.AsyncClient:
    """Async client whose auth token comes from the per-request contextvar, with
    MEALIE_API_TOKEN as a fallback for in-process/stdio transports."""
    return httpx.AsyncClient(
        base_url=settings.mealie_base_url,
        auth=_ForwardedBearerAuth(fallback_token=settings.mealie_api_token),
        timeout=30.0,
    )


def build_server(settings: Settings) -> FastMCP:
    spec = fetch_openapi_spec(settings)
    client = build_client(settings)
    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="Mealie",
        route_maps=build_route_maps(),
    )
    mcp.add_middleware(TokenForwardMiddleware())
    register_all_tools(mcp, client)
    return mcp


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mcp = build_server(settings)
    logger.info("Starting Mealie MCP on %s:%s (%s)", settings.host, settings.port, settings.transport)
    kwargs: dict = {} if settings.transport == "stdio" else {"host": settings.host, "port": settings.port}
    mcp.run(transport=cast(Transport, settings.transport), **kwargs)


if __name__ == "__main__":
    main()
