"""Tests for upload_recipe_image and raw-PUT route exclusion."""

from __future__ import annotations

import base64
import struct
import zlib

import pytest
from fastmcp import Client

from server import build_client


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def mealie_http(mealie_settings):
    async with build_client(mealie_settings) as client:
        yield client


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_tiny_png() -> str:
    """Generate a minimal valid 1×1 red-pixel PNG and return it as base64."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00\xff\x00\x00"  # filter byte + 1px RGB (red)
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return base64.b64encode(signature + ihdr + idat + iend).decode()


async def _create_recipe(client, name: str) -> str:
    r = await client.post("/api/recipes", json={"name": name})
    r.raise_for_status()
    return r.json()


async def _delete_recipe(client, slug: str) -> None:
    await client.delete(f"/api/recipes/{slug}")


# ── Registration ──────────────────────────────────────────────────────────────


async def test_upload_recipe_image_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "upload_recipe_image" for t in tools)


# ── Error cases ───────────────────────────────────────────────────────────────


async def test_upload_recipe_image_unknown_slug_returns_error(mcp_server):
    """upload_recipe_image returns a readable error for a non-existent recipe."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "upload_recipe_image",
            {"recipe_slug": "does-not-exist-xyzzy", "image_b64": "aGVsbG8="},
        )
    assert "ERROR" in result.content[0].text


async def test_upload_recipe_image_bad_base64_returns_error(mealie_http, mcp_server):
    """upload_recipe_image returns a readable error for invalid base64 data."""
    slug = await _create_recipe(mealie_http, "ImageUpload: Bad B64")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "upload_recipe_image",
                {"recipe_slug": slug, "image_b64": "!!!not-valid-base64!!!"},
            )
        assert "ERROR" in result.content[0].text
    finally:
        await _delete_recipe(mealie_http, slug)


# ── Happy path ────────────────────────────────────────────────────────────────


async def test_upload_recipe_image_sends_valid_png(mealie_http, mcp_server):
    """upload_recipe_image accepts a valid PNG and either succeeds or returns a
    Mealie-side error — it must not crash or return a base64/network error."""
    slug = await _create_recipe(mealie_http, "ImageUpload: PNG Upload")
    try:
        png_b64 = _make_tiny_png()
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "upload_recipe_image",
                {"recipe_slug": slug, "image_b64": png_b64, "extension": "png"},
            )
        text = result.content[0].text
        # Accept Mealie success or a non-crash Mealie-side rejection.
        # What must NOT appear: base64 decode error or network/fetch error.
        assert "invalid base64" not in text.lower(), f"Base64 decode failed:\n{text}"
        assert "upload request failed" not in text.lower(), f"Network error:\n{text}"
    finally:
        await _delete_recipe(mealie_http, slug)


# ── Route exclusion ───────────────────────────────────────────────────────────


async def test_raw_put_recipe_tool_is_not_exposed(mcp_server):
    """The auto-generated PUT /api/recipes/{slug} tool must not appear in the tool list.

    Raw PUT wipes all fields absent from the payload (tags, categories, etc.).
    Composed tools (update_recipe, enrich_recipe, …) are the safe alternatives.
    """
    async with Client(mcp_server) as client:
        tools = await client.list_tools()

    # FastMCP generates names like "update_one_api_recipes__slug__put" and
    # "update_many_api_recipes_put" for PUT /api/recipes/{slug} and PUT /api/recipes.
    # Both are excluded because raw PUT wipes fields absent from the payload.
    dangerous = [
        t.name for t in tools
        if "api_recipes" in t.name.lower() and t.name.lower().endswith("_put")
    ]
    assert not dangerous, (
        f"Raw PUT recipe tool(s) still exposed: {dangerous}\n"
        "Check build_route_maps() in routes.py."
    )
