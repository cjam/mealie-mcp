"""Mealie MCP server.

Generates MCP tools directly from the Mealie OpenAPI spec — no hand-written
endpoint wrappers. Curation lives in `routes.py`; auth + config in `config.py`.
"""

from __future__ import annotations

import logging

import httpx
from fastmcp import FastMCP

from cleanup import register_cleanup_tools
from config import Settings, load_settings
from routes import build_route_maps

logger = logging.getLogger("mealie-mcp")


def fetch_openapi_spec(settings: Settings) -> dict:
    """Pull the OpenAPI spec from the running Mealie instance."""
    logger.info("Fetching OpenAPI spec from %s", settings.openapi_url)
    resp = httpx.get(settings.openapi_url, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def build_client(settings: Settings) -> httpx.AsyncClient:
    """Authenticated async client used by the generated tools."""
    return httpx.AsyncClient(
        base_url=settings.mealie_base_url,
        headers={"Authorization": f"Bearer {settings.mealie_api_token}"},
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
    register_cleanup_tools(mcp, client)
    return mcp


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mcp = build_server(settings)
    logger.info("Starting Mealie MCP on %s:%s (%s)", settings.host, settings.port, settings.transport)
    mcp.run(transport=settings.transport, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
