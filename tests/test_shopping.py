"""Integration tests for the normalize_shopping_list tool."""

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


# ── API helpers ───────────────────────────────────────────────────────────────

async def _create_shopping_list(client: httpx.AsyncClient, name: str) -> str:
    r = await client.post("/api/households/shopping/lists", json={"name": name})
    r.raise_for_status()
    return r.json()["id"]


async def _delete_shopping_list(client: httpx.AsyncClient, list_id: str) -> None:
    await client.delete(f"/api/households/shopping/lists/{list_id}")


async def _get_or_create_food(client: httpx.AsyncClient, name: str) -> dict:
    r = await client.post("/api/foods", json={"name": name})
    if r.is_success:
        return r.json()
    lr = await client.get("/api/foods", params={"perPage": 200})
    lr.raise_for_status()
    food = next((f for f in lr.json().get("items", []) if f["name"].lower() == name.lower()), None)
    assert food is not None, f"food '{name}' not found after failed create"
    return food


async def _get_or_create_unit(client: httpx.AsyncClient, name: str, abbr: str = "") -> dict:
    r = await client.post("/api/units", json={"name": name, "abbreviation": abbr})
    if r.is_success:
        return r.json()
    lr = await client.get("/api/units", params={"perPage": 200})
    lr.raise_for_status()
    unit = next((u for u in lr.json().get("items", []) if u["name"].lower() == name.lower()), None)
    assert unit is not None, f"unit '{name}' not found after failed create"
    return unit


async def _add_item(
    client: httpx.AsyncClient,
    list_id: str,
    food: dict,
    unit: dict | None,
    quantity: float,
) -> dict:
    payload: dict = {
        "shoppingListId": list_id,
        "quantity": quantity,
        "foodId": food["id"],
        "note": "",
        "checked": False,
    }
    if unit:
        payload["unitId"] = unit["id"]
    r = await client.post("/api/households/shopping/items", json=payload)
    r.raise_for_status()
    return r.json()


async def _add_item_bypassing_merge(
    client: httpx.AsyncClient,
    list_id: str,
    food: dict,
    unit: dict,
    quantity: float,
) -> dict:
    """Add a shopping list item without triggering Mealie's insert-time auto-merge.

    Mealie merges items that share a foodId when they are POSTed. Work around
    this by inserting a text-only placeholder first, then PUTting the food/unit
    onto the existing item, which does not trigger the merge logic.
    """
    r = await client.post("/api/households/shopping/items", json={
        "shoppingListId": list_id, "quantity": quantity,
        "note": "__placeholder__", "checked": False,
    })
    r.raise_for_status()
    data = r.json()
    item = data["createdItems"][0] if "createdItems" in data else data
    item_id = item["id"]

    full_r = await client.get(f"/api/households/shopping/items/{item_id}")
    full_r.raise_for_status()
    full = full_r.json()
    full["foodId"] = food["id"]
    full["unitId"] = unit["id"]
    full["food"] = food
    full["unit"] = unit
    full["quantity"] = quantity
    full["note"] = ""
    put_r = await client.put(f"/api/households/shopping/items/{item_id}", json=full)
    put_r.raise_for_status()
    return item


async def _list_items(client: httpx.AsyncClient, list_id: str) -> list[dict]:
    r = await client.get(f"/api/households/shopping/lists/{list_id}")
    r.raise_for_status()
    return r.json().get("listItems") or []


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_normalize_shopping_list_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    names = [t.name for t in tools]
    assert "normalize_shopping_list" in names


