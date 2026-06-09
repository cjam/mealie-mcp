"""Integration tests for the cleanup_system composed tool."""

from __future__ import annotations

import pytest
import httpx
from fastmcp import Client

from server import build_client


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def mealie_http(mealie_settings):
    """Authenticated async HTTP client for direct Mealie API access."""
    async with build_client(mealie_settings) as client:
        yield client


# ── API helpers ───────────────────────────────────────────────────────────────

async def _create_food(client: httpx.AsyncClient, name: str) -> str:
    r = await client.post("/api/foods", json={"name": name})
    r.raise_for_status()
    return r.json()["id"]


async def _create_unit(client: httpx.AsyncClient, name: str, abbreviation: str = "") -> str:
    r = await client.post("/api/units", json={"name": name, "abbreviation": abbreviation})
    r.raise_for_status()
    return r.json()["id"]


async def _food_names(client: httpx.AsyncClient) -> set[str]:
    r = await client.get("/api/foods", params={"perPage": 200})
    r.raise_for_status()
    return {f["name"] for f in r.json().get("items", [])}


async def _unit_names(client: httpx.AsyncClient) -> set[str]:
    r = await client.get("/api/units", params={"perPage": 200})
    r.raise_for_status()
    return {u["name"] for u in r.json().get("items", [])}


async def _food_ids_by_name(client: httpx.AsyncClient, name: str) -> list[str]:
    r = await client.get("/api/foods", params={"perPage": 200})
    r.raise_for_status()
    return [f["id"] for f in r.json().get("items", []) if f["name"].lower() == name.lower()]


async def _unit_ids_by_name(client: httpx.AsyncClient, name: str) -> list[str]:
    r = await client.get("/api/units", params={"perPage": 200})
    r.raise_for_status()
    return [u["id"] for u in r.json().get("items", []) if u["name"].lower() == name.lower()]


async def _delete_food(client: httpx.AsyncClient, food_id: str) -> None:
    await client.delete(f"/api/foods/{food_id}")


async def _delete_unit(client: httpx.AsyncClient, unit_id: str) -> None:
    await client.delete(f"/api/units/{unit_id}")


async def _purge_foods(client: httpx.AsyncClient, *names: str) -> None:
    for name in names:
        for fid in await _food_ids_by_name(client, name):
            await _delete_food(client, fid)


async def _purge_units(client: httpx.AsyncClient, *names: str) -> None:
    for name in names:
        for uid in await _unit_ids_by_name(client, name):
            await _delete_unit(client, uid)


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_cleanup_system_tool_is_registered(mcp_server):
    """cleanup_system appears in the MCP tool list."""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "cleanup_system" for t in tools)


async def test_dry_run_reports_but_makes_no_changes(mealie_http, mcp_server):
    """dry_run=True describes what would change without writing anything."""
    food_id = await _create_food(mealie_http, "granny smith apple")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool("cleanup_system", {"dry_run": True})

        report = result.content[0].text
        assert "DRY RUN" in report
        # The food name (or its Title-Case form) should appear in the planned renames
        assert "granny smith apple" in report.lower()

        # State must be unchanged
        names = await _food_names(mealie_http)
        assert "granny smith apple" in names, "dry_run must not rename or delete the food"
    finally:
        await _delete_food(mealie_http, food_id)


async def test_renames_food_to_title_case(mealie_http, mcp_server):
    """A food stored in all-lowercase is renamed to Title Case."""
    await _purge_foods(mealie_http, "black pepper")
    await _create_food(mealie_http, "black pepper")
    try:
        async with Client(mcp_server) as client:
            await client.call_tool("cleanup_system", {})

        names = await _food_names(mealie_http)
        assert "Black Pepper" in names, f"Expected 'Black Pepper' in {names}"
        assert "black pepper" not in names
    finally:
        await _purge_foods(mealie_http, "black pepper", "Black Pepper")


async def test_merges_case_variant_foods(mealie_http, mcp_server):
    """Two foods that differ only in case are merged into one Title-Case entry."""
    await _purge_foods(mealie_http, "garlic")
    await _create_food(mealie_http, "garlic")
    await _create_food(mealie_http, "Garlic")
    try:
        async with Client(mcp_server) as client:
            await client.call_tool("cleanup_system", {})

        names = await _food_names(mealie_http)
        garlic_variants = [n for n in names if n.lower() == "garlic"]
        assert len(garlic_variants) == 1, f"Expected exactly one garlic entry, got: {garlic_variants}"
        assert garlic_variants[0] == "Garlic"
    finally:
        await _purge_foods(mealie_http, "garlic")


async def test_merges_unit_abbreviation_with_full_name(mealie_http, mcp_server):
    """A unit stored as 'tsp' alongside 'teaspoon' is merged into 'teaspoon'."""
    await _purge_units(mealie_http, "tsp", "teaspoon")
    await _create_unit(mealie_http, "tsp", "tsp")
    await _create_unit(mealie_http, "teaspoon", "tsp")
    try:
        async with Client(mcp_server) as client:
            await client.call_tool("cleanup_system", {})

        names = await _unit_names(mealie_http)
        tsp_variants = [n for n in names if n.lower() in ("tsp", "teaspoon")]
        assert len(tsp_variants) == 1, f"Expected exactly one teaspoon-like unit, got: {tsp_variants}"
        assert tsp_variants[0] == "teaspoon"
    finally:
        await _purge_units(mealie_http, "tsp", "teaspoon")


