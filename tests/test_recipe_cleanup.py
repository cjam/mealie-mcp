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


def _linked_ingredient(food: dict, unit: dict, quantity: float = 1.0, ref_id: str | None = None) -> dict:
    """Build an ingredient already linked to DB food/unit objects (full objects with IDs)."""
    return {
        "referenceId": ref_id or str(uuid.uuid4()),
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


async def test_get_recipes_needing_cleanup_tool_is_registered(mcp_server):
    """get_recipes_needing_cleanup appears in the MCP tool list."""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "get_recipes_needing_cleanup" for t in tools)


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
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("cleanup_recipe", {"recipe_slug": slug})

        # Re-read after cleanup: Mealie regenerates step IDs on PUT, so the
        # pre-cleanup ID is stale.
        saved_recipe = await _get_recipe(mealie_http, slug)
        actual_step_id = saved_recipe["recipeInstructions"][0]["id"]

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


async def _list_all_slugs(client: httpx.AsyncClient) -> set[str]:
    r = await client.get("/api/recipes", params={"perPage": 500})
    r.raise_for_status()
    return {item["slug"] for item in r.json().get("items", [])}


async def test_import_and_cleanup_recipe_end_to_end(mealie_http, mcp_server):
    """import_and_cleanup_recipe imports a real recipe URL, resolves ingredients, and reports step-linking data."""
    url = "https://cookieandkate.com/chocolate-chia-pudding/"

    before = await _list_all_slugs(mealie_http)

    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("import_and_cleanup_recipe", {"url": url})

    report = result.content[0].text
    assert not report.startswith("ERROR"), f"Import failed:\n{report}"
    assert "Recipe Cleanup" in report, f"Missing cleanup section:\n{report}"
    assert "Ready for Step Linking" in report, f"Missing step-linking section:\n{report}"

    after = await _list_all_slugs(mealie_http)
    new_slugs = after - before
    assert len(new_slugs) == 1, f"Expected exactly 1 new recipe, got: {new_slugs}"
    slug = next(iter(new_slugs))

    try:
        recipe = await _get_recipe(mealie_http, slug)
        ingredients = recipe.get("recipeIngredient") or []
        assert len(ingredients) > 0, "Imported recipe has no ingredients"
        resolved = [i for i in ingredients if (i.get("food") or {}).get("id")]
        assert len(resolved) > 0, f"No ingredients resolved after cleanup: {ingredients}"
    finally:
        await _delete_recipe(mealie_http, slug)


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


# ── Tests: get_recipes_needing_cleanup ────────────────────────────────────────


async def test_get_recipes_needing_cleanup_surfaces_unresolved_ingredient(mealie_http, mcp_server):
    """A recipe with an unresolved ingredient note appears under needs_cleanup."""
    slug = await _create_recipe(mealie_http, "NeedsCleanup: Unresolved Food")
    try:
        await _set_ingredients(mealie_http, slug, [_note_ingredient("raw unresolved xyzzy_note")])

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("get_recipes_needing_cleanup", {})

        report = result.content[0].text
        assert slug in report, f"Recipe slug not in report:\n{report}"
        needs_cleanup_section = report.split("## Needs Ingredient Cleanup")[1].split("## Needs Step Linking")[0]
        assert slug in needs_cleanup_section, f"Slug should be in needs_cleanup section:\n{report}"
    finally:
        await _delete_recipe(mealie_http, slug)


async def test_get_recipes_needing_cleanup_surfaces_unlinked_steps(mealie_http, mcp_server):
    """A recipe with all food IDs resolved but zero total step references appears under needs_linking."""
    slug = await _create_recipe(mealie_http, "NeedsCleanup: Unlinked Steps")
    r = await mealie_http.post("/api/foods", json={"name": "xyzzy_linked_food_scan"})
    food = r.json()
    r = await mealie_http.post("/api/units", json={"name": "xyzzy_linked_unit_scan", "abbreviation": ""})
    unit = r.json()
    try:
        await _set_ingredients(
            mealie_http, slug,
            ingredients=[_linked_ingredient(food, unit)],
            instructions=[{"title": "", "text": "Mix everything together.", "ingredientReferences": []}],
        )

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("get_recipes_needing_cleanup", {})

        report = result.content[0].text
        needs_linking_section = (
            report.split("## Needs Step Linking")[1].split("## Incomplete Step Linking")[0]
        )
        assert slug in needs_linking_section, f"Slug should be in needs_linking section:\n{report}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, "xyzzy_linked_food_scan")
        await _purge_units(mealie_http, "xyzzy_linked_unit_scan")


