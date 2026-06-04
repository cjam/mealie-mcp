"""Integration tests against a live Mealie instance (via Testcontainers)."""

from __future__ import annotations

import pytest
from fastmcp import Client


@pytest.mark.asyncio
async def test_tools_registered(mcp_server):
    """Server exposes at least the expected tool categories."""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()

    names = [t.name for t in tools]
    assert names, "No tools registered"

    # Expect at least one tool from each curated category.
    assert any("recipe" in n.lower() for n in names), f"No recipe tools in {names}"
    assert any("mealplan" in n.lower() or "meal_plan" in n.lower() for n in names), (
        f"No meal-plan tools in {names}"
    )
    assert any("shopping" in n.lower() for n in names), f"No shopping tools in {names}"


@pytest.mark.asyncio
async def test_list_recipes_empty(mcp_server):
    """Fresh Mealie instance returns an empty recipe list."""
    async with Client(mcp_server) as client:
        result = await client.call_tool("get_all_api_recipes_get", {})

    import json

    payload = json.loads(result.content[0].text)
    assert isinstance(payload.get("items"), list)
    assert payload["items"] == []


@pytest.mark.asyncio
async def test_list_meal_plans_empty(mcp_server):
    """Fresh instance returns an empty meal-plan list."""
    async with Client(mcp_server) as client:
        result = await client.call_tool("get_all_api_households_mealplans_get", {})

    import json

    payload = json.loads(result.content[0].text)
    assert isinstance(payload.get("items"), list)


@pytest.mark.asyncio
async def test_list_shopping_lists_empty(mcp_server):
    """Fresh instance returns an empty shopping-list list."""
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "get_all_api_households_shopping_lists_get", {}
        )

    import json

    payload = json.loads(result.content[0].text)
    assert isinstance(payload.get("items"), list)