async def test_renames_lone_unit_abbreviation(mealie_http, mcp_server):
    """A unit named 'tbsp' with no full-name counterpart is renamed to 'tablespoon'."""
    await _purge_units(mealie_http, "tbsp", "tablespoon")
    await _create_unit(mealie_http, "tbsp", "tbsp")
    try:
        async with Client(mcp_server) as client:
            await client.call_tool("cleanup_system", {})

        names = await _unit_names(mealie_http)
        assert "tablespoon" in names, f"Expected 'tablespoon' in {names}"
        assert "tbsp" not in names, f"'tbsp' should have been renamed"
    finally:
        await _purge_units(mealie_http, "tbsp", "tablespoon")


async def test_creates_missing_standard_units(mealie_http, mcp_server):
    """Standard units absent from the database are created by cleanup."""
    # Remove a representative sample so there's definitely something to add.
    targets = ["cup", "gram", "pinch"]
    await _purge_units(mealie_http, *targets)
    try:
        async with Client(mcp_server) as client:
            await client.call_tool("cleanup_system", {})

        names = await _unit_names(mealie_http)
        for name in targets:
            assert name in names, f"Standard unit '{name}' was not created; present: {names}"
    finally:
        await _purge_units(mealie_http, *targets)


async def test_cleanup_is_idempotent(mealie_http, mcp_server):
    """Running cleanup a second time reports nothing left to change."""
    async with Client(mcp_server) as client:
        await client.call_tool("cleanup_system", {})
        result = await client.call_tool("cleanup_system", {})

    report = result.content[0].text
    assert "MERGE" not in report, f"Unexpected merges on second run:\n{report}"
    assert "RENAME" not in report, f"Unexpected renames on second run:\n{report}"
    assert "ADD" not in report, f"Unexpected additions on second run:\n{report}"


async def test_report_contains_all_sections(mcp_server):
    """Cleanup report always includes Backup, Foods, and Units sections."""
    async with Client(mcp_server) as client:
        result = await client.call_tool("cleanup_system", {"dry_run": True})

    report = result.content[0].text
    for section in ("## Backup", "## Foods", "## Units", "=== Done ==="):
        assert section in report, f"Missing section '{section}' in report"


# ── merge_foods / merge_units tests ──────────────────────────────────────────

async def test_merge_foods_tool_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "merge_foods" for t in tools)


async def test_merge_units_tool_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "merge_units" for t in tools)


async def test_merge_foods_combines_two_foods(mealie_http, mcp_server):
    """merge_foods absorbs the duplicate into the keep entry (one remains)."""
    await _purge_foods(mealie_http, "Basil", "basil")
    keep_id = await _create_food(mealie_http, "Basil")
    dup_id = await _create_food(mealie_http, "basil")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "merge_foods", {"keep_name": "Basil", "remove_name": "basil"}
            )
        report = result.content[0].text
        assert "Merged" in report, f"Expected success message, got: {report}"

        names = await _food_names(mealie_http)
        basil_variants = [n for n in names if n.lower() == "basil"]
        assert len(basil_variants) == 1, f"Expected exactly one basil after merge, got: {basil_variants}"
    finally:
        for fid in await _food_ids_by_name(mealie_http, "Basil"):
            await _delete_food(mealie_http, fid)
        for fid in await _food_ids_by_name(mealie_http, "basil"):
            await _delete_food(mealie_http, fid)


async def test_merge_foods_unknown_keep(mcp_server):
    """merge_foods returns an error if keep_name is not in the database."""
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "merge_foods",
            {"keep_name": "nonexistent_food_xyz", "remove_name": "also_nonexistent"},
        )
    assert "ERROR" in result.content[0].text


async def test_merge_units_combines_two_units(mealie_http, mcp_server):
    """merge_units absorbs the duplicate into the keep entry (one remains)."""
    await _purge_units(mealie_http, "tablespoon", "tbsp")
    keep_id = await _create_unit(mealie_http, "tablespoon", "tbsp")
    dup_id = await _create_unit(mealie_http, "tbsp", "tbsp")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "merge_units", {"keep_name": "tablespoon", "remove_name": "tbsp"}
            )
        report = result.content[0].text
        assert "Merged" in report, f"Expected success message, got: {report}"

        names = await _unit_names(mealie_http)
        tbsp_variants = [n for n in names if n.lower() in ("tablespoon", "tbsp")]
        assert len(tbsp_variants) == 1, f"Expected exactly one entry after merge, got: {tbsp_variants}"
    finally:
        await _purge_units(mealie_http, "tablespoon", "tbsp")


