"""Integration tests for get_ingredient_normalization_report and normalize_ingredients."""

from __future__ import annotations

import uuid

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

async def _create_food(client: httpx.AsyncClient, name: str) -> dict:
    r = await client.post("/api/foods", json={"name": name})
    r.raise_for_status()
    return r.json()


async def _create_unit(client: httpx.AsyncClient, name: str, abbreviation: str = "") -> dict:
    r = await client.post("/api/units", json={"name": name, "abbreviation": abbreviation})
    r.raise_for_status()
    return r.json()


async def _create_recipe(client: httpx.AsyncClient, name: str) -> str:
    r = await client.post("/api/recipes", json={"name": name})
    r.raise_for_status()
    return r.json()  # slug string


async def _add_ingredient(
    client: httpx.AsyncClient,
    recipe_slug: str,
    food: dict,
    unit: dict | None = None,
    quantity: float = 1.0,
    note: str = "",
) -> None:
    r = await client.get(f"/api/recipes/{recipe_slug}")
    r.raise_for_status()
    recipe = r.json()
    ing: dict = {
        "referenceId": str(uuid.uuid4()),
        "quantity": quantity,
        "food": food,
        "note": note,
    }
    if unit:
        ing["unit"] = unit
    recipe.setdefault("recipeIngredient", []).append(ing)
    r = await client.put(f"/api/recipes/{recipe_slug}", json=recipe)
    r.raise_for_status()


async def _get_recipe(client: httpx.AsyncClient, slug: str) -> dict:
    r = await client.get(f"/api/recipes/{slug}")
    r.raise_for_status()
    return r.json()


async def _delete_food(client: httpx.AsyncClient, food_id: str) -> None:
    await client.delete(f"/api/foods/{food_id}")


async def _delete_unit(client: httpx.AsyncClient, unit_id: str) -> None:
    await client.delete(f"/api/units/{unit_id}")


async def _delete_recipe(client: httpx.AsyncClient, slug: str) -> None:
    await client.delete(f"/api/recipes/{slug}")


# ── Registration ──────────────────────────────────────────────────────────────

async def test_get_ingredient_normalization_report_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "get_ingredient_normalization_report" for t in tools)


async def test_normalize_ingredients_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "normalize_ingredients" for t in tools)


# ── get_ingredient_normalization_report ──────────────────────────────────────────

async def test_report_detects_multi_unit_food(mealie_http, mcp_server):
    """Report lists foods that appear with 2+ distinct units."""
    food = await _create_food(mealie_http, "TestGarlicReport")
    clove = await _create_unit(mealie_http, "TestCloveReport")
    tsp = await _create_unit(mealie_http, "TestTspReport")
    slug1 = await _create_recipe(mealie_http, "GarlicReportRecipe1")
    slug2 = await _create_recipe(mealie_http, "GarlicReportRecipe2")
    try:
        await _add_ingredient(mealie_http, slug1, food, clove, quantity=2.0)
        await _add_ingredient(mealie_http, slug2, food, tsp, quantity=0.5, note="minced")

        async with Client(mcp_server) as client:
            result = await client.call_tool("get_ingredient_normalization_report", {})

        report = result.content[0].text
        assert "TestGarlicReport" in report
        assert food["id"] in report
        assert "TestCloveReport" in report
        assert "TestTspReport" in report
        assert clove["id"] in report
        assert tsp["id"] in report
    finally:
        await _delete_recipe(mealie_http, slug1)
        await _delete_recipe(mealie_http, slug2)
        await _delete_food(mealie_http, food["id"])
        await _delete_unit(mealie_http, clove["id"])
        await _delete_unit(mealie_http, tsp["id"])


async def test_report_includes_reference_ids(mealie_http, mcp_server):
    """Each ingredient line in the report contains its referenceId."""
    food = await _create_food(mealie_http, "TestGarlicRef")
    clove = await _create_unit(mealie_http, "TestCloveRef")
    tsp = await _create_unit(mealie_http, "TestTspRef")
    slug1 = await _create_recipe(mealie_http, "GarlicRefRecipe1")
    slug2 = await _create_recipe(mealie_http, "GarlicRefRecipe2")
    try:
        await _add_ingredient(mealie_http, slug1, food, clove, quantity=1.0)
        await _add_ingredient(mealie_http, slug2, food, tsp, quantity=2.0)

        async with Client(mcp_server) as client:
            result = await client.call_tool("get_ingredient_normalization_report", {})

        report = result.content[0].text
        # Each ingredient line should have "ref: <uuid>" in it
        assert "ref:" in report
    finally:
        await _delete_recipe(mealie_http, slug1)
        await _delete_recipe(mealie_http, slug2)
        await _delete_food(mealie_http, food["id"])
        await _delete_unit(mealie_http, clove["id"])
        await _delete_unit(mealie_http, tsp["id"])


