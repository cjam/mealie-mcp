"""Integration tests for cleanup_recipe and import_and_cleanup_recipe tools."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastmcp import Client

from server import build_client


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def mealie_http(mealie_settings):
    async with build_client(mealie_settings) as client:
        yield client


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_recipe(client: httpx.AsyncClient, slug: str) -> dict:
    r = await client.get(f"/api/recipes/{slug}")
    r.raise_for_status()
    return r.json()


async def _create_recipe(client: httpx.AsyncClient, name: str) -> str:
    """Create a minimal recipe and return its slug."""
    r = await client.post("/api/recipes", json={"name": name})
    r.raise_for_status()
    return r.json()  # Mealie returns the slug as a bare JSON string


async def _delete_recipe(client: httpx.AsyncClient, slug: str) -> None:
    await client.delete(f"/api/recipes/{slug}")


async def _set_ingredients(
    client: httpx.AsyncClient,
    slug: str,
    ingredients: list[dict],
    instructions: list[dict] | None = None,
) -> None:
    recipe = await _get_recipe(client, slug)
    recipe["recipeIngredient"] = ingredients
    if instructions is not None:
        recipe["recipeInstructions"] = instructions
    r = await client.put(f"/api/recipes/{slug}", json=recipe)
    r.raise_for_status()


def _note_ingredient(note_text: str, ref_id: str | None = None) -> dict:
    """Build an ingredient as Mealie's URL import produces: raw text in note, null food/unit."""
    return {
        "referenceId": ref_id or str(uuid.uuid4()),
        "food": None,
        "unit": None,
        "quantity": 0.0,
        "note": note_text,
        "display": note_text,
        "title": None,
        "originalText": note_text,
    }


def _linked_ingredient(food: dict, unit: dict, quantity: float = 1.0) -> dict:
    """Build an ingredient already linked to DB food/unit objects (full objects with IDs)."""
    return {
        "referenceId": str(uuid.uuid4()),
        "food": food,
        "unit": unit,
        "quantity": quantity,
        "note": "",
        "display": f"{quantity} {unit.get('name','')} {food.get('name','')}".strip(),
        "title": None,
        "originalText": None,
    }


async def _food_ids_by_name(client: httpx.AsyncClient, name: str) -> list[str]:
    r = await client.get("/api/foods", params={"perPage": 200})
    r.raise_for_status()
    return [f["id"] for f in r.json().get("items", []) if f["name"].lower() == name.lower()]


async def _unit_ids_by_name(client: httpx.AsyncClient, name: str) -> list[str]:
    r = await client.get("/api/units", params={"perPage": 200})
    r.raise_for_status()
    return [u["id"] for u in r.json().get("items", []) if u["name"].lower() == name.lower()]


async def _purge_foods(client: httpx.AsyncClient, *names: str) -> None:
    for name in names:
        for fid in await _food_ids_by_name(client, name):
            await client.delete(f"/api/foods/{fid}")


async def _purge_units(client: httpx.AsyncClient, *names: str) -> None:
    for name in names:
        for uid in await _unit_ids_by_name(client, name):
            await client.delete(f"/api/units/{uid}")


# ── Tests: registration ────────────────────────────────────────────────────────


async def test_cleanup_recipe_tool_is_registered(mcp_server):
    """cleanup_recipe appears in the MCP tool list."""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "cleanup_recipe" for t in tools)


async def test_import_and_cleanup_recipe_tool_is_registered(mcp_server):
    """import_and_cleanup_recipe appears in the MCP tool list."""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "import_and_cleanup_recipe" for t in tools)


# ── Tests: food/unit resolution ────────────────────────────────────────────────


async def test_cleanup_recipe_creates_missing_food_and_links_it(mealie_http, mcp_server):
    """An ingredient note gets parsed; the food is created and linked to the ingredient."""
    # Use a unique name unlikely to be parsed as anything other than a food
    food_note = "xyzzy_cleanup_test_food"
    await _purge_foods(mealie_http, food_note)
    slug = await _create_recipe(mealie_http, "Cleanup Test: Create Food")
    try:
        await _set_ingredients(mealie_http, slug, [_note_ingredient(food_note)])

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("cleanup_recipe", {"recipe_slug": slug})

        report = result.content[0].text
        assert "created" in report.lower(), f"Expected 'created' in:\n{report}"

        # Ingredient has food linked after cleanup
        recipe = await _get_recipe(mealie_http, slug)
        ing = recipe["recipeIngredient"][0]
        food_obj = ing.get("food") or {}
        assert food_obj.get("id"), f"Ingredient food.id still null after cleanup: {ing}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, food_note, food_note.title())


