"""Integration tests for get_all_recipes, get_recipes_detail, set_recipe_tags, set_recipe_categories."""

from __future__ import annotations

import pytest
from fastmcp import Client

from server import build_client


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def mealie_http(mealie_settings):
    async with build_client(mealie_settings) as client:
        yield client


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _create_recipe(client, name: str) -> str:
    r = await client.post("/api/recipes", json={"name": name})
    r.raise_for_status()
    return r.json()


async def _delete_recipe(client, slug: str) -> None:
    await client.delete(f"/api/recipes/{slug}")


async def _get_recipe(client, slug: str) -> dict:
    r = await client.get(f"/api/recipes/{slug}")
    r.raise_for_status()
    return r.json()


async def _delete_tag_by_name(client, name: str) -> None:
    r = await client.get("/api/organizers/tags", params={"perPage": 200})
    for tag in r.json().get("items", []):
        if tag["name"].lower() == name.lower():
            await client.delete(f"/api/organizers/tags/{tag['id']}")


async def _delete_category_by_name(client, name: str) -> None:
    r = await client.get("/api/organizers/categories", params={"perPage": 200})
    for cat in r.json().get("items", []):
        if cat["name"].lower() == name.lower():
            await client.delete(f"/api/organizers/categories/{cat['id']}")


# ── Registration tests ────────────────────────────────────────────────────────


async def test_get_all_recipes_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "get_all_recipes" for t in tools)


async def test_get_recipes_detail_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "get_recipes_detail" for t in tools)


async def test_set_recipe_tags_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "set_recipe_tags" for t in tools)


async def test_set_recipe_categories_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "set_recipe_categories" for t in tools)


# ── get_all_recipes ───────────────────────────────────────────────────────────


async def test_get_all_recipes_includes_created_recipe(mealie_http, mcp_server):
    """get_all_recipes returns a listing that includes a freshly created recipe."""
    slug = await _create_recipe(mealie_http, "GetAllRecipes: Test Recipe XYZ")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("get_all_recipes", {})
        report = result.content[0].text
        assert slug in report, f"New slug not found in listing:\n{report}"
        assert "GetAllRecipes: Test Recipe XYZ" in report
    finally:
        await _delete_recipe(mealie_http, slug)


async def test_get_all_recipes_shows_tags_and_categories(mealie_http, mcp_server):
    """get_all_recipes includes tag and category metadata when present."""
    tag_name = "xyzzy_ga_tag"
    cat_name = "xyzzy_ga_cat"
    await _delete_tag_by_name(mealie_http, tag_name)
    await _delete_category_by_name(mealie_http, cat_name)

    slug = await _create_recipe(mealie_http, "GetAllRecipes: With Meta")
    try:
        # Apply tag and category via the set_* tools
        async with Client(mcp_server) as mcp:
            await mcp.call_tool("set_recipe_tags", {"recipe_slug": slug, "tag_names": [tag_name]})
            await mcp.call_tool("set_recipe_categories", {"recipe_slug": slug, "category_names": [cat_name]})
            result = await mcp.call_tool("get_all_recipes", {})

        report = result.content[0].text
        assert tag_name in report, f"Tag not in listing:\n{report}"
        assert cat_name in report, f"Category not in listing:\n{report}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_tag_by_name(mealie_http, tag_name)
        await _delete_category_by_name(mealie_http, cat_name)


# ── get_recipes_detail ────────────────────────────────────────────────────────


async def test_get_recipes_detail_returns_ingredient_and_step_data(mealie_http, mcp_server):
    """get_recipes_detail shows ingredient and step content for the requested slug."""
    slug = await _create_recipe(mealie_http, "Detail: Soup")
    try:
        # Add an ingredient and a step via direct PUT
        recipe = await _get_recipe(mealie_http, slug)
        import uuid
        ref_id = str(uuid.uuid4())
        recipe["recipeIngredient"] = [{
            "referenceId": ref_id,
            "food": None,
            "unit": None,
            "quantity": 2.0,
            "note": "detail test ingredient",
            "display": "detail test ingredient",
            "title": None,
            "originalText": "detail test ingredient",
        }]
        recipe["recipeInstructions"] = [{"title": "", "text": "Stir it all together.", "ingredientReferences": []}]
        r = await mealie_http.put(f"/api/recipes/{slug}", json=recipe)
        r.raise_for_status()

        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("get_recipes_detail", {"slugs": [slug]})

        report = result.content[0].text
        assert "Detail: Soup" in report
        assert "detail test ingredient" in report
        assert "Stir it all together" in report
    finally:
        await _delete_recipe(mealie_http, slug)