async def test_report_includes_normalize_instructions(mealie_http, mcp_server):
    """Report ends with instructions for calling normalize_ingredients."""
    food = await _create_food(mealie_http, "TestGarlicInstr")
    clove = await _create_unit(mealie_http, "TestCloveInstr")
    tsp = await _create_unit(mealie_http, "TestTspInstr")
    slug1 = await _create_recipe(mealie_http, "GarlicInstrRecipe1")
    slug2 = await _create_recipe(mealie_http, "GarlicInstrRecipe2")
    try:
        await _add_ingredient(mealie_http, slug1, food, clove, quantity=1.0)
        await _add_ingredient(mealie_http, slug2, food, tsp, quantity=2.0)

        async with Client(mcp_server) as client:
            result = await client.call_tool("get_ingredient_normalization_report", {})

        report = result.content[0].text
        assert "normalize_ingredients" in report
        assert "target_unit_id" in report
        assert "conversions" in report
        assert "factor" in report
    finally:
        await _delete_recipe(mealie_http, slug1)
        await _delete_recipe(mealie_http, slug2)
        await _delete_food(mealie_http, food["id"])
        await _delete_unit(mealie_http, clove["id"])
        await _delete_unit(mealie_http, tsp["id"])


async def test_report_skips_single_unit_foods(mealie_http, mcp_server):
    """Foods used with only one distinct unit do not appear in the report."""
    food = await _create_food(mealie_http, "TestOnionSingleUnit")
    unit = await _create_unit(mealie_http, "TestCloveSingle")
    slug = await _create_recipe(mealie_http, "OnionSingleUnitRecipe")
    try:
        await _add_ingredient(mealie_http, slug, food, unit, quantity=1.0)

        async with Client(mcp_server) as client:
            result = await client.call_tool("get_ingredient_normalization_report", {})

        assert "TestOnionSingleUnit" not in result.content[0].text
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_food(mealie_http, food["id"])
        await _delete_unit(mealie_http, unit["id"])


# ── normalize_ingredients ─────────────────────────────────────────────────────

async def _get_reference_id(client: httpx.AsyncClient, recipe_slug: str, food_id: str) -> str:
    """Fetch a recipe and return the referenceId of the ingredient matching food_id."""
    recipe = await _get_recipe(client, recipe_slug)
    for ing in recipe.get("recipeIngredient") or []:
        if (ing.get("food") or {}).get("id") == food_id:
            return ing["referenceId"]
    raise AssertionError(f"No ingredient with food_id={food_id} in recipe {recipe_slug}")


async def test_normalize_changes_unit(mealie_http, mcp_server):
    """normalize_ingredients sets the target unit on the specified ingredient."""
    food = await _create_food(mealie_http, "TestGarlicNorm")
    clove = await _create_unit(mealie_http, "TestCloveNorm")
    tsp = await _create_unit(mealie_http, "TestTspNorm")
    slug = await _create_recipe(mealie_http, "GarlicNormRecipe")
    try:
        await _add_ingredient(mealie_http, slug, food, tsp, quantity=2.0)
        ref_id = await _get_reference_id(mealie_http, slug, food["id"])

        async with Client(mcp_server) as client:
            result = await client.call_tool("normalize_ingredients", {
                "food_id": food["id"],
                "target_unit_id": clove["id"],
                "conversions": [
                    {"recipe_slug": slug, "reference_id": ref_id, "factor": 1.0},
                ],
            })

        assert "ERROR" not in result.content[0].text
        recipe = await _get_recipe(mealie_http, slug)
        ing = next(i for i in recipe.get("recipeIngredient") or [] if i.get("referenceId") == ref_id)
        assert (ing.get("unit") or {}).get("id") == clove["id"]
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_food(mealie_http, food["id"])
        await _delete_unit(mealie_http, clove["id"])
        await _delete_unit(mealie_http, tsp["id"])