async def test_cleanup_recipe_creates_missing_unit_and_links_it(mealie_http, mcp_server):
    """An ingredient note containing a quantity and unit gets both created/linked."""
    # Use a well-known unit phrase so the NLP parser reliably extracts it
    food_name = "xyzzy_cleanup_test_food2"
    await _purge_foods(mealie_http, food_name, food_name.title())
    await _purge_units(mealie_http, "teaspoon")
    slug = await _create_recipe(mealie_http, "Cleanup Test: Create Unit")
    try:
        await _set_ingredients(
            mealie_http, slug,
            [_note_ingredient(f"1 teaspoon {food_name}")],
        )

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("cleanup_recipe", {"recipe_slug": slug})

        report = result.content[0].text
        assert "created" in report.lower()

        recipe = await _get_recipe(mealie_http, slug)
        ing = recipe["recipeIngredient"][0]
        unit_obj = ing.get("unit") or {}
        assert unit_obj.get("id"), f"Ingredient unit.id still null after cleanup: {ing}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, food_name, food_name.title())
        await _purge_units(mealie_http, "teaspoon")


async def test_cleanup_recipe_links_to_existing_food_without_duplicate(mealie_http, mcp_server):
    """cleanup_recipe links to a pre-existing food and does not create a duplicate."""
    # Use a single-word name so the NLP parser reliably identifies it as a food
    food_name = "xyzzygarlic"
    await _purge_foods(mealie_http, food_name, food_name.title())
    r = await mealie_http.post("/api/foods", json={"name": food_name})
    existing_id = r.json()["id"]

    slug = await _create_recipe(mealie_http, "Cleanup Test: Reuse Food")
    try:
        await _set_ingredients(mealie_http, slug, [_note_ingredient(food_name)])

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("cleanup_recipe", {"recipe_slug": slug})

        report = result.content[0].text
        assert "found" in report.lower(), f"Expected 'found' in:\n{report}"

        ids = await _food_ids_by_name(mealie_http, food_name)
        assert len(ids) == 1, f"Expected 1 food entry, got {len(ids)}: {ids}"
        assert ids[0] == existing_id, "Cleanup should link to existing food, not create a new one"

        recipe = await _get_recipe(mealie_http, slug)
        ing = recipe["recipeIngredient"][0]
        assert (ing.get("food") or {}).get("id") == existing_id
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, food_name, food_name.title())


async def test_cleanup_recipe_links_to_existing_unit_without_duplicate(mealie_http, mcp_server):
    """cleanup_recipe links to a pre-existing unit and does not create a duplicate."""
    food_name = "xyzzy_unit_reuse_food"
    await _purge_foods(mealie_http, food_name, food_name.title())
    await _purge_units(mealie_http, "cup")
    r = await mealie_http.post("/api/units", json={"name": "cup", "abbreviation": "c", "pluralName": "cups", "fraction": True})
    existing_id = r.json()["id"]

    slug = await _create_recipe(mealie_http, "Cleanup Test: Reuse Unit")
    try:
        await _set_ingredients(
            mealie_http, slug,
            [_note_ingredient(f"1 cup {food_name}")],
        )

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("cleanup_recipe", {"recipe_slug": slug})

        ids = await _unit_ids_by_name(mealie_http, "cup")
        assert len(ids) == 1, f"Expected 1 cup unit, got {len(ids)}: {ids}"
        assert ids[0] == existing_id, "Cleanup should link to existing unit, not create a new one"

        recipe = await _get_recipe(mealie_http, slug)
        ing = recipe["recipeIngredient"][0]
        assert (ing.get("unit") or {}).get("id") == existing_id
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, food_name, food_name.title())
        await _purge_units(mealie_http, "cup")


# ── Tests: step-linking section ────────────────────────────────────────────────


