"""Integration tests for create_food, update_food, delete_food, list_unlabeled_foods."""

from __future__ import annotations

import pytest
import httpx
from fastmcp import Client

from server import build_client


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def mealie_http(mealie_settings):
    async with build_client(mealie_settings) as client:
        yield client


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _food_by_name(client: httpx.AsyncClient, name: str) -> dict | None:
    r = await client.get("/api/foods", params={"perPage": 200})
    r.raise_for_status()
    return next(
        (f for f in r.json().get("items", []) if f["name"].lower() == name.lower()),
        None,
    )


async def _create_food(client: httpx.AsyncClient, name: str) -> str:
    r = await client.post("/api/foods", json={"name": name})
    r.raise_for_status()
    return r.json()["id"]


async def _delete_food_by_name(client: httpx.AsyncClient, *names: str) -> None:
    r = await client.get("/api/foods", params={"perPage": 200})
    r.raise_for_status()
    for food in r.json().get("items", []):
        if food["name"].lower() in {n.lower() for n in names}:
            await client.delete(f"/api/foods/{food['id']}")


async def _create_label(client: httpx.AsyncClient, name: str) -> str:
    r = await client.post("/api/groups/labels", json={"name": name})
    r.raise_for_status()
    return r.json()["id"]


async def _delete_label(client: httpx.AsyncClient, label_id: str) -> None:
    await client.delete(f"/api/groups/labels/{label_id}")


async def _food_label_id(client: httpx.AsyncClient, food_id: str) -> str | None:
    r = await client.get(f"/api/foods/{food_id}")
    r.raise_for_status()
    return r.json().get("labelId")


# ── Registration tests ────────────────────────────────────────────────────────

async def test_create_food_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "create_food" for t in tools)


async def test_update_food_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "update_food" for t in tools)


async def test_delete_food_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "delete_food" for t in tools)


async def test_list_unlabeled_foods_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "list_unlabeled_foods" for t in tools)


# ── create_food ───────────────────────────────────────────────────────────────

async def test_create_food_creates_new_entry(mealie_http, mcp_server):
    await _delete_food_by_name(mealie_http, "Dragonfruit")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool("create_food", {"name": "Dragonfruit"})
        assert "Created" in result.content[0].text
        assert await _food_by_name(mealie_http, "Dragonfruit") is not None
    finally:
        await _delete_food_by_name(mealie_http, "Dragonfruit")


async def test_create_food_returns_existing_instead_of_duplicate(mealie_http, mcp_server):
    await _delete_food_by_name(mealie_http, "Saffron")
    fid = await _create_food(mealie_http, "Saffron")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool("create_food", {"name": "saffron"})
        assert "Found existing" in result.content[0].text
        r = await mealie_http.get("/api/foods", params={"perPage": 200})
        variants = [f for f in r.json()["items"] if f["name"].lower() == "saffron"]
        assert len(variants) == 1, "create_food must not create a duplicate"
    finally:
        await _delete_food_by_name(mealie_http, "Saffron", "saffron")


async def test_create_food_assigns_label(mealie_http, mcp_server):
    await _delete_food_by_name(mealie_http, "Starfruit")
    label_id = await _create_label(mealie_http, "_TestFruits")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "create_food", {"name": "Starfruit", "label_name": "_TestFruits"}
            )
        text = result.content[0].text
        assert "Created" in text
        assert "_TestFruits" in text

        food = await _food_by_name(mealie_http, "Starfruit")
        assert food is not None
        assert await _food_label_id(mealie_http, food["id"]) == label_id
    finally:
        await _delete_food_by_name(mealie_http, "Starfruit")
        await _delete_label(mealie_http, label_id)


async def test_create_food_unknown_label_returns_error(mealie_http, mcp_server):
    await _delete_food_by_name(mealie_http, "Kumquat")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "create_food",
                {"name": "Kumquat", "label_name": "_NoSuchLabel_xyz"},
            )
        assert "ERROR" in result.content[0].text
    finally:
        await _delete_food_by_name(mealie_http, "Kumquat")


# ── update_food ───────────────────────────────────────────────────────────────