async def test_normalize_merges_same_unit_family(mcp_server, mealie_http):
    """3 tsp + 1 tbsp of the same food collapses to a single item."""
    food = await _get_or_create_food(mealie_http, "Test Garlic Normalize")
    # Use alias names ("t", "tb") so Mealie doesn't set standardUnit and
    # won't auto-merge items on insert or update.
    tsp = await _get_or_create_unit(mealie_http, "t", "")
    tbsp = await _get_or_create_unit(mealie_http, "tb", "")
    list_id = await _create_shopping_list(mealie_http, "Test Normalize Merge")

    try:
        await _add_item_bypassing_merge(mealie_http, list_id, food, tsp, 3.0)
        await _add_item_bypassing_merge(mealie_http, list_id, food, tbsp, 1.0)

        assert len(await _list_items(mealie_http, list_id)) == 2

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("normalize_shopping_list", {"list_id": list_id})

        text = result.content[0].text
        assert "MERGE" in text
        assert "1 food(s) consolidated" in text

        items = await _list_items(mealie_http, list_id)
        assert len(items) == 1
        # 3 tsp + 1 tbsp = 6 tsp total; target is tablespoon (larger, tied count)
        # = 6/3 = 2 tbsp
        assert abs(float(items[0]["quantity"]) - 2.0) < 0.01

    finally:
        await _delete_shopping_list(mealie_http, list_id)


async def test_normalize_dry_run_no_changes(mcp_server, mealie_http):
    """dry_run=True reports the merge plan but does not write anything."""
    food = await _get_or_create_food(mealie_http, "Test Onion DryRun")
    tsp = await _get_or_create_unit(mealie_http, "t", "")
    tbsp = await _get_or_create_unit(mealie_http, "tb", "")
    list_id = await _create_shopping_list(mealie_http, "Test Normalize DryRun")

    try:
        await _add_item_bypassing_merge(mealie_http, list_id, food, tsp, 6.0)
        await _add_item_bypassing_merge(mealie_http, list_id, food, tbsp, 2.0)

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "normalize_shopping_list", {"list_id": list_id, "dry_run": True}
            )

        text = result.content[0].text
        assert "DRY RUN" in text
        assert "MERGE" in text
        assert "dry run" in text.lower()

        # Items must be unchanged after a dry run.
        assert len(await _list_items(mealie_http, list_id)) == 2

    finally:
        await _delete_shopping_list(mealie_http, list_id)


async def test_normalize_skips_incompatible_families(mcp_server, mealie_http):
    """Items in different unit families for the same food are left alone."""
    food = await _get_or_create_food(mealie_http, "Test Chicken Incompat")
    # "lbs" → pound (imperial_weight), "t" → teaspoon (us_volume).
    # Both aliases give standardUnit=None so Mealie won't auto-merge them.
    lbs = await _get_or_create_unit(mealie_http, "lbs", "")   # imperial_weight
    tsp = await _get_or_create_unit(mealie_http, "t", "")     # us_volume
    list_id = await _create_shopping_list(mealie_http, "Test Normalize Incompat")

    try:
        await _add_item_bypassing_merge(mealie_http, list_id, food, lbs, 4.0)
        await _add_item_bypassing_merge(mealie_http, list_id, food, tsp, 1.0)

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("normalize_shopping_list", {"list_id": list_id})

        text = result.content[0].text
        assert "Needs manual review" in text
        assert "0 food(s) consolidated" in text

        # Both items must still be present.
        assert len(await _list_items(mealie_http, list_id)) == 2

    finally:
        await _delete_shopping_list(mealie_http, list_id)


async def test_normalize_single_item_foods_untouched(mcp_server, mealie_http):
    """Foods with only one item on the list are never touched."""
    food = await _get_or_create_food(mealie_http, "Test Carrot Untouched")
    cup = await _get_or_create_unit(mealie_http, "cup", "c")
    list_id = await _create_shopping_list(mealie_http, "Test Normalize Single")

    try:
        await _add_item(mealie_http, list_id, food, cup, 2.0)

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("normalize_shopping_list", {"list_id": list_id})

        text = result.content[0].text
        assert "0 food(s) consolidated" in text
        assert len(await _list_items(mealie_http, list_id)) == 1

    finally:
        await _delete_shopping_list(mealie_http, list_id)
