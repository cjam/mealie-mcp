"""Shopping list tools."""

from __future__ import annotations

from typing import Any

import httpx

from utils import normalize_unit, unit_family_and_factor


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
            were added successfully. After this call, consider running
            normalize_shopping_list(list_id) to collapse any ingredients that
            appear in different units across the added recipes (e.g. garlic in
            teaspoons from one recipe and tablespoons from another).
        """
        resp = await client.get(f"/api/households/shopping/lists/{list_id}")
        resp.raise_for_status()
        all_items: list[dict] = resp.json().get("listItems") or []

        lines = [f"=== Replace Shopping List from Recipes (list: {list_id}) ===", ""]

        deleted = 0
        if all_items:
            item_ids = [i["id"] for i in all_items if i.get("id")]
            del_resp = await client.request(
                "DELETE",
                "/api/households/shopping/items",
                params={"ids": item_ids},
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

    @mcp.tool()
    async def normalize_shopping_list(list_id: str, dry_run: bool = False) -> str:
        """
        Consolidate duplicate ingredients on a shopping list by merging items
        that reference the same food but use different (convertible) units.

        For each food with multiple line items, this tool:
        1. Groups items by measurement family (US volume, metric volume,
           imperial weight, metric weight).
        2. Within each family, converts all quantities to a common unit and
           sums them. Target unit is the most-used one; ties break toward the
           larger unit (e.g. tablespoon beats teaspoon) so quantities stay small.
        3. Keeps one surviving item with the summed quantity; deletes duplicates.

        Items whose units belong to different measurement families (e.g. "3 oz"
        weight vs "1 cup" volume of the same food, or whole-count vs volume),
        items with no food reference, or items with a null quantity, are left
        untouched and listed in the "Needs manual review" section.

        Run this after replace_shopping_list_from_recipes when the same
        ingredient appears in different units across multiple recipes.

        Args:
            list_id: UUID of the shopping list to normalize.
            dry_run: When True, report what would change without writing anything.

        Returns:
            Plain-text report of every merge performed (or planned in dry_run),
            followed by a "Needs manual review" section for items that cannot
            be merged automatically.
        """
        # ── 1. Fetch items via list detail (the items endpoint has no list filter)
        resp = await client.get(f"/api/households/shopping/lists/{list_id}")
        resp.raise_for_status()
        all_items: list[dict] = resp.json().get("listItems") or []

        lines = [
            f"=== Normalize Shopping List {'(DRY RUN)' if dry_run else '(LIVE)'} ===",
            f"List: {list_id}  ·  {len(all_items)} item(s) fetched",
            "",
        ]

        # ── 2. Group by food_id ───────────────────────────────────────────────
        food_groups: dict[str, list[dict]] = {}
        for item in all_items:
            food = item.get("food") or {}
            food_id = food.get("id") if isinstance(food, dict) else None
            if food_id:
                food_groups.setdefault(food_id, []).append(item)

        # ── 3. Process each multi-item food group ─────────────────────────────
        to_update: list[dict] = []
        to_delete: list[str] = []
        review_lines: list[str] = []
        merged_count = 0

        for _food_id, items in food_groups.items():
            if len(items) < 2:
                continue

            food_name = (items[0].get("food") or {}).get("name", _food_id)

            # Bucket each item into its unit family.
            family_groups: dict[str, list[dict]] = {}
            unclassified: list[dict] = []
            for item in items:
                unit_obj = item.get("unit") or {}
                unit_name = unit_obj.get("name", "") if isinstance(unit_obj, dict) else ""
                canonical = normalize_unit(unit_name) if unit_name else ""
                fam_info = unit_family_and_factor(canonical) if canonical else None
                if fam_info:
                    family_groups.setdefault(fam_info[0], []).append(item)
                else:
                    unclassified.append(item)

            # Flag foods that span multiple families — can't auto-merge.
            if len(family_groups) > 1 or (family_groups and unclassified):
                parts = [f"{len(v)} × {k}" for k, v in family_groups.items()]
                if unclassified:
                    parts.append(f"{len(unclassified)} × (count/no unit)")
                review_lines.append(
                    f"  '{food_name}': mixed families ({', '.join(parts)}) — merge manually"
                )

            # Merge within each single convertible family that has 2+ items.
            for family, fam_items in family_groups.items():
                if len(fam_items) < 2:
                    continue

                if any(i.get("quantity") is None for i in fam_items):
                    review_lines.append(
                        f"  '{food_name}' ({family}): some quantities are null — skipped"
                    )
                    continue

                # Pick target unit: most-used by count; tie-break = larger factor.
                unit_counts: dict[str, int] = {}
                for item in fam_items:
                    u = (item.get("unit") or {}).get("name", "")
                    can = normalize_unit(u) if u else ""
                    unit_counts[can] = unit_counts.get(can, 0) + 1

                target_canonical = max(
                    unit_counts,
                    key=lambda u: (
                        unit_counts[u],
                        (unit_family_and_factor(u) or ("", 0.0))[1],
                    ),
                )
                target_factor = (unit_family_and_factor(target_canonical) or ("", 1.0))[1]

                # Preserve the full unit object so Mealie keeps all its fields.
                target_unit_obj: dict | None = next(
                    (
                        item.get("unit")
                        for item in fam_items
                        if normalize_unit((item.get("unit") or {}).get("name", ""))
                        == target_canonical
                    ),
                    None,
                )

                # Sum in base units then convert back to target.
                total_base = 0.0
                for item in fam_items:
                    qty = float(item["quantity"])
                    u = (item.get("unit") or {}).get("name", "")
                    can = normalize_unit(u) if u else ""
                    factor = (unit_family_and_factor(can) or ("", 1.0))[1]
                    total_base += qty * factor
                total_target = round(total_base / target_factor, 6)

                unit_display = (
                    target_unit_obj.get("name", target_canonical)
                    if isinstance(target_unit_obj, dict)
                    else target_canonical
                )
                before = " + ".join(
                    f"{i.get('quantity')} {(i.get('unit') or {}).get('name', '?')}"
                    for i in fam_items
                )
                lines.append(
                    f"  MERGE  '{food_name}': {before}  →  {total_target} {unit_display}"
                )

                # Keep the item already on the target unit (or fall back to first).
                keep = next(
                    (
                        i for i in fam_items
                        if normalize_unit((i.get("unit") or {}).get("name", ""))
                        == target_canonical
                    ),
                    fam_items[0],
                )
                extras = [i for i in fam_items if i["id"] != keep["id"]]

                merged_count += 1
                to_delete.extend(i["id"] for i in extras)
                if not dry_run:
                    updated = dict(keep)
                    updated["quantity"] = total_target
                    updated["unit"] = target_unit_obj
                    to_update.append(updated)

        # ── 4. Apply changes ──────────────────────────────────────────────────
        updated_count = deleted_count = 0
        if not dry_run:
            for item in to_update:
                r = await client.put(
                    f"/api/households/shopping/items/{item['id']}", json=item
                )
                if r.is_success:
                    updated_count += 1
                else:
                    lines.append(
                        f"  WARN: update failed for item {item['id'][:8]}… — HTTP {r.status_code}"
                    )

            if to_delete:
                r = await client.request(
                    "DELETE", "/api/households/shopping/items", params={"ids": to_delete}
                )
                if r.is_success:
                    deleted_count = len(to_delete)
                else:
                    lines.append(f"  WARN: bulk delete returned HTTP {r.status_code}")

        suffix = (
            f" · {updated_count} updated · {deleted_count} deleted"
            if not dry_run
            else " (dry run — no changes written)"
        )
        lines += [
            "",
            f"Summary: {merged_count} food(s) consolidated{suffix}",
        ]

        if review_lines:
            lines += ["", "## Needs manual review", ""]
            lines += review_lines
            lines += [
                "",
                "Tip: use merge_foods to unify cross-unit duplicates at the food level,",
                "or adjust recipe ingredients so they share a consistent unit.",
            ]

        lines.append("\n=== Done ===")
        return "\n".join(lines)