async def test_update_food_renames(mealie_http, mcp_server):
    await _delete_food_by_name(mealie_http, "Tumeric", "Turmeric")
    fid = await _create_food(mealie_http, "Tumeric")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "update_food", {"food_name": "Tumeric", "new_name": "Turmeric"}
            )
        text = result.content[0].text
        assert "Turmeric" in text
        assert "ERROR" not in text
        assert await _food_by_name(mealie_http, "Turmeric") is not None
        assert await _food_by_name(mealie_http, "Tumeric") is None
    finally:
        await _delete_food_by_name(mealie_http, "Tumeric", "Turmeric")


async def test_update_food_assigns_label(mealie_http, mcp_server):
    await _delete_food_by_name(mealie_http, "Cardamom")
    fid = await _create_food(mealie_http, "Cardamom")
    label_id = await _create_label(mealie_http, "_TestSpices")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "update_food", {"food_name": "Cardamom", "label_name": "_TestSpices"}
            )
        assert "ERROR" not in result.content[0].text
        assert await _food_label_id(mealie_http, fid) == label_id
    finally:
        await _delete_food_by_name(mealie_http, "Cardamom")
        await _delete_label(mealie_http, label_id)


async def test_update_food_no_op_returns_error(mcp_server):
    async with Client(mcp_server) as client:
        result = await client.call_tool("update_food", {"food_name": "Anything"})
    assert "ERROR" in result.content[0].text


async def test_update_food_unknown_food_returns_error(mcp_server):
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "update_food",
            {"food_name": "_NoSuchFood_xyz", "new_name": "Whatever"},
        )
    assert "ERROR" in result.content[0].text


# ── delete_food ───────────────────────────────────────────────────────────────

async def test_delete_food_removes_entry(mealie_http, mcp_server):
    await _delete_food_by_name(mealie_http, "Garnishes")
    await _create_food(mealie_http, "Garnishes")
    async with Client(mcp_server) as client:
        result = await client.call_tool("delete_food", {"food_name": "Garnishes"})
    assert "Deleted" in result.content[0].text
    assert await _food_by_name(mealie_http, "Garnishes") is None


async def test_delete_food_case_insensitive(mealie_http, mcp_server):
    await _delete_food_by_name(mealie_http, "Extra-Virgin")
    await _create_food(mealie_http, "Extra-Virgin")
    async with Client(mcp_server) as client:
        result = await client.call_tool("delete_food", {"food_name": "extra-virgin"})
    assert "Deleted" in result.content[0].text
    assert await _food_by_name(mealie_http, "Extra-Virgin") is None


async def test_delete_food_unknown_returns_error(mcp_server):
    async with Client(mcp_server) as client:
        result = await client.call_tool("delete_food", {"food_name": "_NoSuchFood_xyz"})
    assert "ERROR" in result.content[0].text


# ── list_unlabeled_foods ──────────────────────────────────────────────────────

async def test_list_unlabeled_foods_includes_unlabeled(mealie_http, mcp_server):
    await _delete_food_by_name(mealie_http, "Ugli Fruit")
    await _create_food(mealie_http, "Ugli Fruit")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool("list_unlabeled_foods", {})
        text = result.content[0].text
        assert "Ugli Fruit" in text
    finally:
        await _delete_food_by_name(mealie_http, "Ugli Fruit")


async def test_list_unlabeled_foods_excludes_labeled(mealie_http, mcp_server):
    await _delete_food_by_name(mealie_http, "Tamarind")
    fid = await _create_food(mealie_http, "Tamarind")
    label_id = await _create_label(mealie_http, "_TestLabeled")
    try:
        r = await mealie_http.get(f"/api/foods/{fid}")
        full = r.json()
        full["labelId"] = label_id
        await mealie_http.put(f"/api/foods/{fid}", json=full)

        async with Client(mcp_server) as client:
            result = await client.call_tool("list_unlabeled_foods", {})
        assert "Tamarind" not in result.content[0].text
    finally:
        await _delete_food_by_name(mealie_http, "Tamarind")
        await _delete_label(mealie_http, label_id)
