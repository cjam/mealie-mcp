"""Shopping list tools."""

from __future__ import annotations

import re
from typing import Any

import httpx

from utils import (
    find_or_create_food,
    find_or_create_unit,
    get_all,
    get_recipe,
    normalize_food,
    normalize_unit,
    resolve_recipe_id,
    unit_family_and_factor,
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def register_shopping_tools(mcp: Any, client: httpx.AsyncClient) -> None:

    @mcp.tool()
    async def replace_shopping_list_from_recipes(
        list_id: str,
        recipe_ids: list[str],
        scale: float = 1.0,
        include_optional: bool = False,
    ) -> str:
        """
        Replace all items in a shopping list with the ingredients from a set of recipes.

        Steps:
          1. Fetch all current items on the list.
          2. Bulk-delete them.
          3. For each recipe, call Mealie's ingredient-expansion endpoint
             (POST /api/households/shopping/lists/{id}/recipe/{recipe_id}) so Mealie
             handles unit conversion, quantity scaling, and ingredient deduplication.
          4. Unless include_optional=True, remove any items whose ingredient was
             marked optional — either by an "optional" note on the ingredient, or by
             being under a section title that starts with "Optional".

        When the same food is required in one recipe and optional in another, it is
        kept (required wins). This makes it safe to pass several recipes at once.

        Args:
            list_id:          UUID of the shopping list to replace.
            recipe_ids:       Ordered list of recipe UUIDs or slugs whose ingredients to add.
            scale:            Serving multiplier applied to every recipe (default 1.0).
            include_optional: When True, all ingredients are added regardless of their
                              optional status. Default False skips optional ingredients.

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

        # Pre-collect optional/required food IDs so we can filter after adding.
        truly_optional: set[str] = set()
        if not include_optional:
            required_food_ids: set[str] = set()
            optional_food_ids: set[str] = set()
            for slug_or_id in recipe_ids:
                try:
                    recipe_data = await get_recipe(client, slug_or_id)
                except Exception:
                    continue
                in_optional_section = False
                for ing in recipe_data.get("recipeIngredient") or []:
                    title = ing.get("title") or ""
                    if title:
                        in_optional_section = title.lower().startswith("optional")
                        continue
                    food_id = (ing.get("food") or {}).get("id")
                    if not food_id:
                        continue
                    note = (ing.get("note") or "").lower()
                    is_opt = in_optional_section or "optional" in note
                    if is_opt:
                        optional_food_ids.add(food_id)
                    else:
                        required_food_ids.add(food_id)
            # Required in any recipe trumps optional in any recipe
            truly_optional = optional_food_ids - required_food_ids

        added = 0
        params = {"recipeIncrements": scale} if scale != 1.0 else {}
        for slug_or_id in recipe_ids:
            try:
                recipe_id = await resolve_recipe_id(client, slug_or_id)
            except Exception:
                lines.append(f"  WARN: could not resolve recipe '{slug_or_id}' — skipping")
                continue
            r = await client.post(
                f"/api/households/shopping/lists/{list_id}/recipe/{recipe_id}",
                params=params or None,
            )
            if r.is_success:
                added += 1
            else:
                lines.append(f"  WARN: add recipe {slug_or_id} failed — HTTP {r.status_code}")
        lines.append(f"  Added {added}/{len(recipe_ids)} recipes")

        if truly_optional:
            final_resp = await client.get(f"/api/households/shopping/lists/{list_id}")
            if final_resp.is_success:
                final_items = final_resp.json().get("listItems") or []
                opt_ids = [
                    i["id"] for i in final_items
                    if (i.get("food") or {}).get("id") in truly_optional
                ]
                if opt_ids:
                    del_r = await client.request(
                        "DELETE", "/api/households/shopping/items", params={"ids": opt_ids}
                    )
                    if del_r.is_success:
                        lines.append(f"  Removed {len(opt_ids)} optional ingredient(s)")
                    else:
                        lines.append(
                            f"  WARN: could not remove optional items — HTTP {del_r.status_code}"
                        )

        lines.append("\n=== Done ===")
        return "\n".join(lines)

    @mcp.tool()
    async def get_shopping_list_items(list_id: str) -> str:
        """
        Return a compact item table for a shopping list.

        Much lighter than fetching the full list detail — returns only id, food
        name, quantity, unit, note, and checked status for each item. Use this to
        verify list state, look up item UUIDs for targeted edits, or confirm an
        adjust_shopping_list_items call took effect.

        Args:
            list_id: UUID of the shopping list.

        Returns:
            Plain-text table: one line per item, id · checked · food · qty · unit.
        """
        resp = await client.get(f"/api/households/shopping/lists/{list_id}")
        resp.raise_for_status()
        data = resp.json()
        items: list[dict] = data.get("listItems") or []
        list_name = data.get("name", list_id)

        lines = [f"=== Shopping List: {list_name} ({len(items)} item(s)) ===", ""]
        if not items:
            lines.append("  (empty)")
            return "\n".join(lines)

        for item in items:
            item_id = item.get("id", "?")
            food_name = (item.get("food") or {}).get("name") or item.get("note") or "(no food)"
            qty = item.get("quantity")
            unit_name = (item.get("unit") or {}).get("name") or ""
            note = item.get("note") or ""
            checked = "✓" if item.get("checked") else "○"
            qty_str = f"{qty} {unit_name}".strip() if qty is not None else ""
            display = f"{checked} {food_name}"
            if qty_str:
                display += f"  {qty_str}"
            if note and note != food_name:
                display += f"  ({note})"
            lines.append(f"  {item_id}  {display}")

        return "\n".join(lines)

    @mcp.tool()
    async def adjust_shopping_list_items(
        list_id: str,
        add: list[dict] | None = None,
        update: list[dict] | None = None,
        remove: list[str] | None = None,
    ) -> str:
        """
        Add, update, or remove items on a shopping list in one call.

        All operations accept food and unit names — ID resolution and food/unit
        creation are handled internally, matching the same pattern as the recipe
        ingredient tools. Items can also be identified by UUID for precise targeting.

        add entries (dict):
          food      (str, required)  food name — created in Mealie if missing
          quantity  (float)          optional
          unit      (str)            unit name — created if missing; optional
          note      (str)            free-text note; optional

        update entries (dict) — item identified by food name OR id:
          food      (str)    first unchecked item on the list whose food matches this name
          id        (str)    specific item UUID (takes precedence over food)
          quantity  (float)  new quantity; optional
          unit      (str)    new unit name; optional
          note      (str)    new note; optional

        remove entries (str): food name — removes all unchecked matching items;
          or item UUID — removes that specific item.

        Args:
            list_id: UUID of the shopping list to modify.
            add:     Items to add.
            update:  Items to update in place.
            remove:  Food names or item UUIDs to remove.

        Returns:
            Plain-text report of every operation and its outcome.
        """
        lines = ["=== Adjust Shopping List Items ===", ""]

        # Fetch existing items once for update/remove lookups
        existing_items: list[dict] = []
        id_to_item: dict[str, dict] = {}
        food_name_to_items: dict[str, list[dict]] = {}
        if update or remove:
            r = await client.get(f"/api/households/shopping/lists/{list_id}")
            r.raise_for_status()
            existing_items = r.json().get("listItems") or []
            for item in existing_items:
                iid = item.get("id")
                if iid:
                    id_to_item[iid] = item
                fname = (item.get("food") or {}).get("name", "")
                if fname:
                    food_name_to_items.setdefault(fname.lower(), []).append(item)

        food_map: dict[str, dict] = {}
        unit_map: dict[str, dict] = {}

        async def _ensure_foods() -> None:
            if not food_map:
                foods = await get_all(client, "/api/foods")
                food_map.update({normalize_food(f["name"]): f for f in foods})

        async def _ensure_units() -> None:
            if not unit_map:
                units = await get_all(client, "/api/units")
                unit_map.update({normalize_unit(u["name"]): u for u in units})

        # ── ADD ───────────────────────────────────────────────────────────────
        if add:
            lines.append("## Add")
            await _ensure_foods()
            for entry in add:
                food_name = entry.get("food", "")
                unit_name = entry.get("unit", "")
                quantity = entry.get("quantity")
                note = entry.get("note", "")

                if not food_name and not note:
                    lines.append(f"  WARN: skipping entry with no food or note: {entry}")
                    continue

                payload: dict = {
                    "shoppingListId": list_id,
                    "quantity": quantity,
                    "note": note,
                    "checked": False,
                }
                label_parts: list[str] = []

                if food_name:
                    food, action = await find_or_create_food(client, food_name, food_map)
                    if food is None:
                        lines.append(f"  FAILED: could not find or create food '{food_name}'")
                        continue
                    payload["foodId"] = food["id"]
                    label_parts.append(f"'{food['name']}' [{action}]")

                if unit_name:
                    await _ensure_units()
                    unit, u_action = await find_or_create_unit(client, unit_name, unit_map)
                    if unit:
                        payload["unitId"] = unit["id"]
                        label_parts.append(f"unit '{unit['name']}' [{u_action}]")
                    else:
                        label_parts.append(f"unit '{unit_name}' [FAILED]")

                if quantity is not None:
                    label_parts.append(f"qty={quantity}")

                try:
                    r = await client.post("/api/households/shopping/items", json=payload)
                    if r.is_success:
                        lines.append(f"  ADDED  {' | '.join(label_parts)}")
                    else:
                        lines.append(f"  FAILED (HTTP {r.status_code})  {' | '.join(label_parts)}")
                except Exception as exc:
                    lines.append(f"  ERROR  {' | '.join(label_parts)}: {exc}")
            lines.append("")

        # ── UPDATE ────────────────────────────────────────────────────────────
        if update:
            lines.append("## Update")
            await _ensure_units()
            for entry in update:
                entry_id = entry.get("id", "")
                food_name = entry.get("food", "")

                target: dict | None = None
                if entry_id:
                    target = id_to_item.get(entry_id)
                    if target is None:
                        lines.append(f"  FAILED: item '{entry_id[:8]}…' not found on list")
                        continue
                elif food_name:
                    matches = [
                        i for i in food_name_to_items.get(food_name.lower(), [])
                        if not i.get("checked")
                    ]
                    if not matches:
                        lines.append(f"  FAILED: no unchecked item with food '{food_name}' on list")
                        continue
                    target = matches[0]
                else:
                    lines.append(f"  WARN: skipping update with no id or food: {entry}")
                    continue

                target_id = target["id"]
                updated = dict(target)
                label_parts = [food_name or entry_id[:8] + "…"]

                if "quantity" in entry:
                    updated["quantity"] = entry["quantity"]
                    label_parts.append(f"qty={entry['quantity']}")

                if "unit" in entry:
                    unit, u_action = await find_or_create_unit(client, entry["unit"], unit_map)
                    if unit:
                        updated["unit"] = unit
                        updated["unitId"] = unit["id"]
                        label_parts.append(f"unit '{unit['name']}' [{u_action}]")
                    else:
                        label_parts.append(f"unit '{entry['unit']}' [FAILED]")

                if "note" in entry:
                    updated["note"] = entry["note"]

                try:
                    r = await client.put(
                        f"/api/households/shopping/items/{target_id}", json=updated
                    )
                    if r.is_success:
                        lines.append(f"  UPDATED  {' | '.join(label_parts)}")
                    else:
                        lines.append(f"  FAILED (HTTP {r.status_code})  {' | '.join(label_parts)}")
                except Exception as exc:
                    lines.append(f"  ERROR  {' | '.join(label_parts)}: {exc}")
            lines.append("")

        # ── REMOVE ────────────────────────────────────────────────────────────
        if remove:
            lines.append("## Remove")
            ids_to_delete: list[str] = []
            for ref in remove:
                if _UUID_RE.match(ref) and ref in id_to_item:
                    ids_to_delete.append(ref)
                    food_display = (id_to_item[ref].get("food") or {}).get("name", ref[:8])
                    lines.append(f"  REMOVE (by id)  '{food_display}'")
                else:
                    matches = [
                        i for i in food_name_to_items.get(ref.lower(), [])
                        if not i.get("checked")
                    ]
                    if not matches:
                        lines.append(f"  WARN: no unchecked item matching '{ref}'")
                    else:
                        ids_to_delete.extend(i["id"] for i in matches)
                        lines.append(f"  REMOVE  '{ref}' ({len(matches)} item(s))")

            if ids_to_delete:
                try:
                    r = await client.request(
                        "DELETE", "/api/households/shopping/items",
                        params={"ids": ids_to_delete},
                    )
                    if not r.is_success:
                        lines.append(f"  WARN: bulk delete returned HTTP {r.status_code}")
                except Exception as exc:
                    lines.append(f"  ERROR during delete: {exc}")

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
