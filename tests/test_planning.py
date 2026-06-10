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
