"""Integration tests for meal planning tools."""

from __future__ import annotations

import pytest
import httpx
from fastmcp import Client

from server import build_client


@pytest.fixture
async def mealie_http(mealie_settings):
    async with build_client(mealie_settings) as client:
        yield client


async def _create_recipe(client: httpx.AsyncClient, name: str) -> dict:
    r = await client.post("/api/recipes", json={"name": name})
    r.raise_for_status()
    slug = r.json()
    r2 = await client.get(f"/api/recipes/{slug}")
    r2.raise_for_status()
    return r2.json()


async def _delete_recipe(client: httpx.AsyncClient, slug: str) -> None:
    await client.delete(f"/api/recipes/{slug}")


async def _get_mealplans(
    client: httpx.AsyncClient, start_date: str, end_date: str
) -> list[dict]:
    r = await client.get(
        "/api/households/mealplans",
        params={"start_date": start_date, "end_date": end_date, "perPage": 500},
    )
    r.raise_for_status()
    data = r.json()
    return data.get("items", data) if isinstance(data, dict) else data


async def _delete_mealplan_entry(client: httpx.AsyncClient, entry_id: str) -> None:
    await client.delete(f"/api/households/mealplans/{entry_id}")


def _note_ingredient(note_text: str) -> dict:
    """Raw-text ingredient, as produced by URL import (no linked food/unit)."""
    return {
        "referenceId": None,
        "food": None,
        "unit": None,
        "quantity": 0.0,
        "note": note_text,
        "display": note_text,
        "title": None,
        "originalText": note_text,
    }


async def _create_recipe_with_ingredients(
    client: httpx.AsyncClient, name: str, ingredient_notes: list[str]
) -> dict:
    recipe = await _create_recipe(client, name)
    recipe["recipeIngredient"] = [_note_ingredient(n) for n in ingredient_notes]
    r = await client.put(f"/api/recipes/{recipe['slug']}", json=recipe)
    r.raise_for_status()
    return r.json()


async def test_replace_week_meal_plan_accepts_slug(mcp_server, mealie_http):
    """replace_week_meal_plan resolves recipe slugs to UUIDs without error."""
    recipe = await _create_recipe(mealie_http, "Test Meal Plan Slug Recipe")
    week_start = "2030-01-07"
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "replace_week_meal_plan",
                {
                    "week_start": week_start,
                    "entries": [
                        {
                            "date": week_start,
                            "recipe_id": recipe["slug"],
                            "entry_type": "dinner",
                        }
                    ],
                },
            )
        text = result.content[0].text
        assert "could not resolve recipe" not in text
        assert "Created 1/1" in text

        entries = await _get_mealplans(mealie_http, week_start, "2030-01-13")
        assert len(entries) == 1
        assert entries[0]["recipe"]["id"] == recipe["id"]
    finally:
        entries = await _get_mealplans(mealie_http, week_start, "2030-01-13")
        for e in entries:
            await _delete_mealplan_entry(mealie_http, e["id"])
        await _delete_recipe(mealie_http, recipe["slug"])


async def test_replace_week_meal_plan_warns_on_missing_slug(mcp_server, mealie_http):
    """replace_week_meal_plan warns and skips an unresolvable slug."""
    week_start = "2030-01-14"
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "replace_week_meal_plan",
            {
                "week_start": week_start,
                "entries": [
                    {
                        "date": week_start,
                        "recipe_id": "no-such-recipe-slug",
                        "entry_type": "dinner",
                    }
                ],
            },
        )
    text = result.content[0].text
    assert "could not resolve recipe" in text

    entries = await _get_mealplans(mealie_http, week_start, "2030-01-20")
    assert len(entries) == 0


async def test_find_recipes_using_ingredients_registered(mcp_server):
    async with Client(mcp_server) as mcp:
        tools = await mcp.list_tools()
    assert "find_recipes_using_ingredients" in [t.name for t in tools]


async def test_find_recipes_using_ingredients_ranks_by_match_count(mcp_server, mealie_http):
    """Recipes matching more of the given ingredients rank above those matching fewer,
    and recipes matching none are excluded."""
    cabbage_chicken = await _create_recipe_with_ingredients(
        mealie_http,
        "Find-Ingredients Cabbage Chicken Skillet",
        ["1 head cabbage", "2 chicken breasts"],
    )
    eggs_only = await _create_recipe_with_ingredients(
        mealie_http, "Find-Ingredients Egg Scramble", ["3 eggs"]
    )
    unrelated = await _create_recipe_with_ingredients(
        mealie_http, "Find-Ingredients Pasta Bake", ["1 lb pasta"]
    )
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "find_recipes_using_ingredients",
                {"ingredients": ["cabbage", "eggs", "chicken"]},
            )
        text = result.content[0].text

        assert cabbage_chicken["slug"] in text
        assert eggs_only["slug"] in text
        assert unrelated["slug"] not in text
        # The recipe matching two ingredients should be listed before the one matching one.
        assert text.index(cabbage_chicken["slug"]) < text.index(eggs_only["slug"])
    finally:
        await _delete_recipe(mealie_http, cabbage_chicken["slug"])
        await _delete_recipe(mealie_http, eggs_only["slug"])
        await _delete_recipe(mealie_http, unrelated["slug"])


async def test_find_recipes_using_ingredients_no_matches(mcp_server):
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "find_recipes_using_ingredients",
            {"ingredients": ["nonexistent-ingredient-xyz123"]},
        )
    text = result.content[0].text
    assert "No recipes found" in text