async def test_get_recipes_detail_handles_multiple_slugs(mealie_http, mcp_server):
    """get_recipes_detail returns data for each slug in the list."""
    slug_a = await _create_recipe(mealie_http, "Detail: Recipe A")
    slug_b = await _create_recipe(mealie_http, "Detail: Recipe B")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool("get_recipes_detail", {"slugs": [slug_a, slug_b]})
        report = result.content[0].text
        assert "Detail: Recipe A" in report
        assert "Detail: Recipe B" in report
    finally:
        await _delete_recipe(mealie_http, slug_a)
        await _delete_recipe(mealie_http, slug_b)


async def test_get_recipes_detail_reports_error_for_unknown_slug(mcp_server):
    """get_recipes_detail emits an ERROR line for a slug that doesn't exist."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("get_recipes_detail", {"slugs": ["does-not-exist-xyzzy-abc"]})
    report = result.content[0].text
    assert "ERROR" in report, f"Expected ERROR for unknown slug:\n{report}"


async def test_get_recipes_detail_mixed_valid_and_invalid(mealie_http, mcp_server):
    """get_recipes_detail succeeds for valid slugs even when one is invalid."""
    slug = await _create_recipe(mealie_http, "Detail: Mixed Valid")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "get_recipes_detail",
                {"slugs": [slug, "xyzzy-does-not-exist"]},
            )
        report = result.content[0].text
        assert "Detail: Mixed Valid" in report
        assert "ERROR" in report
    finally:
        await _delete_recipe(mealie_http, slug)


# ── set_recipe_tags ───────────────────────────────────────────────────────────


async def test_set_recipe_tags_creates_and_applies_tags(mealie_http, mcp_server):
    """set_recipe_tags creates new tags and links them to the recipe."""
    tag_a = "xyzzy_tag_create_a"
    tag_b = "xyzzy_tag_create_b"
    await _delete_tag_by_name(mealie_http, tag_a)
    await _delete_tag_by_name(mealie_http, tag_b)

    slug = await _create_recipe(mealie_http, "SetTags: Create Tags")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "set_recipe_tags",
                {"recipe_slug": slug, "tag_names": [tag_a, tag_b]},
            )
        report = result.content[0].text
        assert "created" in report.lower(), f"Expected 'created':\n{report}"
        assert "PATCH OK" in report

        recipe = await _get_recipe(mealie_http, slug)
        names = {t["name"].lower() for t in recipe.get("tags") or []}
        assert tag_a in names, f"Tag A not on recipe: {names}"
        assert tag_b in names, f"Tag B not on recipe: {names}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_tag_by_name(mealie_http, tag_a)
        await _delete_tag_by_name(mealie_http, tag_b)


async def test_set_recipe_tags_reuses_existing_tag(mealie_http, mcp_server):
    """set_recipe_tags links to an existing tag without creating a duplicate."""
    tag_name = "xyzzy_tag_reuse"
    await _delete_tag_by_name(mealie_http, tag_name)
    r = await mealie_http.post("/api/organizers/tags", json={"name": tag_name})
    existing_id = r.json()["id"]

    slug = await _create_recipe(mealie_http, "SetTags: Reuse Tag")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "set_recipe_tags",
                {"recipe_slug": slug, "tag_names": [tag_name]},
            )
        report = result.content[0].text
        assert "found" in report.lower(), f"Expected 'found':\n{report}"

        # No duplicate should have been created
        r2 = await mealie_http.get("/api/organizers/tags", params={"perPage": 200})
        ids = [t["id"] for t in r2.json().get("items", []) if t["name"].lower() == tag_name]
        assert len(ids) == 1, f"Duplicate tag created: {ids}"
        assert ids[0] == existing_id
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_tag_by_name(mealie_http, tag_name)


async def test_set_recipe_tags_empty_list_clears_tags(mealie_http, mcp_server):
    """set_recipe_tags with an empty list removes all tags from the recipe."""
    tag_name = "xyzzy_tag_to_clear"
    await _delete_tag_by_name(mealie_http, tag_name)

    slug = await _create_recipe(mealie_http, "SetTags: Clear Tags")
    try:
        async with Client(mcp_server) as mcp:
            # First apply a tag, then clear
            await mcp.call_tool("set_recipe_tags", {"recipe_slug": slug, "tag_names": [tag_name]})
            await mcp.call_tool("set_recipe_tags", {"recipe_slug": slug, "tag_names": []})

        recipe = await _get_recipe(mealie_http, slug)
        assert not (recipe.get("tags") or []), f"Tags not cleared: {recipe.get('tags')}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_tag_by_name(mealie_http, tag_name)


async def test_set_recipe_tags_returns_error_for_unknown_slug(mcp_server):
    """set_recipe_tags returns a readable error for a non-existent recipe."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "set_recipe_tags",
            {"recipe_slug": "does-not-exist-xyzzy", "tag_names": ["sometag"]},
        )
    assert "ERROR" in result.content[0].text


