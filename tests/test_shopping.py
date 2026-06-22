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


async def _create_recipe(client, name: str) -> dict:
    r = await client.post("/api/recipes", json={"name": name})
    r.raise_for_status()
    slug = r.json()
    r2 = await client.get(f"/api/recipes/{slug}")
    r2.raise_for_status()
    return r2.json()


async def _delete_recipe(client, slug: str) -> None:
    await client.delete(f"/api/recipes/{slug}")


async def test_replace_shopping_list_accepts_slug(mcp_server, mealie_http):
    """replace_shopping_list_from_recipes resolves slugs to UUIDs without error."""
    recipe = await _create_recipe(mealie_http, "Test Slug Resolution Recipe")
    list_id = await _create_shopping_list(mealie_http, "Test Slug List")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "replace_shopping_list_from_recipes",
                {"list_id": list_id, "recipe_ids": [recipe["slug"]]},
            )
        text = result.content[0].text
        assert "could not resolve recipe" not in text
        assert "Added 1/1" in text
    finally:
        await _delete_recipe(mealie_http, recipe["slug"])
        await _delete_shopping_list(mealie_http, list_id)


async def test_replace_shopping_list_warns_on_missing_slug(mcp_server, mealie_http):
    """replace_shopping_list_from_recipes warns and skips an unresolvable slug."""
    list_id = await _create_shopping_list(mealie_http, "Test Bad Slug List")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "replace_shopping_list_from_recipes",
                {"list_id": list_id, "recipe_ids": ["no-such-recipe-slug"]},
            )
        text = result.content[0].text
        assert "could not resolve recipe" in text
        assert "Added 0/1" in text
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


# ── get_shopping_list_items tests ─────────────────────────────────────────────

async def test_get_shopping_list_items_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert "get_shopping_list_items" in [t.name for t in tools]


async def test_get_shopping_list_items_compact_table(mcp_server, mealie_http):
    """Returns a compact line per item — id, food, qty, unit."""
    food = await _get_or_create_food(mealie_http, "Test Broccoli Items")
    cup = await _get_or_create_unit(mealie_http, "cup", "c")
    list_id = await _create_shopping_list(mealie_http, "Test Items Table")
    try:
        await _add_item(mealie_http, list_id, food, cup, 2.0)
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("get_shopping_list_items", {"list_id": list_id})
        text = result.content[0].text
        assert "Test Broccoli Items" in text
        assert "1 item" in text
    finally:
        await _delete_shopping_list(mealie_http, list_id)


async def test_get_shopping_list_items_empty(mcp_server, mealie_http):
    """Empty list reports zero items without error."""
    list_id = await _create_shopping_list(mealie_http, "Test Items Empty")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("get_shopping_list_items", {"list_id": list_id})
        text = result.content[0].text
        assert "0 item" in text or "(empty)" in text
    finally:
        await _delete_shopping_list(mealie_http, list_id)


# ── adjust_shopping_list_items tests ─────────────────────────────────────────

async def test_adjust_shopping_list_items_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert "adjust_shopping_list_items" in [t.name for t in tools]


async def test_adjust_adds_item_by_food_name(mcp_server, mealie_http):
    """add entry creates a new shopping list item resolved from food name."""
    list_id = await _create_shopping_list(mealie_http, "Test Adjust Add")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "adjust_shopping_list_items",
                {
                    "list_id": list_id,
                    "add": [{"food": "Test Spinach Adjust", "quantity": 1.5, "unit": "lb"}],
                },
            )
        text = result.content[0].text
        assert "ADDED" in text
        items = await _list_items(mealie_http, list_id)
        assert len(items) == 1
        assert abs(float(items[0]["quantity"]) - 1.5) < 0.01
    finally:
        await _delete_shopping_list(mealie_http, list_id)


async def test_adjust_updates_item_by_food_name(mcp_server, mealie_http):
    """update entry changes quantity on an existing item identified by food name."""
    food = await _get_or_create_food(mealie_http, "Test Pepper Adjust")
    lb = await _get_or_create_unit(mealie_http, "pound", "lb")
    list_id = await _create_shopping_list(mealie_http, "Test Adjust Update")
    try:
        await _add_item(mealie_http, list_id, food, lb, 1.0)
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "adjust_shopping_list_items",
                {
                    "list_id": list_id,
                    "update": [{"food": "Test Pepper Adjust", "quantity": 2.5}],
                },
            )
        text = result.content[0].text
        assert "UPDATED" in text
        items = await _list_items(mealie_http, list_id)
        assert len(items) == 1
        assert abs(float(items[0]["quantity"]) - 2.5) < 0.01
    finally:
        await _delete_shopping_list(mealie_http, list_id)


async def test_adjust_removes_item_by_food_name(mcp_server, mealie_http):
    """remove entry deletes all unchecked items matching the food name."""
    food = await _get_or_create_food(mealie_http, "Test Tomato Adjust")
    cup = await _get_or_create_unit(mealie_http, "cup", "c")
    list_id = await _create_shopping_list(mealie_http, "Test Adjust Remove")
    try:
        await _add_item(mealie_http, list_id, food, cup, 3.0)
        assert len(await _list_items(mealie_http, list_id)) == 1

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "adjust_shopping_list_items",
                {"list_id": list_id, "remove": ["Test Tomato Adjust"]},
            )
        text = result.content[0].text
        assert "REMOVE" in text
        assert len(await _list_items(mealie_http, list_id)) == 0
    finally:
        await _delete_shopping_list(mealie_http, list_id)