async def test_get_recipes_needing_cleanup_surfaces_incomplete_linking(mealie_http, mcp_server):
    """A recipe where some but not all ingredients are referenced in steps appears under incomplete_linking."""
    slug = await _create_recipe(mealie_http, "NeedsCleanup: Incomplete Linking")
    r = await mealie_http.post("/api/foods", json={"name": "xyzzy_incomplete_food_a"})
    food_a = r.json()
    r = await mealie_http.post("/api/foods", json={"name": "xyzzy_incomplete_food_b"})
    food_b = r.json()
    r = await mealie_http.post("/api/units", json={"name": "xyzzy_incomplete_unit", "abbreviation": ""})
    unit = r.json()
    try:
        ref_a = str(uuid.uuid4())
        ref_b = str(uuid.uuid4())
        # Step references only ingredient A — ingredient B intentionally left unlinked.
        await _set_ingredients(
            mealie_http, slug,
            ingredients=[
                _linked_ingredient(food_a, unit, ref_id=ref_a),
                _linked_ingredient(food_b, unit, ref_id=ref_b),
            ],
            instructions=[{
                "title": "",
                "text": "Use food A only.",
                "ingredientReferences": [{"referenceId": ref_a}],
            }],
        )

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("get_recipes_needing_cleanup", {})

        report = result.content[0].text
        incomplete_section = report.split("## Incomplete Step Linking")[1]
        assert slug in incomplete_section, f"Slug should be in incomplete_linking section:\n{report}"
        # Must not be mis-classified as needs_linking (it has references, just not full coverage)
        needs_linking_section = (
            report.split("## Needs Step Linking")[1].split("## Incomplete Step Linking")[0]
        )
        assert slug not in needs_linking_section, (
            f"Slug should not be in needs_linking (it has some references):\n{report}"
        )
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, "xyzzy_incomplete_food_a", "xyzzy_incomplete_food_b")
        await _purge_units(mealie_http, "xyzzy_incomplete_unit")


async def test_get_recipes_needing_cleanup_skips_empty_recipes(mealie_http, mcp_server):
    """A recipe with no ingredients does not appear in any category."""
    slug = await _create_recipe(mealie_http, "NeedsCleanup: Empty Recipe")
    try:
        # Mealie injects a placeholder ingredient on creation; clear it so the
        # recipe is genuinely ingredient-free for this test.
        await _set_ingredients(mealie_http, slug, [])

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("get_recipes_needing_cleanup", {})

        report = result.content[0].text
        needs_cleanup_section = report.split("## Needs Ingredient Cleanup")[1].split("## Needs Step Linking")[0]
        needs_linking_section = report.split("## Needs Step Linking")[1].split("## Incomplete Step Linking")[0]
        incomplete_section = report.split("## Incomplete Step Linking")[1]
        assert slug not in needs_cleanup_section, f"Empty recipe should not be in needs_cleanup:\n{report}"
        assert slug not in needs_linking_section, f"Empty recipe should not be in needs_linking:\n{report}"
        assert slug not in incomplete_section, f"Empty recipe should not be in incomplete_linking:\n{report}"
    finally:
        await _delete_recipe(mealie_http, slug)


# ── Tests: fix_ingredient ─────────────────────────────────────────────────────


async def test_fix_ingredient_tool_is_registered(mcp_server):
    """fix_ingredient appears in the MCP tool list."""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "fix_ingredient" for t in tools)


async def test_fix_ingredient_replaces_food_and_patches_recipe(mealie_http, mcp_server):
    """fix_ingredient links a corrected food to a specific ingredient by referenceId."""
    food_name = "xyzzy_fix_food"
    await _purge_foods(mealie_http, food_name, food_name.title())

    # Start with a bad ingredient (raw note, no food linked)
    ref_id = str(uuid.uuid4())
    slug = await _create_recipe(mealie_http, "FixIngredient: Replace Food")
    try:
        await _set_ingredients(mealie_http, slug, [_note_ingredient("bad ingredient text", ref_id=ref_id)])

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "fix_ingredient",
                {"recipe_slug": slug, "reference_id": ref_id, "food_name": food_name},
            )

        report = result.content[0].text
        assert "error" not in report.lower(), f"Unexpected error:\n{report}"
        assert "patch ok" in report.lower(), f"Expected PATCH OK:\n{report}"

        recipe = await _get_recipe(mealie_http, slug)
        ing = next(i for i in recipe["recipeIngredient"] if i.get("referenceId") == ref_id)
        assert (ing.get("food") or {}).get("id"), f"Food ID still null after fix: {ing}"
        assert (ing.get("food") or {})["name"].lower() == food_name.lower()
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, food_name, food_name.title())


