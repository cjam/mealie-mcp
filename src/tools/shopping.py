"""Shopping list tools."""

from __future__ import annotations

from typing import Any

import httpx


def register_shopping_tools(mcp: Any, client: httpx.AsyncClient) -> None:

    @mcp.tool()
    async def replace_shopping_list_from_recipes(
        list_id: str,
        recipe_ids: list[str],
        scale: float = 1.0,
    ) -> str:
        """
        Replace all items in a shopping list with the ingredients from a set of recipes.

        Steps:
          1. Fetch all current items on the list.
          2. Bulk-delete them.
          3. For each recipe, call Mealie's ingredient-expansion endpoint
             (POST /api/households/shopping/lists/{id}/recipe/{recipe_id}) so Mealie
             handles unit conversion, quantity scaling, and ingredient deduplication.

        Args:
            list_id:    UUID of the shopping list to replace.
            recipe_ids: Ordered list of recipe UUIDs whose ingredients to add.
            scale:      Serving multiplier applied to every recipe (default 1.0).

        Returns:
            Plain-text summary of how many items were cleared and how many recipes
            were added successfully.
        """
        all_items: list[dict] = []
        page = 1
        while True:
            resp = await client.get(
                "/api/households/shopping/items",
                params={"shoppingListId": list_id, "page": page, "perPage": 100},
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("items", []) if isinstance(data, dict) else data
            all_items.extend(batch)
            total_pages = (
                data.get("totalPages") or data.get("total_pages", 1)
                if isinstance(data, dict) else 1
            )
            if page >= total_pages:
                break
            page += 1

        lines = [f"=== Replace Shopping List from Recipes (list: {list_id}) ===", ""]

        deleted = 0
        if all_items:
            item_ids = [i["id"] for i in all_items if i.get("id")]
            del_resp = await client.request(
                "DELETE",
                "/api/households/shopping/items",
                json={"ids": item_ids},
            )
            if del_resp.is_success:
                deleted = len(item_ids)
            else:
                lines.append(
                    f"  WARN: bulk delete returned HTTP {del_resp.status_code} — proceeding anyway"
                )
        lines.append(f"  Cleared {deleted}/{len(all_items)} existing items")

        added = 0
        params = {"recipeIncrements": scale} if scale != 1.0 else {}
        for recipe_id in recipe_ids:
            r = await client.post(
                f"/api/households/shopping/lists/{list_id}/recipe/{recipe_id}",
                params=params or None,
            )
            if r.is_success:
                added += 1
            else:
                lines.append(f"  WARN: add recipe {recipe_id} failed — HTTP {r.status_code}")
        lines.append(f"  Added {added}/{len(recipe_ids)} recipes")
        lines.append("\n=== Done ===")
        return "\n".join(lines)