async def test_adjust_mixed_operations(mcp_server, mealie_http):
    """add + update + remove all succeed in one call."""
    food_keep = await _get_or_create_food(mealie_http, "Test Zucchini Adjust")
    food_drop = await _get_or_create_food(mealie_http, "Test Eggplant Adjust")
    cup = await _get_or_create_unit(mealie_http, "cup", "c")
    list_id = await _create_shopping_list(mealie_http, "Test Adjust Mixed")
    try:
        await _add_item(mealie_http, list_id, food_keep, cup, 1.0)
        await _add_item(mealie_http, list_id, food_drop, cup, 2.0)

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "adjust_shopping_list_items",
                {
                    "list_id": list_id,
                    "add": [{"food": "Test Kale Adjust", "quantity": 1.0}],
                    "update": [{"food": "Test Zucchini Adjust", "quantity": 3.0}],
                    "remove": ["Test Eggplant Adjust"],
                },
            )
        text = result.content[0].text
        assert "ADDED" in text
        assert "UPDATED" in text
        assert "REMOVE" in text

        items = await _list_items(mealie_http, list_id)
        food_names = {(i.get("food") or {}).get("name", "") for i in items}
        assert "Test Eggplant Adjust" not in food_names
        keep_item = next(i for i in items if (i.get("food") or {}).get("name") == "Test Zucchini Adjust")
        assert abs(float(keep_item["quantity"]) - 3.0) < 0.01
    finally:
        await _delete_shopping_list(mealie_http, list_id)


# ── optional ingredient filtering tests ──────────────────────────────────────

async def _create_recipe_with_ingredients(
    client: httpx.AsyncClient,
    name: str,
    ingredients: list[dict],
) -> dict:
    """Create a recipe, set ingredients via PUT, and return the recipe dict."""
    r = await client.post("/api/recipes", json={"name": name})
    r.raise_for_status()
    slug = r.json()
    r2 = await client.get(f"/api/recipes/{slug}")
    r2.raise_for_status()
    recipe = r2.json()
    recipe["recipeIngredient"] = ingredients
    r3 = await client.put(f"/api/recipes/{slug}", json=recipe)
    r3.raise_for_status()
    # Re-fetch to get authoritative state including IDs
    r4 = await client.get(f"/api/recipes/{slug}")
    r4.raise_for_status()
    return r4.json()


async def test_replace_shopping_list_skips_optional_by_note(mcp_server, mealie_http):
    """Ingredients with 'optional' in the note are excluded by default."""
    food_req = await _get_or_create_food(mealie_http, "Test Required Chicken")
    food_opt = await _get_or_create_food(mealie_http, "Test Optional Shrimp")

    import uuid as _uuid
    ingredients = [
        {
            "referenceId": str(_uuid.uuid4()),
            "food": food_req,
            "foodId": food_req["id"],
            "quantity": 1.0,
            "note": "",
            "title": "",
        },
        {
            "referenceId": str(_uuid.uuid4()),
            "food": food_opt,
            "foodId": food_opt["id"],
            "quantity": 1.0,
            "note": "optional — or use tofu",
            "title": "",
        },
    ]
    recipe = await _create_recipe_with_ingredients(
        mealie_http, "Test Optional Note Recipe", ingredients
    )
    list_id = await _create_shopping_list(mealie_http, "Test Optional Note List")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "replace_shopping_list_from_recipes",
                {"list_id": list_id, "recipe_ids": [recipe["slug"]]},
            )
        text = result.content[0].text
        assert "optional" in text.lower()

        items = await _list_items(mealie_http, list_id)
        food_ids = {(i.get("food") or {}).get("id") for i in items}
        assert food_req["id"] in food_ids, "required ingredient should remain"
        assert food_opt["id"] not in food_ids, "optional ingredient should be removed"
    finally:
        await _delete_recipe(mealie_http, recipe["slug"])
        await _delete_shopping_list(mealie_http, list_id)


async def test_replace_shopping_list_include_optional_flag(mcp_server, mealie_http):
    """include_optional=True keeps all ingredients including optional ones."""
    food_req = await _get_or_create_food(mealie_http, "Test Req Beef Opt Flag")
    food_opt = await _get_or_create_food(mealie_http, "Test Opt Bacon Opt Flag")

    import uuid as _uuid
    ingredients = [
        {
            "referenceId": str(_uuid.uuid4()),
            "food": food_req,
            "foodId": food_req["id"],
            "quantity": 1.0,
            "note": "",
            "title": "",
        },
        {
            "referenceId": str(_uuid.uuid4()),
            "food": food_opt,
            "foodId": food_opt["id"],
            "quantity": 1.0,
            "note": "optional",
            "title": "",
        },
    ]
    recipe = await _create_recipe_with_ingredients(
        mealie_http, "Test Include Optional Recipe", ingredients
    )
    list_id = await _create_shopping_list(mealie_http, "Test Include Optional List")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "replace_shopping_list_from_recipes",
                {
                    "list_id": list_id,
                    "recipe_ids": [recipe["slug"]],
                    "include_optional": True,
                },
            )
        items = await _list_items(mealie_http, list_id)
        food_ids = {(i.get("food") or {}).get("id") for i in items}
        assert food_opt["id"] in food_ids, "optional ingredient kept when include_optional=True"
    finally:
        await _delete_recipe(mealie_http, recipe["slug"])
        await _delete_shopping_list(mealie_http, list_id)