async def test_fix_ingredient_creates_missing_food(mealie_http, mcp_server):
    """fix_ingredient creates the food in the DB when it doesn't exist yet."""
    food_name = "xyzzy_fix_new_food"
    await _purge_foods(mealie_http, food_name, food_name.title())

    ref_id = str(uuid.uuid4())
    slug = await _create_recipe(mealie_http, "FixIngredient: Create Food")
    try:
        await _set_ingredients(mealie_http, slug, [_note_ingredient("whatever", ref_id=ref_id)])

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "fix_ingredient",
                {"recipe_slug": slug, "reference_id": ref_id, "food_name": food_name},
            )

        report = result.content[0].text
        assert "created" in report.lower(), f"Expected 'created' in:\n{report}"

        ids = await _food_ids_by_name(mealie_http, food_name)
        assert len(ids) == 1, f"Expected exactly 1 food, got {len(ids)}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, food_name, food_name.title())


async def test_fix_ingredient_sets_unit_and_quantity(mealie_http, mcp_server):
    """fix_ingredient applies optional unit and quantity overrides."""
    food_name = "xyzzy_fix_food_with_unit"
    await _purge_foods(mealie_http, food_name, food_name.title())
    await _purge_units(mealie_http, "tablespoon")

    ref_id = str(uuid.uuid4())
    slug = await _create_recipe(mealie_http, "FixIngredient: Unit + Quantity")
    try:
        await _set_ingredients(mealie_http, slug, [_note_ingredient("raw text", ref_id=ref_id)])

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "fix_ingredient",
                {
                    "recipe_slug": slug,
                    "reference_id": ref_id,
                    "food_name": food_name,
                    "unit_name": "tablespoon",
                    "quantity": 2.0,
                },
            )

        report = result.content[0].text
        assert "patch ok" in report.lower(), f"Expected PATCH OK:\n{report}"

        recipe = await _get_recipe(mealie_http, slug)
        ing = next(i for i in recipe["recipeIngredient"] if i.get("referenceId") == ref_id)
        assert (ing.get("unit") or {}).get("id"), f"Unit ID still null: {ing}"
        assert ing.get("quantity") == 2.0, f"Quantity not updated: {ing}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, food_name, food_name.title())
        await _purge_units(mealie_http, "tablespoon")


async def test_fix_ingredient_reuses_existing_food(mealie_http, mcp_server):
    """fix_ingredient links to an existing food without creating a duplicate."""
    food_name = "xyzzy_fix_existing_food"
    await _purge_foods(mealie_http, food_name, food_name.title())
    r = await mealie_http.post("/api/foods", json={"name": food_name})
    existing_id = r.json()["id"]

    ref_id = str(uuid.uuid4())
    slug = await _create_recipe(mealie_http, "FixIngredient: Reuse Food")
    try:
        await _set_ingredients(mealie_http, slug, [_note_ingredient("old name", ref_id=ref_id)])

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "fix_ingredient",
                {"recipe_slug": slug, "reference_id": ref_id, "food_name": food_name},
            )

        report = result.content[0].text
        assert "found" in report.lower(), f"Expected 'found' in:\n{report}"

        ids = await _food_ids_by_name(mealie_http, food_name)
        assert len(ids) == 1, f"Duplicate food created: {ids}"
        assert ids[0] == existing_id
    finally:
        await _delete_recipe(mealie_http, slug)
        await _purge_foods(mealie_http, food_name, food_name.title())


async def test_fix_ingredient_returns_error_for_bad_reference_id(mealie_http, mcp_server):
    """fix_ingredient returns a readable error when referenceId doesn't exist."""
    slug = await _create_recipe(mealie_http, "FixIngredient: Bad RefId")
    try:
        await _set_ingredients(mealie_http, slug, [_note_ingredient("some ingredient")])

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "fix_ingredient",
                {"recipe_slug": slug, "reference_id": str(uuid.uuid4()), "food_name": "anything"},
            )

        report = result.content[0].text
        assert "error" in report.lower(), f"Expected error for bad referenceId:\n{report}"
    finally:
        await _delete_recipe(mealie_http, slug)


async def test_fix_ingredient_returns_error_for_unknown_recipe(mcp_server):
    """fix_ingredient returns a readable error for a non-existent recipe slug."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "fix_ingredient",
            {"recipe_slug": "does-not-exist-xyz", "reference_id": str(uuid.uuid4()), "food_name": "anything"},
        )
    report = result.content[0].text  # type: ignore[union-attr]
    assert "error" in report.lower(), f"Expected error for unknown recipe:\n{report}"
