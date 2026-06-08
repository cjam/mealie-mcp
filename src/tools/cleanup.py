"""System cleanup tool — deduplicates and normalises foods and units."""

from __future__ import annotations

from typing import Any

import httpx

from utils import (
    STANDARD_UNITS,
    canonical_food,
    get_all,
    get_recipe,
    normalize_food,
    normalize_unit,
    put_recipe,
)


# ── Helpers used only by cleanup_system ───────────────────────────────────────

async def _backup(client: httpx.AsyncClient) -> str:
    try:
        resp = await client.post("/api/admin/backups")
        resp.raise_for_status()
        name = resp.json().get("name", resp.json().get("id", "unknown"))
        return f"created ({name})"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            return "skipped — insufficient permissions (admin token required)"
        return f"failed — HTTP {exc.response.status_code}"
    except Exception as exc:
        return f"failed — {exc}"


async def _merge_foods(client: httpx.AsyncClient, from_id: str, to_id: str) -> bool:
    try:
        r = await client.put("/api/foods/merge", json={"fromFood": from_id, "toFood": to_id})
        return r.is_success
    except Exception:
        return False


async def _merge_units(client: httpx.AsyncClient, from_id: str, to_id: str) -> bool:
    try:
        r = await client.put("/api/units/merge", json={"fromUnit": from_id, "toUnit": to_id})
        return r.is_success
    except Exception:
        return False


async def _rename(client: httpx.AsyncClient, path: str, item_id: str, name: str) -> bool:
    # Mealie's PUT handler writes every body field back including id;
    # omitting id causes SQLAlchemy to overwrite it with NULL.
    try:
        r = await client.put(f"{path}/{item_id}", json={"id": item_id, "name": name})
        return r.is_success
    except Exception:
        return False


async def _delete(client: httpx.AsyncClient, path: str, item_id: str) -> bool:
    try:
        r = await client.delete(f"{path}/{item_id}")
        return r.is_success
    except Exception:
        return False


async def _create_unit(client: httpx.AsyncClient, unit: dict) -> bool:
    try:
        r = await client.post("/api/units", json=unit)
        return r.is_success
    except Exception:
        return False


# ── Tool registration ──────────────────────────────────────────────────────────