async def test_merge_units_unknown_remove(mealie_http, mcp_server):
    """merge_units returns an error if remove_name is not in the database."""
    await _purge_units(mealie_http, "cup")
    uid = await _create_unit(mealie_http, "cup", "c")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "merge_units",
                {"keep_name": "cup", "remove_name": "nonexistent_unit_xyz"},
            )
        assert "ERROR" in result.content[0].text
    finally:
        await _purge_units(mealie_http, "cup")


# ── delete_many_foods ─────────────────────────────────────────────────────────

async def test_delete_many_foods_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "delete_many_foods" for t in tools)


async def test_delete_many_foods_removes_all(mealie_http, mcp_server):
    """delete_many_foods removes every listed food."""
    await _purge_foods(mealie_http, "JunkA", "JunkB")
    await _create_food(mealie_http, "JunkA")
    await _create_food(mealie_http, "JunkB")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "delete_many_foods", {"food_names": ["JunkA", "JunkB"]}
            )
        report = result.content[0].text
        assert "2 deleted" in report, f"Unexpected report: {report}"
        names = await _food_names(mealie_http)
        assert "JunkA" not in names and "JunkB" not in names
    finally:
        await _purge_foods(mealie_http, "JunkA", "JunkB")


async def test_delete_many_foods_partial_not_found(mealie_http, mcp_server):
    """delete_many_foods reports errors for unknown names, still deletes known ones."""
    await _purge_foods(mealie_http, "RealFood")
    await _create_food(mealie_http, "RealFood")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "delete_many_foods",
                {"food_names": ["RealFood", "nonexistent_food_xyz"]},
            )
        report = result.content[0].text
        assert "1 deleted" in report
        assert "1 not found" in report
        names = await _food_names(mealie_http)
        assert "RealFood" not in names
    finally:
        await _purge_foods(mealie_http, "RealFood")


# ── merge_many_foods ──────────────────────────────────────────────────────────

async def test_merge_many_foods_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "merge_many_foods" for t in tools)


async def test_merge_many_foods_merges_all_pairs(mealie_http, mcp_server):
    """merge_many_foods absorbs each remove_name into its keep_name."""
    await _purge_foods(mealie_http, "Basil", "basil", "Oregano", "oregano")
    await _create_food(mealie_http, "Basil")
    await _create_food(mealie_http, "basil")
    await _create_food(mealie_http, "Oregano")
    await _create_food(mealie_http, "oregano")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "merge_many_foods",
                {
                    "merges": [
                        {"keep_name": "Basil", "remove_name": "basil"},
                        {"keep_name": "Oregano", "remove_name": "oregano"},
                    ]
                },
            )
        report = result.content[0].text
        assert "2 merged" in report, f"Unexpected report: {report}"
        names = await _food_names(mealie_http)
        basil_variants = [n for n in names if n.lower() == "basil"]
        oregano_variants = [n for n in names if n.lower() == "oregano"]
        assert len(basil_variants) == 1
        assert len(oregano_variants) == 1
    finally:
        await _purge_foods(mealie_http, "Basil", "basil", "Oregano", "oregano")


async def test_merge_many_foods_partial_error(mcp_server):
    """merge_many_foods reports errors for unknown names without aborting valid pairs."""
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "merge_many_foods",
            {
                "merges": [
                    {"keep_name": "nonexistent_keep_xyz", "remove_name": "nonexistent_remove_xyz"},
                ]
            },
        )
    assert "ERROR" in result.content[0].text or "1 failed" in result.content[0].text


# ── update_many_foods ─────────────────────────────────────────────────────────

async def test_update_many_foods_is_registered(mcp_server):
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
    assert any(t.name == "update_many_foods" for t in tools)


async def test_update_many_foods_renames_all(mealie_http, mcp_server):
    """update_many_foods renames each listed food."""
    await _purge_foods(mealie_http, "Tumeric", "Turmeric", "Cilantro", "Coriander Leaf")
    await _create_food(mealie_http, "Tumeric")
    await _create_food(mealie_http, "Cilantro")
    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "update_many_foods",
                {
                    "updates": [
                        {"food_name": "Tumeric", "new_name": "Turmeric"},
                        {"food_name": "Cilantro", "new_name": "Coriander Leaf"},
                    ]
                },
            )
        report = result.content[0].text
        assert "2 updated" in report, f"Unexpected report: {report}"
        names = await _food_names(mealie_http)
        assert "Turmeric" in names
        assert "Coriander Leaf" in names
        assert "Tumeric" not in names
        assert "Cilantro" not in names
    finally:
        await _purge_foods(mealie_http, "Tumeric", "Turmeric", "Cilantro", "Coriander Leaf")


async def test_update_many_foods_partial_not_found(mcp_server):
    """update_many_foods reports errors for unknown foods without aborting valid ones."""
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "update_many_foods",
            {
                "updates": [
                    {"food_name": "nonexistent_food_xyz", "new_name": "Something"},
                ]
            },
        )
    assert "1 failed" in result.content[0].text or "ERROR" in result.content[0].text