async def test_normalize_scales_quantity_by_factor(mealie_http, mcp_server):
    """normalize_ingredients multiplies the ingredient quantity by factor."""
    food = await _create_food(mealie_http, "TestGarlicScale")
    clove = await _create_unit(mealie_http, "TestCloveScale")
    tsp = await _create_unit(mealie_http, "TestTspScale")
    slug = await _create_recipe(mealie_http, "GarlicScaleRecipe")
    try:
        await _add_ingredient(mealie_http, slug, food, tsp, quantity=4.0)
        ref_id = await _get_reference_id(mealie_http, slug, food["id"])

        async with Client(mcp_server) as client:
            await client.call_tool("normalize_ingredients", {
                "food_id": food["id"],
                "target_unit_id": clove["id"],
                "conversions": [
                    {"recipe_slug": slug, "reference_id": ref_id, "factor": 0.5},
                ],
            })

        recipe = await _get_recipe(mealie_http, slug)
        ing = next(i for i in recipe.get("recipeIngredient") or [] if i.get("referenceId") == ref_id)
        assert ing.get("quantity") == pytest.approx(2.0)  # 4.0 × 0.5
        assert (ing.get("unit") or {}).get("id") == clove["id"]
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_food(mealie_http, food["id"])
        await _delete_unit(mealie_http, clove["id"])
        await _delete_unit(mealie_http, tsp["id"])


async def test_normalize_factor_1_leaves_quantity_unchanged(mealie_http, mcp_server):
    """factor=1.0 changes the unit but leaves the quantity untouched."""
    food = await _create_food(mealie_http, "TestGarlicFactor1")
    clove = await _create_unit(mealie_http, "TestCloveF1")
    tsp = await _create_unit(mealie_http, "TestTspF1")
    slug = await _create_recipe(mealie_http, "GarlicFactor1Recipe")
    try:
        await _add_ingredient(mealie_http, slug, food, tsp, quantity=3.0)
        ref_id = await _get_reference_id(mealie_http, slug, food["id"])

        async with Client(mcp_server) as client:
            await client.call_tool("normalize_ingredients", {
                "food_id": food["id"],
                "target_unit_id": clove["id"],
                "conversions": [
                    {"recipe_slug": slug, "reference_id": ref_id, "factor": 1.0},
                ],
            })

        recipe = await _get_recipe(mealie_http, slug)
        ing = next(i for i in recipe.get("recipeIngredient") or [] if i.get("referenceId") == ref_id)
        assert ing.get("quantity") == pytest.approx(3.0)
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_food(mealie_http, food["id"])
        await _delete_unit(mealie_http, clove["id"])
        await _delete_unit(mealie_http, tsp["id"])


async def test_normalize_only_touches_listed_ingredients(mealie_http, mcp_server):
    """Ingredients not in conversions list are not modified."""
    food = await _create_food(mealie_http, "TestGarlicSelective")
    clove = await _create_unit(mealie_http, "TestCloveSelective")
    tsp = await _create_unit(mealie_http, "TestTspSelective")
    slug = await _create_recipe(mealie_http, "GarlicSelectiveRecipe")
    try:
        await _add_ingredient(mealie_http, slug, food, tsp, quantity=1.0, note="first")
        await _add_ingredient(mealie_http, slug, food, tsp, quantity=2.0, note="second")

        recipe = await _get_recipe(mealie_http, slug)
        ings = [i for i in recipe.get("recipeIngredient") or [] if (i.get("food") or {}).get("id") == food["id"]]
        ref_first, ref_second = ings[0]["referenceId"], ings[1]["referenceId"]

        async with Client(mcp_server) as client:
            await client.call_tool("normalize_ingredients", {
                "food_id": food["id"],
                "target_unit_id": clove["id"],
                "conversions": [
                    {"recipe_slug": slug, "reference_id": ref_first, "factor": 1.0},
                ],
            })

        recipe = await _get_recipe(mealie_http, slug)
        by_ref = {i["referenceId"]: i for i in recipe.get("recipeIngredient") or []}
        assert (by_ref[ref_first].get("unit") or {}).get("id") == clove["id"], "first should be updated"
        assert (by_ref[ref_second].get("unit") or {}).get("id") == tsp["id"], "second must be unchanged"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_food(mealie_http, food["id"])
        await _delete_unit(mealie_http, clove["id"])
        await _delete_unit(mealie_http, tsp["id"])


async def test_normalize_unknown_unit_returns_error(mcp_server):
    """normalize_ingredients with an unknown target_unit_id returns an error."""
    async with Client(mcp_server) as client:
        result = await client.call_tool("normalize_ingredients", {
            "food_id": "any-food-id",
            "target_unit_id": "nonexistent-unit-id-xyz",
            "conversions": [],
        })
    assert "ERROR" in result.content[0].text


async def test_normalize_unknown_recipe_returns_error(mcp_server):
    """normalize_ingredients with an unknown recipe slug logs an ERROR in the report."""
    async with Client(mcp_server) as client:
        result = await client.call_tool("normalize_ingredients", {
            "food_id": "any-food-id",
            "target_unit_id": None,
            "conversions": [
                {"recipe_slug": "nonexistent-slug-xyz", "reference_id": "some-ref", "factor": 1.0},
            ],
        })
    assert "ERROR" in result.content[0].text