async def test_cleanup_recipe_output_contains_step_linking_section(mealie_http, mcp_server):
    """Output lists ingredient referenceIds and step IDs so AI can map them."""
    ref_id = str(uuid.uuid4())
    slug = await _create_recipe(mealie_http, "Cleanup Test: Step Linking")
    try:
        await _set_ingredients(
            mealie_http, slug,
            ingredients=[_note_ingredient("2 tablespoons olive oil", ref_id=ref_id)],
            instructions=[{
                "title": "",
                "text": "Heat the olive oil in a pan over medium heat.",
                "ingredientReferences": [],
            }],
        )
        # Mealie assigns its own step IDs — read the actual one back
        saved_recipe = await _get_recipe(mealie_http, slug)
        actual_step_id = saved_recipe["recipeInstructions"][0]["id"]

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("cleanup_recipe", {"recipe_slug": slug})

        report = result.content[0].text
        assert "Ready for Step Linking" in report, f"Missing step-linking section:\n{report}"
        assert ref_id in report, f"Ingredient referenceId not in report:\n{report}"
        assert actual_step_id in report, f"Step ID not in report:\n{report}"
        assert "Heat the olive oil" in report, f"Step text not in report:\n{report}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, "olive oil", "Olive Oil")
        await _purge_units(mealie_http, "tablespoon")


async def test_cleanup_recipe_already_linked_ingredients_not_reported_as_changes(
    mealie_http, mcp_server
):
    """Ingredients that already have food/unit IDs are skipped silently."""
    slug = await _create_recipe(mealie_http, "Cleanup Test: Already Linked")
    r = await mealie_http.post("/api/foods", json={"name": "already_linked_food_test"})
    food = r.json()
    r = await mealie_http.post("/api/units", json={"name": "already_linked_unit_test", "abbreviation": ""})
    unit = r.json()
    try:
        await _set_ingredients(
            mealie_http, slug,
            [_linked_ingredient(food, unit)],
        )

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("cleanup_recipe", {"recipe_slug": slug})

        report = result.content[0].text
        assert "created" not in report.lower(), f"Should not create already-linked items:\n{report}"
        assert "no changes needed" in report.lower() or "already linked" in report.lower(), (
            f"Expected 'no changes' message:\n{report}"
        )
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, "already_linked_food_test")
        await _purge_units(mealie_http, "already_linked_unit_test")


# ── Tests: idempotency ─────────────────────────────────────────────────────────


async def test_cleanup_recipe_is_idempotent(mealie_http, mcp_server):
    """Running cleanup_recipe twice creates no additional DB entries."""
    food_note = "xyzzy_idempotent_food"
    await _purge_foods(mealie_http, food_note, food_note.title())
    slug = await _create_recipe(mealie_http, "Cleanup Test: Idempotent")
    try:
        await _set_ingredients(mealie_http, slug, [_note_ingredient(food_note)])

        async with Client(mcp_server) as mcp:
            await mcp.call_tool("cleanup_recipe", {"recipe_slug": slug})
            result2 = await mcp.call_tool("cleanup_recipe", {"recipe_slug": slug})

        report2 = result2.content[0].text
        assert "created" not in report2.lower(), f"Second run should not create entries:\n{report2}"
        assert "no changes needed" in report2.lower(), f"Second run should be a no-op:\n{report2}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, food_note, food_note.title())


# ── Tests: error handling ─────────────────────────────────────────────────────


async def test_cleanup_recipe_returns_error_for_unknown_slug(mcp_server):
    """cleanup_recipe returns a readable error string for a non-existent recipe."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("cleanup_recipe", {"recipe_slug": "this-recipe-does-not-exist-xyz"})
    report = result.content[0].text
    assert "error" in report.lower(), f"Expected error message, got:\n{report}"


async def test_import_and_cleanup_returns_error_for_invalid_url(mcp_server):
    """import_and_cleanup_recipe returns a readable error for an unreachable URL."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "import_and_cleanup_recipe",
            {"url": "https://not-a-real-recipe-site-at-all.invalid/recipe/123"},
        )
    report = result.content[0].text
    assert "error" in report.lower() or "failed" in report.lower(), (
        f"Expected error message for invalid URL, got:\n{report}"
    )