def register_cleanup_tools(mcp: Any, client: httpx.AsyncClient) -> None:

    @mcp.tool()
    async def cleanup_system(dry_run: bool = False) -> str:
        """
        Perform a full system cleanup of the Mealie database.

        Steps performed in order:
          1. Create a database backup (requires an admin-level API token).
          2. Fetch all foods, group by case-insensitive name, merge duplicates,
             and normalize surviving names to Title Case.
          3. Fetch all units, group by canonical name (resolving common
             abbreviations and plural forms), merge duplicates, and create
             any standard cooking units that are absent.

        Args:
            dry_run: When True, report every planned change without writing
                     anything to the database. Useful for previewing impact.

        Returns:
            A plain-text report summarising every action taken (or planned).
        """
        lines: list[str] = [
            f"=== Mealie Cleanup System {'(DRY RUN)' if dry_run else '(LIVE)'} ===",
            "",
        ]

        # ── 1. Backup ──────────────────────────────────────────────────────────
        lines.append("## Backup")
        if dry_run:
            lines.append("  skipped (dry run)")
        else:
            lines.append(f"  {await _backup(client)}")
        lines.append("")

        # ── 2. Foods ───────────────────────────────────────────────────────────
        lines.append("## Foods")
        try:
            foods = await get_all(client, "/api/foods")
        except Exception as exc:
            lines.append(f"  ERROR fetching foods: {exc}")
            foods = []

        food_merges: list[tuple[str, str, str]] = []
        food_renames: list[tuple[str, str, str]] = []

        if foods:
            groups: dict[str, list[dict]] = {}
            for food in foods:
                groups.setdefault(normalize_food(food["name"]), []).append(food)

            for key, group in sorted(groups.items()):
                canon = canonical_food(key, [f["name"] for f in group])

                if len(group) > 1:
                    keep = next((f for f in group if f["name"] == f["name"].title()), group[0])
                    for dup in group:
                        if dup["id"] == keep["id"]:
                            continue
                        food_merges.append((dup["id"], keep["id"], canon))
                        lines.append(f"  MERGE  '{dup['name']}' → '{canon}'")
                    if keep["name"] != canon:
                        food_renames.append((keep["id"], keep["name"], canon))
                        lines.append(f"  RENAME '{keep['name']}' → '{canon}'")
                else:
                    food = group[0]
                    if food["name"] != canon:
                        food_renames.append((food["id"], food["name"], canon))
                        lines.append(f"  RENAME '{food['name']}' → '{canon}'")

            lines.append(
                f"  Summary: {len(foods)} foods · "
                f"{len(food_merges)} to merge · {len(food_renames)} to rename"
            )

            if not dry_run:
                merged = renamed = 0
                for item_id, old_name, canon in food_renames:
                    if await _rename(client, "/api/foods", item_id, canon):
                        renamed += 1
                    else:
                        lines.append(f"  WARN: rename failed for '{old_name}'")
                for dup_id, keep_id, canon in food_merges:
                    ok = await _merge_foods(client, dup_id, keep_id)
                    if not ok:
                        ok = await _delete(client, "/api/foods", dup_id)
                    if ok:
                        merged += 1
                    else:
                        lines.append(f"  WARN: merge/delete failed for food {dup_id[:8]}…")
                lines.append(f"  Applied: {merged} merged · {renamed} renamed")
        else:
            lines.append("  No foods found.")
        lines.append("")

        # ── 3. Units ───────────────────────────────────────────────────────────
        lines.append("## Units")
        try:
            units = await get_all(client, "/api/units")
        except Exception as exc:
            lines.append(f"  ERROR fetching units: {exc}")
            units = []

        unit_merges: list[tuple[str, str, str]] = []
        unit_renames: list[tuple[str, str, str]] = []
        existing_canonical: set[str] = set()

        if units:
            unit_groups: dict[str, list[dict]] = {}
            for unit in units:
                unit_groups.setdefault(normalize_unit(unit["name"]), []).append(unit)

            for canon, group in sorted(unit_groups.items()):
                existing_canonical.add(canon)

                if len(group) > 1:
                    keep = next((u for u in group if u["name"].lower() == canon), group[0])
                    for dup in group:
                        if dup["id"] == keep["id"]:
                            continue
                        unit_merges.append((dup["id"], keep["id"], canon))
                        lines.append(f"  MERGE  '{dup['name']}' → '{canon}'")
                    if keep["name"] != canon:
                        unit_renames.append((keep["id"], keep["name"], canon))
                        lines.append(f"  RENAME '{keep['name']}' → '{canon}'")
                else:
                    unit = group[0]
                    if unit["name"] != canon:
                        unit_renames.append((unit["id"], unit["name"], canon))
                        lines.append(f"  RENAME '{unit['name']}' → '{canon}'")

        missing = [u for u in STANDARD_UNITS if u["name"] not in existing_canonical]
        for u in missing:
            abbr = f" (abbr: {u['abbreviation']})" if u["abbreviation"] else ""
            lines.append(f"  ADD    '{u['name']}'{abbr}")

        lines.append(
            f"  Summary: {len(units)} units · "
            f"{len(unit_merges)} to merge · {len(unit_renames)} to rename · "
            f"{len(missing)} to add"
        )

        if not dry_run and units is not None:
            merged = renamed = added = 0
            for item_id, old_name, canon in unit_renames:
                if await _rename(client, "/api/units", item_id, canon):
                    renamed += 1
                else:
                    lines.append(f"  WARN: rename failed for '{old_name}'")
            for dup_id, keep_id, canon in unit_merges:
                ok = await _merge_units(client, dup_id, keep_id)
                if not ok:
                    ok = await _delete(client, "/api/units", dup_id)
                if ok:
                    merged += 1
                else:
                    lines.append(f"  WARN: merge/delete failed for unit {dup_id[:8]}…")
            for u in missing:
                payload = {
                    "name": u["name"],
                    "pluralName": u["pluralName"],
                    "abbreviation": u["abbreviation"],
                    "fraction": u["fraction"],
                }
                if await _create_unit(client, payload):
                    added += 1
                else:
                    lines.append(f"  WARN: failed to create unit '{u['name']}'")
            lines.append(f"  Applied: {merged} merged · {renamed} renamed · {added} added")
        lines.append("")

        lines.append("=== Done ===")
        return "\n".join(lines)

    @mcp.tool()
    async def merge_foods(keep_name: str, remove_name: str) -> str:
        """
        Merge two food entries by name.

        Redirects every recipe ingredient that references remove_name to keep_name,
        then removes the duplicate entry. Use this to manually resolve specific
        duplicates without running the full cleanup_system.

        Args:
            keep_name:   The food name to keep (the surviving entry).
            remove_name: The food name to absorb into keep_name (will be deleted).

        Returns:
            Success or error message.
        """
        foods = await get_all(client, "/api/foods")
        name_map = {normalize_food(f["name"]): f for f in foods}

        keep = name_map.get(normalize_food(keep_name))
        remove = name_map.get(normalize_food(remove_name))

        if keep is None:
            return f"ERROR: food '{keep_name}' not found in database"
        if remove is None:
            return f"ERROR: food '{remove_name}' not found in database"
        if keep["id"] == remove["id"]:
            return f"ERROR: '{keep_name}' and '{remove_name}' resolve to the same entry — nothing to merge"

        ok = await _merge_foods(client, remove["id"], keep["id"])
        if ok:
            return f"Merged '{remove['name']}' → '{keep['name']}' (removed {remove['id'][:8]}…)"
        return "ERROR: merge failed — check Mealie logs"

    @mcp.tool()
    async def merge_units(keep_name: str, remove_name: str) -> str:
        """
        Merge two unit entries by name.

        Redirects every recipe ingredient that references remove_name to keep_name,
        then removes the duplicate entry. Use this to manually resolve specific
        duplicates without running the full cleanup_system.

        Args:
            keep_name:   The unit name to keep (the surviving entry).
            remove_name: The unit name to absorb into keep_name (will be deleted).

        Returns:
            Success or error message.
        """
        units = await get_all(client, "/api/units")
        name_map = {normalize_unit(u["name"]): u for u in units}

        keep = name_map.get(normalize_unit(keep_name))
        remove = name_map.get(normalize_unit(remove_name))

        if keep is None:
            return f"ERROR: unit '{keep_name}' not found in database"
        if remove is None:
            return f"ERROR: unit '{remove_name}' not found in database"
        if keep["id"] == remove["id"]:
            return f"ERROR: '{keep_name}' and '{remove_name}' resolve to the same entry — nothing to merge"

        ok = await _merge_units(client, remove["id"], keep["id"])
        if ok:
            return f"Merged '{remove['name']}' → '{keep['name']}' (removed {remove['id'][:8]}…)"
        return "ERROR: merge failed — check Mealie logs"

    @mcp.tool()
    async def get_ingredient_normalization_report() -> str:
        """
        Scan all recipes and report foods that appear with more than one distinct
        unit, sorted by total recipe-use count descending.

        Use this to diagnose shopping list duplication caused by the same food
        being measured in different units across recipes (e.g. garlic in cloves
        in one recipe and teaspoons in another). Only foods with 2+ distinct
        units are included; single-unit foods are omitted.

        Each ingredient entry includes its referenceId so you can pass the results
        directly to normalize_ingredients without any additional lookups.

        Returns:
            Plain-text report listing each inconsistent food with food_id, each
            unit variant with unit_id, and per-ingredient recipe_slug + reference_id
            + quantity — followed by instructions for calling normalize_ingredients.
        """
        summaries = await get_all(client, "/api/recipes")

        # food_id → unit_id_or_None → [(recipe_slug, recipe_name, ref_id, quantity, note)]
        food_unit_map: dict[str, dict] = {}
        food_names: dict[str, str] = {}
        unit_names: dict[str, str] = {}

        skipped = 0
        for summary in summaries:
            slug = summary.get("slug") or summary.get("id", "")
            try:
                recipe = await get_recipe(client, slug)
            except Exception:
                skipped += 1
                continue

            recipe_slug = recipe.get("slug", slug)
            recipe_name = recipe.get("name", slug)

            for ing in recipe.get("recipeIngredient") or []:
                food_obj = ing.get("food") or {}
                food_id = food_obj.get("id")
                if not food_id:
                    continue

                food_names[food_id] = food_obj.get("name", food_id)

                unit_obj = ing.get("unit") or {}
                unit_id: str | None = unit_obj.get("id") or None
                if unit_id:
                    unit_names[unit_id] = unit_obj.get("name", unit_id)

                food_unit_map.setdefault(food_id, {})
                food_unit_map[food_id].setdefault(unit_id, []).append((
                    recipe_slug,
                    recipe_name,
                    ing.get("referenceId") or "",
                    ing.get("quantity"),
                    (ing.get("note") or "").strip(),
                ))

        multi_unit = {
            fid: units
            for fid, units in food_unit_map.items()
            if len(units) >= 2
        }

        if not multi_unit:
            return (
                "=== Ingredient Inconsistency Report ===\n\n"
                "No foods with multiple unit variants found.\n"
                "=== Done ==="
            )

        sorted_foods = sorted(
            multi_unit.items(),
            key=lambda kv: sum(len(v) for v in kv[1].values()),
            reverse=True,
        )

        lines = ["=== Ingredient Inconsistency Report ===", ""]
        lines.append(f"Foods with 2+ distinct units: {len(sorted_foods)}")
        if skipped:
            lines.append(f"Note: {skipped} recipe(s) skipped (fetch error)")
        lines.append("")

        for food_id, units in sorted_foods:
            food_name = food_names.get(food_id, food_id)
            total = sum(len(v) for v in units.values())
            lines.append(f"## {food_name}  (food_id: {food_id})")
            lines.append(f"   {len(units)} unit variant(s) across {total} use(s)")

            for unit_id, uses in units.items():
                uname = unit_names.get(unit_id, unit_id) if unit_id else "(no unit)"
                uid_str = unit_id if unit_id else "null"
                lines.append(f"   Unit: {uname}  (unit_id: {uid_str})")
                for recipe_slug, recipe_name, ref_id, qty, note in uses:
                    qty_str = str(qty) if qty is not None else "?"
                    example = f"{qty_str} {uname}".strip()
                    if note:
                        example += f", {note}"
                    lines.append(
                        f"     {recipe_name}  (slug: {recipe_slug})"
                        f"  ref: {ref_id}  — {example}"
                    )
            lines.append("")

        lines += [
            "## Instructions",
            "For each food above, pick a target unit and call normalize_ingredients().",
            "Use the ref and slug values from each ingredient line to build the conversions list.",
            "",
            "Steps:",
            "  1. Choose a target_unit_id for the food (usually the most common unit above).",
            "  2. For each ingredient NOT already on the target unit, calculate a conversion",
            "     factor so that: new_quantity = old_quantity × factor.",
            "     Example: 1 tsp minced garlic ≈ 0.5 cloves → factor = 0.5",
            "  3. Include every ingredient you want to change in the conversions list.",
            "     Ingredients already on the target unit can use factor=1.0 or be omitted.",
            "",
            "Call structure:",
            "  normalize_ingredients(",
            '    food_id        = "<food_id from ## header>",',
            '    target_unit_id = "<chosen unit_id>",',
            "    conversions    = [",
            '      {"recipe_slug": "<slug>", "reference_id": "<ref>", "factor": <float>},',
            "      ...",
            "    ]",
            "  )",
            "=== Done ===",
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def normalize_ingredients(
        food_id: str,
        target_unit_id: str | None,
        conversions: list[dict],
    ) -> str:
        """
        Normalize the unit on specific recipe ingredients and optionally rescale
        their quantities in one shot.

        This is the write step after get_ingredient_normalization_report. Each entry in
        conversions identifies one ingredient by recipe_slug + reference_id, sets its
        unit to target_unit_id, and multiplies its current quantity by factor.

        Ingredients not listed in conversions are not touched.

        Args:
            food_id:        The food's database ID (from get_ingredient_normalization_report).
                            Used only for reporting; the actual ingredient lookup is by
                            reference_id.
            target_unit_id: The unit ID to set on every listed ingredient. Pass null to
                            clear the unit (whole/uncounted items).
            conversions:    List of dicts, each containing:
                              recipe_slug  — recipe slug (from the report)
                              reference_id — ingredient referenceId (from the report)
                              factor       — multiply current quantity by this;
                                            use 1.0 when only the unit name changes

        Returns:
            Plain-text summary of each ingredient updated and the total count.
        """
        target_unit: dict | None = None
        if target_unit_id:
            try:
                r = await client.get(f"/api/units/{target_unit_id}")
                if r.is_success:
                    target_unit = r.json()
            except Exception:
                pass
            if target_unit is None:
                all_units = await get_all(client, "/api/units")
                target_unit = next((u for u in all_units if u.get("id") == target_unit_id), None)
            if target_unit is None:
                return f"ERROR: unit '{target_unit_id}' not found in database"

        target_name = target_unit["name"] if target_unit else "(no unit)"

        # Group conversions by recipe slug so we make one PUT per recipe.
        by_recipe: dict[str, list[dict]] = {}
        for conv in conversions:
            by_recipe.setdefault(conv.get("recipe_slug", ""), []).append(conv)

        lines = ["=== Normalize Ingredients ===", "", f"Target unit: {target_name}", ""]
        total_updated = 0

        for slug, recipe_convs in by_recipe.items():
            try:
                recipe = await get_recipe(client, slug)
            except Exception as exc:
                lines.append(f"  ERROR: could not fetch '{slug}': {exc}")
                continue

            recipe_name = recipe.get("name", slug)
            recipe_slug = recipe.get("slug", slug)
            ref_map = {conv["reference_id"]: conv for conv in recipe_convs}

            updated = 0
            for ing in recipe.get("recipeIngredient") or []:
                ref_id = ing.get("referenceId")
                if ref_id not in ref_map:
                    continue

                factor = float(ref_map[ref_id].get("factor", 1.0))
                old_qty = ing.get("quantity")

                ing["unit"] = target_unit
                if old_qty is not None and factor != 1.0:
                    ing["quantity"] = round(old_qty * factor, 6)

                updated += 1

            if updated:
                ok = await put_recipe(client, recipe_slug, recipe)
                status = "OK" if ok else "WARN: PUT failed"
                lines.append(f"  {recipe_name}: {updated} ingredient(s) → {target_name}  [{status}]")
                total_updated += updated
            else:
                lines.append(f"  {recipe_name}: no matching referenceIds found")

        lines += [
            "",
            f"=== Total: {total_updated} ingredient(s) normalized across {len(by_recipe)} recipe(s) ===",
        ]
        return "\n".join(lines)
