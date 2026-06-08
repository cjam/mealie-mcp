"""Integration tests for set_food_label and the /api/groups/labels passthrough routes."""

from __future__ import annotations

import pytest
import httpx
from fastmcp import Client

from server import build_client


@pytest.fixture
async def mealie_http(mealie_settings):
    async with build_client(mealie_settings) as client:
        yield client


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _create_food(client: httpx.AsyncClient, name: str) -> str:
    r = await client.post("/api/foods", json={"name": name})
    r.raise_for_status()
    return r.json()["id"]


async def _create_label(client: httpx.AsyncClient, name: str, color: str = "#aabbcc") -> str:
    r = await client.post("/api/groups/labels", json={"name": name, "color": color})
    r.raise_for_status()
    return r.json()["id"]


async def _delete_food(client: httpx.AsyncClient, food_id: str) -> None:
    await client.delete(f"/api/foods/{food_id}")


async def _delete_label(client: httpx.AsyncClient, label_id: str) -> None:
    await client.delete(f"/api/groups/labels/{label_id}")


async def _get_food(client: httpx.AsyncClient, food_id: str) -> dict:
    r = await client.get(f"/api/foods/{food_id}")
    r.raise_for_status()
    return r.json()


# ── Registration ───────────────────────────────────────────────────────────────

async def test_set_food_label_registered(mcp_server):
    async with Client(mcp_server) as c:
        names = [t.name for t in await c.list_tools()]
    assert "set_food_label" in names


# ── Happy path ────────────────────────────────────────────────────────────────

async def test_set_food_label_assigns_label(mcp_server, mealie_http):
    food_id = await _create_food(mealie_http, "Test Carrot Label")
    label_id = await _create_label(mealie_http, "Test Produce")
    try:
        async with Client(mcp_server) as c:
            result = await c.call_tool("set_food_label", {
                "food_name": "Test Carrot Label",
                "label_name": "Test Produce",
            })
        text = result.content[0].text
        assert "ERROR" not in text
        assert "Test Produce" in text

        food = await _get_food(mealie_http, food_id)
        assert food.get("labelId") == label_id
    finally:
        await _delete_food(mealie_http, food_id)
        await _delete_label(mealie_http, label_id)


async def test_set_food_label_case_insensitive(mcp_server, mealie_http):
    food_id = await _create_food(mealie_http, "Test Milk Label")
    label_id = await _create_label(mealie_http, "Test Dairy")
    try:
        async with Client(mcp_server) as c:
            result = await c.call_tool("set_food_label", {
                "food_name": "TEST MILK LABEL",
                "label_name": "test dairy",
            })
        text = result.content[0].text
        assert "ERROR" not in text

        food = await _get_food(mealie_http, food_id)
        assert food.get("labelId") == label_id
    finally:
        await _delete_food(mealie_http, food_id)
        await _delete_label(mealie_http, label_id)


# ── Error cases ───────────────────────────────────────────────────────────────

async def test_set_food_label_food_not_found(mcp_server):
    async with Client(mcp_server) as c:
        result = await c.call_tool("set_food_label", {
            "food_name": "xyzzy-no-such-food-abc",
            "label_name": "Produce",
        })
    assert "ERROR" in result.content[0].text


async def test_set_food_label_label_not_found(mcp_server, mealie_http):
    food_id = await _create_food(mealie_http, "Test Apple Label")
    try:
        async with Client(mcp_server) as c:
            result = await c.call_tool("set_food_label", {
                "food_name": "Test Apple Label",
                "label_name": "xyzzy-no-such-label-abc",
            })
        assert "ERROR" in result.content[0].text
    finally:
        await _delete_food(mealie_http, food_id)


# ── assign_food_labels ────────────────────────────────────────────────────────

async def test_assign_food_labels_registered(mcp_server):
    async with Client(mcp_server) as c:
        names = [t.name for t in await c.list_tools()]
    assert "assign_food_labels" in names


async def test_assign_food_labels_bulk(mcp_server, mealie_http):
    """All foods in both label groups get the correct labelId."""
    carrot_id = await _create_food(mealie_http, "Bulk Carrot")
    onion_id  = await _create_food(mealie_http, "Bulk Onion")
    milk_id   = await _create_food(mealie_http, "Bulk Milk")
    produce_id = await _create_label(mealie_http, "Bulk Produce")
    dairy_id   = await _create_label(mealie_http, "Bulk Dairy")
    try:
        async with Client(mcp_server) as c:
            result = await c.call_tool("assign_food_labels", {
                "assignments": {
                    "Bulk Produce": ["Bulk Carrot", "Bulk Onion"],
                    "Bulk Dairy":   ["Bulk Milk"],
                },
            })
        text = result.content[0].text
        assert "ERROR" not in text

        assert (await _get_food(mealie_http, carrot_id)).get("labelId") == produce_id
        assert (await _get_food(mealie_http, onion_id)).get("labelId")  == produce_id
        assert (await _get_food(mealie_http, milk_id)).get("labelId")   == dairy_id
    finally:
        for fid in (carrot_id, onion_id, milk_id):
            await _delete_food(mealie_http, fid)
        for lid in (produce_id, dairy_id):
            await _delete_label(mealie_http, lid)


async def test_assign_food_labels_unknown_label_continues(mcp_server, mealie_http):
    """An unknown label is reported but does not abort processing of other labels."""
    food_id  = await _create_food(mealie_http, "Bulk Pepper")
    label_id = await _create_label(mealie_http, "Bulk Spice")
    try:
        async with Client(mcp_server) as c:
            result = await c.call_tool("assign_food_labels", {
                "assignments": {
                    "xyzzy-no-such-label": ["Bulk Pepper"],
                    "Bulk Spice": ["Bulk Pepper"],
                },
            })
        text = result.content[0].text
        assert "NOT FOUND" in text or "not found" in text.lower()
        assert (await _get_food(mealie_http, food_id)).get("labelId") == label_id
    finally:
        await _delete_food(mealie_http, food_id)
        await _delete_label(mealie_http, label_id)


async def test_assign_food_labels_unknown_food_continues(mcp_server, mealie_http):
    """An unknown food name is reported but does not abort the rest of the batch."""
    food_id  = await _create_food(mealie_http, "Bulk Butter")
    label_id = await _create_label(mealie_http, "Bulk Fats")
    try:
        async with Client(mcp_server) as c:
            result = await c.call_tool("assign_food_labels", {
                "assignments": {
                    "Bulk Fats": ["xyzzy-no-such-food", "Bulk Butter"],
                },
            })
        text = result.content[0].text
        assert "NOT FOUND" in text or "not found" in text.lower()
        assert (await _get_food(mealie_http, food_id)).get("labelId") == label_id
    finally:
        await _delete_food(mealie_http, food_id)
        await _delete_label(mealie_http, label_id)