# ── set_recipe_categories ─────────────────────────────────────────────────────


async def test_set_recipe_categories_creates_and_applies(mealie_http, mcp_server):
    """set_recipe_categories creates new categories and links them to the recipe."""
    cat_a = "xyzzy_cat_create_a"
    cat_b = "xyzzy_cat_create_b"
    await _delete_category_by_name(mealie_http, cat_a)
    await _delete_category_by_name(mealie_http, cat_b)

    slug = await _create_recipe(mealie_http, "SetCats: Create Categories")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "set_recipe_categories",
                {"recipe_slug": slug, "category_names": [cat_a, cat_b]},
            )
        report = result.content[0].text
        assert "created" in report.lower(), f"Expected 'created':\n{report}"
        assert "PATCH OK" in report

        recipe = await _get_recipe(mealie_http, slug)
        names = {c["name"].lower() for c in recipe.get("recipeCategory") or []}
        assert cat_a in names, f"Category A not on recipe: {names}"
        assert cat_b in names, f"Category B not on recipe: {names}"
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_category_by_name(mealie_http, cat_a)
        await _delete_category_by_name(mealie_http, cat_b)


async def test_set_recipe_categories_reuses_existing(mealie_http, mcp_server):
    """set_recipe_categories links to an existing category without creating a duplicate."""
    cat_name = "xyzzy_cat_reuse"
    await _delete_category_by_name(mealie_http, cat_name)
    r = await mealie_http.post("/api/organizers/categories", json={"name": cat_name})
    existing_id = r.json()["id"]

    slug = await _create_recipe(mealie_http, "SetCats: Reuse Category")
    try:
        async with Client(mcp_server) as mcp:
            result = await mcp.call_tool(
                "set_recipe_categories",
                {"recipe_slug": slug, "category_names": [cat_name]},
            )
        report = result.content[0].text
        assert "found" in report.lower(), f"Expected 'found':\n{report}"

        r2 = await mealie_http.get("/api/organizers/categories", params={"perPage": 200})
        ids = [c["id"] for c in r2.json().get("items", []) if c["name"].lower() == cat_name]
        assert len(ids) == 1, f"Duplicate category created: {ids}"
        assert ids[0] == existing_id
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_category_by_name(mealie_http, cat_name)


async def test_set_recipe_categories_empty_list_clears(mealie_http, mcp_server):
    """set_recipe_categories with an empty list removes all categories from the recipe."""
    cat_name = "xyzzy_cat_to_clear"
    await _delete_category_by_name(mealie_http, cat_name)

    slug = await _create_recipe(mealie_http, "SetCats: Clear Categories")
    try:
        async with Client(mcp_server) as mcp:
            await mcp.call_tool("set_recipe_categories", {"recipe_slug": slug, "category_names": [cat_name]})
            await mcp.call_tool("set_recipe_categories", {"recipe_slug": slug, "category_names": []})

        recipe = await _get_recipe(mealie_http, slug)
        assert not (recipe.get("recipeCategory") or []), (
            f"Categories not cleared: {recipe.get('recipeCategory')}"
        )
    finally:
        await _delete_recipe(mealie_http, slug)
        await _delete_category_by_name(mealie_http, cat_name)


async def test_set_recipe_categories_returns_error_for_unknown_slug(mcp_server):
    """set_recipe_categories returns a readable error for a non-existent recipe."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "set_recipe_categories",
            {"recipe_slug": "does-not-exist-xyzzy", "category_names": ["somecat"]},
        )
    assert "ERROR" in result.content[0].text
