"""Higher-level composed tools for Mealie data maintenance."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("mealie-mcp.cleanup")

# Standard units that a well-configured Mealie instance should have.
# Grouped logically; "fraction" controls whether Mealie renders them as fractions.
STANDARD_UNITS: list[dict[str, Any]] = [
    # ── US Volume ──────────────────────────────────────────────────────────────
    {"name": "teaspoon",     "abbreviation": "tsp",   "pluralName": "teaspoons",     "fraction": True},
    {"name": "tablespoon",   "abbreviation": "tbsp",  "pluralName": "tablespoons",   "fraction": True},
    {"name": "cup",          "abbreviation": "c",     "pluralName": "cups",          "fraction": True},
    {"name": "fluid ounce",  "abbreviation": "fl oz", "pluralName": "fluid ounces",  "fraction": True},
    {"name": "pint",         "abbreviation": "pt",    "pluralName": "pints",         "fraction": True},
    {"name": "quart",        "abbreviation": "qt",    "pluralName": "quarts",        "fraction": True},
    {"name": "gallon",       "abbreviation": "gal",   "pluralName": "gallons",       "fraction": True},
    # ── Metric Volume ─────────────────────────────────────────────────────────
    {"name": "milliliter",   "abbreviation": "ml",    "pluralName": "milliliters",   "fraction": False},
    {"name": "liter",        "abbreviation": "L",     "pluralName": "liters",        "fraction": False},
    # ── Imperial Weight ───────────────────────────────────────────────────────
    {"name": "ounce",        "abbreviation": "oz",    "pluralName": "ounces",        "fraction": True},
    {"name": "pound",        "abbreviation": "lb",    "pluralName": "pounds",        "fraction": True},
    # ── Metric Weight ─────────────────────────────────────────────────────────
    {"name": "milligram",    "abbreviation": "mg",    "pluralName": "milligrams",    "fraction": False},
    {"name": "gram",         "abbreviation": "g",     "pluralName": "grams",         "fraction": False},
    {"name": "kilogram",     "abbreviation": "kg",    "pluralName": "kilograms",     "fraction": False},
    # ── Count / Descriptive ───────────────────────────────────────────────────
    {"name": "whole",        "abbreviation": "",      "pluralName": "whole",         "fraction": False},
    {"name": "piece",        "abbreviation": "pc",    "pluralName": "pieces",        "fraction": False},
    {"name": "clove",        "abbreviation": "",      "pluralName": "cloves",        "fraction": False},
    {"name": "bunch",        "abbreviation": "",      "pluralName": "bunches",       "fraction": False},
    {"name": "slice",        "abbreviation": "",      "pluralName": "slices",        "fraction": False},
    {"name": "sprig",        "abbreviation": "",      "pluralName": "sprigs",        "fraction": False},
    {"name": "head",         "abbreviation": "",      "pluralName": "heads",         "fraction": False},
    {"name": "ear",          "abbreviation": "",      "pluralName": "ears",          "fraction": False},
    {"name": "stalk",        "abbreviation": "",      "pluralName": "stalks",        "fraction": False},
    {"name": "pinch",        "abbreviation": "",      "pluralName": "pinches",       "fraction": False},
    {"name": "dash",         "abbreviation": "",      "pluralName": "dashes",        "fraction": False},
    {"name": "drop",         "abbreviation": "",      "pluralName": "drops",         "fraction": False},
    {"name": "handful",      "abbreviation": "",      "pluralName": "handfuls",      "fraction": False},
    {"name": "can",          "abbreviation": "",      "pluralName": "cans",          "fraction": False},
    {"name": "package",      "abbreviation": "pkg",   "pluralName": "packages",      "fraction": False},
    {"name": "bag",          "abbreviation": "",      "pluralName": "bags",          "fraction": False},
    {"name": "container",    "abbreviation": "",      "pluralName": "containers",    "fraction": False},
    {"name": "bottle",       "abbreviation": "",      "pluralName": "bottles",       "fraction": False},
    {"name": "jar",          "abbreviation": "",      "pluralName": "jars",          "fraction": False},
    {"name": "strip",        "abbreviation": "",      "pluralName": "strips",        "fraction": False},
    {"name": "sheet",        "abbreviation": "",      "pluralName": "sheets",        "fraction": False},
    {"name": "inch",         "abbreviation": "in",    "pluralName": "inches",        "fraction": False},
]

# Maps known abbreviations and plural/variant spellings → canonical lowercase unit name.
# This drives duplicate detection for units.
_UNIT_ALIASES: dict[str, str] = {
    # teaspoon
    "tsp": "teaspoon", "tsps": "teaspoon", "t": "teaspoon", "teaspoons": "teaspoon",
    # tablespoon
    "tbsp": "tablespoon", "tbsps": "tablespoon", "tbs": "tablespoon", "tb": "tablespoon",
    "tablespoons": "tablespoon",
    # cup
    "c": "cup", "cups": "cup",
    # fluid ounce
    "fl oz": "fluid ounce", "fl. oz.": "fluid ounce", "floz": "fluid ounce",
    "fluid ounces": "fluid ounce",
    # pint / quart / gallon
    "pt": "pint", "pts": "pint", "pints": "pint",
    "qt": "quart", "qts": "quart", "quarts": "quart",
    "gal": "gallon", "gals": "gallon", "gallons": "gallon",
    # metric volume
    "ml": "milliliter", "mL": "milliliter",
    "milliliters": "milliliter", "millilitres": "milliliter",
    "l": "liter", "L": "liter", "liters": "liter", "litres": "liter", "litre": "liter",
    # imperial weight
    "oz": "ounce", "ozs": "ounce", "ounces": "ounce",
    "lb": "pound", "lbs": "pound", "pounds": "pound",
    # metric weight
    "mg": "milligram", "mgs": "milligram", "milligrams": "milligram",
    "g": "gram", "gs": "gram", "grams": "gram",
    "kg": "kilogram", "kgs": "kilogram", "kilograms": "kilogram",
    # count / descriptive (plural forms only — singular is already canonical)
    "cloves": "clove", "bunches": "bunch", "slices": "slice", "sprigs": "sprig",
    "heads": "head", "ears": "ear", "stalks": "stalk", "pinches": "pinch",
    "dashes": "dash", "drops": "drop", "handfuls": "handful", "handfull": "handful",
    "pieces": "piece", "pcs": "piece", "pc": "piece",
    "cans": "can", "pkg": "package", "pkgs": "package", "packages": "package",
    "bags": "bag", "containers": "container", "bottles": "bottle", "jars": "jar",
    "strips": "strip", "sheets": "sheet",
    "in": "inch", "ins": "inch", "inches": "inch",
}


# ── Normalization helpers ──────────────────────────────────────────────────────

def _normalize_food(name: str) -> str:
    """Lowercase + strip; groups same-name foods with different casing."""
    return name.strip().lower()


def _canonical_food(normalized_key: str, originals: list[str]) -> str:
    """Return the preferred display name (Title Case) for a food group."""
    for n in originals:
        if n.strip() == n.strip().title():
            return n.strip()
    return normalized_key.title()


def _normalize_unit(name: str) -> str:
    """Map abbreviations/variants to a canonical lowercase unit name."""
    raw = name.strip()
    if raw in _UNIT_ALIASES:
        return _UNIT_ALIASES[raw]
    lower = raw.lower()
    return _UNIT_ALIASES.get(lower, lower)


# ── API helpers ────────────────────────────────────────────────────────────────

async def _get_all(client: httpx.AsyncClient, path: str) -> list[dict]:
    """Collect all items from a paginated Mealie endpoint."""
    items: list[dict] = []
    page = 1
    while True:
        resp = await client.get(path, params={"page": page, "perPage": 100})
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("items", []))
        total = data.get("totalPages") or data.get("total_pages", 1)
        if page >= total:
            break
        page += 1
    return items


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


# ── Recipe cleanup helpers ────────────────────────────────────────────────────


async def _get_recipe(client: httpx.AsyncClient, slug: str) -> dict:
    resp = await client.get(f"/api/recipes/{slug}")
    resp.raise_for_status()
    return resp.json()


async def _put_recipe(client: httpx.AsyncClient, slug: str, recipe: dict) -> bool:
    try:
        r = await client.put(f"/api/recipes/{slug}", json=recipe)
        return r.is_success
    except Exception:
        return False


async def _parse_ingredient_text(client: httpx.AsyncClient, text: str) -> dict | None:
    """Call Mealie's NLP parser. Returns the parsed ingredient dict or None."""
    try:
        r = await client.post("/api/parser/ingredient", json={"ingredient": text})
        if r.is_success:
            return r.json().get("ingredient")
    except Exception:
        pass
    return None


async def _find_or_create_food(
    client: httpx.AsyncClient,
    name: str,
    food_map: dict[str, dict],
) -> tuple[dict | None, str]:
    """Return (food_object, "found"|"created"|"failed")."""
    normalized = _normalize_food(name)
    if normalized in food_map:
        return food_map[normalized], "found"
    display = name.strip().title()
    try:
        r = await client.post("/api/foods", json={"name": display})
        if r.is_success:
            food = r.json()
            food_map[_normalize_food(food["name"])] = food
            return food, "created"
    except Exception:
        pass
    return None, "failed"


async def _find_or_create_unit(
    client: httpx.AsyncClient,
    name: str,
    unit_map: dict[str, dict],
) -> tuple[dict | None, str]:
    """Return (unit_object, "found"|"created"|"failed")."""
    canonical = _normalize_unit(name)
    if canonical in unit_map:
        return unit_map[canonical], "found"
    std = next((u for u in STANDARD_UNITS if u["name"] == canonical), None)
    payload: dict[str, Any] = std or {
        "name": canonical,
        "pluralName": canonical + "s",
        "abbreviation": "",
        "fraction": False,
    }
    try:
        r = await client.post("/api/units", json=payload)
        if r.is_success:
            unit = r.json()
            unit_map[_normalize_unit(unit["name"])] = unit
            return unit, "created"
    except Exception:
        pass
    return None, "failed"


async def _cleanup_recipe_impl(client: httpx.AsyncClient, recipe_slug: str) -> str:
    try:
        recipe = await _get_recipe(client, recipe_slug)
    except Exception as exc:
        return f"ERROR: could not fetch recipe '{recipe_slug}': {exc}"

    recipe_name = recipe.get("name", recipe_slug)
    lines: list[str] = [f"=== Recipe Cleanup: {recipe_name} ===", ""]

    foods = await _get_all(client, "/api/foods")
    units = await _get_all(client, "/api/units")
    food_map = {_normalize_food(f["name"]): f for f in foods}
    unit_map = {_normalize_unit(u["name"]): u for u in units}

    lines.append("## Ingredient Resolution")
    ingredients: list[dict] = recipe.get("recipeIngredient") or []
    changes = 0

    for ing in ingredients:
        food_obj = ing.get("food")
        unit_obj = ing.get("unit")
        food_id   = (food_obj or {}).get("id")   if isinstance(food_obj, dict) else None
        food_name = (food_obj or {}).get("name") if isinstance(food_obj, dict) else None
        unit_id   = (unit_obj or {}).get("id")   if isinstance(unit_obj, dict) else None
        unit_name = (unit_obj or {}).get("name") if isinstance(unit_obj, dict) else None
        note = (ing.get("note") or "").strip()

        parts: list[str] = []

        # Case 1: URL-import result — food/unit are null, raw text is in note.
        # Use Mealie's NLP parser to extract food and unit names.
        if food_obj is None and note:
            parsed = await _parse_ingredient_text(client, note)
            if parsed:
                p_food = parsed.get("food") or {}
                p_unit = parsed.get("unit") or {}
                food_name = p_food.get("name") if isinstance(p_food, dict) else None
                unit_name = p_unit.get("name") if isinstance(p_unit, dict) else None
                p_qty = parsed.get("quantity")

                if food_name:
                    food, action = await _find_or_create_food(client, food_name, food_map)
                    if food:
                        ing["food"] = food
                        if p_qty:
                            ing["quantity"] = p_qty
                        parts.append(f"food '{food['name']}' [{action}]")
                        changes += 1
                    else:
                        parts.append(f"food '{food_name}' [FAILED]")

                if unit_name:
                    unit, action = await _find_or_create_unit(client, unit_name, unit_map)
                    if unit:
                        ing["unit"] = unit
                        parts.append(f"unit '{unit['name']}' [{action}]")
                        changes += 1
                    else:
                        parts.append(f"unit '{unit_name}' [FAILED]")

        # Case 2: food/unit objects present but not yet DB-linked (partial objects).
        elif food_name and not food_id:
            food, action = await _find_or_create_food(client, food_name, food_map)
            if food:
                ing["food"] = food
                parts.append(f"food '{food['name']}' [{action}]")
                changes += 1
            else:
                parts.append(f"food '{food_name}' [FAILED]")

            if unit_name and not unit_id:
                unit, action = await _find_or_create_unit(client, unit_name, unit_map)
                if unit:
                    ing["unit"] = unit
                    parts.append(f"unit '{unit['name']}' [{action}]")
                    changes += 1
                else:
                    parts.append(f"unit '{unit_name}' [FAILED]")

        if parts:
            qty   = ing.get("quantity") or ""
            uname = (ing.get("unit") or {}).get("name") or ""
            fname = (ing.get("food") or {}).get("name") or note or "?"
            display = f"{qty} {uname} {fname}".strip()
            lines.append(f"  {display}: {' | '.join(parts)}")

    if changes:
        lines.append("")
        lines.append(f"  Patching recipe ({changes} references resolved)…")
        ok = await _put_recipe(client, recipe_slug, recipe)
        lines.append("  " + ("PATCH OK" if ok else "WARN: patch failed — check Mealie logs"))
        if ok:
            # Mealie regenerates step IDs on PUT — re-fetch to get the current IDs.
            try:
                recipe = await _get_recipe(client, recipe_slug)
                ingredients = recipe.get("recipeIngredient") or []
            except Exception:
                pass
    else:
        lines.append("  All ingredient references already linked; no changes needed.")

    lines += [
        "",
        "## Ready for Step Linking",
        "Set `ingredientReferences` on each step by matching the referenceIds below.",
        "",
        "Ingredients:",
    ]
    for ing in ingredients:
        ref   = ing.get("referenceId", "?")
        qty   = ing.get("quantity") or ""
        uname = (ing.get("unit") or {}).get("name") or ""
        fname = (ing.get("food") or {}).get("name") or (ing.get("note") or "").strip() or "?"
        qty_str = f"{qty} {uname}".strip()
        lines.append(f"  [{ref}]  {qty_str} {fname}".rstrip())

    lines += ["", "Steps:"]
    for step in recipe.get("recipeInstructions") or []:
        sid  = step.get("id", "?")
        text = (step.get("text") or "").replace("\n", " ")[:150]
        lines.append(f"  [{sid}]  {text}")

    lines += ["", "=== Done ==="]
    return "\n".join(lines)


# ── Tool registration ──────────────────────────────────────────────────────────

def register_cleanup_tools(mcp: Any, client: httpx.AsyncClient) -> None:
    """Attach higher-level composed tools to an existing FastMCP instance."""

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
            foods = await _get_all(client, "/api/foods")
        except Exception as exc:
            lines.append(f"  ERROR fetching foods: {exc}")
            foods = []

        food_merges: list[tuple[str, str, str]] = []   # (dup_id, keep_id, canonical)
        food_renames: list[tuple[str, str, str]] = []  # (id, old_name, canonical)

        if foods:
            groups: dict[str, list[dict]] = {}
            for food in foods:
                groups.setdefault(_normalize_food(food["name"]), []).append(food)

            for key, group in sorted(groups.items()):
                canonical = _canonical_food(key, [f["name"] for f in group])

                if len(group) > 1:
                    # Prefer any entry already in Title Case as the one to keep
                    keep = next((f for f in group if f["name"] == f["name"].title()), group[0])
                    for dup in group:
                        if dup["id"] == keep["id"]:
                            continue
                        food_merges.append((dup["id"], keep["id"], canonical))
                        lines.append(f"  MERGE  '{dup['name']}' → '{canonical}'")
                    if keep["name"] != canonical:
                        food_renames.append((keep["id"], keep["name"], canonical))
                        lines.append(f"  RENAME '{keep['name']}' → '{canonical}'")
                else:
                    food = group[0]
                    if food["name"] != canonical:
                        food_renames.append((food["id"], food["name"], canonical))
                        lines.append(f"  RENAME '{food['name']}' → '{canonical}'")

            lines.append(
                f"  Summary: {len(foods)} foods · "
                f"{len(food_merges)} to merge · {len(food_renames)} to rename"
            )

            if not dry_run:
                merged = renamed = 0
                for item_id, old_name, canonical in food_renames:
                    if await _rename(client, "/api/foods", item_id, canonical):
                        renamed += 1
                    else:
                        lines.append(f"  WARN: rename failed for '{old_name}'")
                for dup_id, keep_id, canonical in food_merges:
                    ok = await _merge_foods(client, dup_id, keep_id)
                    if not ok:
                        # Fallback: delete the duplicate outright
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
            units = await _get_all(client, "/api/units")
        except Exception as exc:
            lines.append(f"  ERROR fetching units: {exc}")
            units = []

        unit_merges: list[tuple[str, str, str]] = []   # (dup_id, keep_id, canonical)
        unit_renames: list[tuple[str, str, str]] = []  # (id, old_name, canonical)

        existing_canonical: set[str] = set()

        if units:
            unit_groups: dict[str, list[dict]] = {}
            for unit in units:
                unit_groups.setdefault(_normalize_unit(unit["name"]), []).append(unit)

            for canonical, group in sorted(unit_groups.items()):
                existing_canonical.add(canonical)

                if len(group) > 1:
                    keep = next((u for u in group if u["name"].lower() == canonical), group[0])
                    for dup in group:
                        if dup["id"] == keep["id"]:
                            continue
                        unit_merges.append((dup["id"], keep["id"], canonical))
                        lines.append(f"  MERGE  '{dup['name']}' → '{canonical}'")
                    if keep["name"] != canonical:
                        unit_renames.append((keep["id"], keep["name"], canonical))
                        lines.append(f"  RENAME '{keep['name']}' → '{canonical}'")
                else:
                    unit = group[0]
                    if unit["name"] != canonical:
                        unit_renames.append((unit["id"], unit["name"], canonical))
                        lines.append(f"  RENAME '{unit['name']}' → '{canonical}'")

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
            for item_id, old_name, canonical in unit_renames:
                if await _rename(client, "/api/units", item_id, canonical):
                    renamed += 1
                else:
                    lines.append(f"  WARN: rename failed for '{old_name}'")
            for dup_id, keep_id, canonical in unit_merges:
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
    async def cleanup_recipe(recipe_slug: str) -> str:
        """
        Enrich a recipe by resolving its ingredients against the Mealie food and
        unit databases, creating any missing entries.

        Call this after importing a recipe from a URL. It will:
          1. Fetch the full recipe.
          2. For every ingredient that has a food name but no food ID, look up or
             create the food in the Mealie food database and link it.
          3. For every ingredient that has a unit name but no unit ID, look up or
             create the unit in the Mealie unit database and link it.
          4. PUT the recipe back with the resolved ingredient references.
          5. Return a structured summary showing each ingredient's referenceId and
             each instruction step's ID + text — ready for you to determine which
             ingredients belong in which steps and call the recipe update endpoint
             to set ingredientReferences on each step.

        Args:
            recipe_slug: The recipe slug or ID (visible in the Mealie URL or
                         returned by the recipe create/import endpoints).

        Returns:
            Plain-text report of what was resolved/created, followed by a
            listing of ingredient referenceIds and step IDs for step linking.
        """
        return await _cleanup_recipe_impl(client, recipe_slug)

    @mcp.tool()
    async def link_recipe_steps(recipe_slug: str, step_ingredient_map: dict[str, list[str]]) -> str:
        """
        Set ingredient references on recipe instruction steps.

        Call this after cleanup_recipe once you have decided which ingredients
        belong to each step. It fetches the recipe, applies your mapping, and
        PUTs it back.

        Args:
            recipe_slug: The recipe slug (same value passed to cleanup_recipe).
            step_ingredient_map: Dict mapping each step ID to the list of
                ingredient referenceIds that appear in that step. Steps omitted
                from the map are left unchanged. Pass an empty list for a step
                to clear its references.

        Returns:
            Plain-text summary of every step updated and the final PATCH status.
        """
        try:
            recipe = await _get_recipe(client, recipe_slug)
        except Exception as exc:
            return f"ERROR: could not fetch recipe '{recipe_slug}': {exc}"

        recipe_name = recipe.get("name", recipe_slug)
        lines: list[str] = [f"=== Step Linking: {recipe_name} ===", ""]

        steps: list[dict] = recipe.get("recipeInstructions") or []
        applied = 0
        unknown_steps = set(step_ingredient_map) - {s["id"] for s in steps}
        if unknown_steps:
            for sid in sorted(unknown_steps):
                lines.append(f"  WARN: step ID not found in recipe — {sid}")

        for step in steps:
            sid = step.get("id")
            if sid not in step_ingredient_map:
                continue
            ref_ids = step_ingredient_map[sid]
            step["ingredientReferences"] = [{"referenceId": r} for r in ref_ids]
            text_preview = (step.get("text") or "")[:80].replace("\n", " ")
            lines.append(f"  [{sid}]  {len(ref_ids)} reference(s) → {text_preview}…")
            applied += 1

        lines.append("")
        if applied:
            ok = await _put_recipe(client, recipe_slug, recipe)
            lines.append(f"  Applied {applied} step(s). " + ("PATCH OK" if ok else "WARN: patch failed — check Mealie logs"))
        else:
            lines.append("  No matching steps found — nothing written.")

        lines.append("\n=== Done ===")
        return "\n".join(lines)

    @mcp.tool()
    async def get_recipes_needing_cleanup() -> str:
        """
        Scan all recipes and report which ones need ingredient cleanup or step linking.

        Three categories are returned, in priority order:
          needs_cleanup — has at least one ingredient with no resolved food ID.
              Run cleanup_recipe on these first.
          needs_linking — all food IDs resolved, has steps, but zero ingredient
              references exist across all steps (linking has never been done).
              Run link_recipe_steps on these next.
          incomplete_linking — some references exist, but not every ingredient
              referenceId is covered by at least one step. Partial linking done;
              revisit with link_recipe_steps to fill gaps.

        Recipes with no ingredients are skipped — they don't need either operation.

        Note: fetches full detail for every recipe, so this can be slow on large
        collections.

        Returns:
            Plain-text report listing slug and name for each recipe in all three
            categories, with a summary line.
        """
        summaries = await _get_all(client, "/api/recipes")

        needs_cleanup: list[dict] = []
        needs_linking: list[dict] = []
        incomplete_linking: list[dict] = []
        skipped = 0

        for summary in summaries:
            slug = summary.get("slug") or summary.get("id", "")
            name = summary.get("name", slug)
            try:
                recipe = await _get_recipe(client, slug)
            except Exception:
                skipped += 1
                continue

            ingredients: list[dict] = recipe.get("recipeIngredient") or []
            if not ingredients:
                continue

            has_unresolved = any(
                not (ing.get("food") or {}).get("id")
                for ing in ingredients
            )

            if has_unresolved:
                needs_cleanup.append({"slug": slug, "name": name})
                continue

            steps: list[dict] = recipe.get("recipeInstructions") or []
            if not steps:
                continue

            total_references = sum(
                len(step.get("ingredientReferences") or [])
                for step in steps
            )

            if total_references == 0:
                needs_linking.append({"slug": slug, "name": name})
                continue

            ingredient_ref_ids = {
                ing["referenceId"]
                for ing in ingredients
                if ing.get("referenceId")
            }
            step_ref_ids = {
                ref["referenceId"]
                for step in steps
                for ref in (step.get("ingredientReferences") or [])
                if ref.get("referenceId")
            }

            if not ingredient_ref_ids.issubset(step_ref_ids):
                incomplete_linking.append({"slug": slug, "name": name})

        lines = ["=== Recipes Needing Cleanup ===", ""]

        lines.append(f"## Needs Ingredient Cleanup ({len(needs_cleanup)} recipes)")
        lines.append("  Next step: cleanup_recipe(<slug>)")
        if needs_cleanup:
            for r in needs_cleanup:
                lines.append(f"  {r['slug']}  —  {r['name']}")
        else:
            lines.append("  (none)")

        lines += [""]
        lines.append(f"## Needs Step Linking ({len(needs_linking)} recipes)")
        lines.append("  Next step: link_recipe_steps(<slug>, ...)")
        if needs_linking:
            for r in needs_linking:
                lines.append(f"  {r['slug']}  —  {r['name']}")
        else:
            lines.append("  (none)")

        lines += [""]
        lines.append(f"## Incomplete Step Linking ({len(incomplete_linking)} recipes)")
        lines.append("  Some ingredients not referenced in any step.")
        lines.append("  Next step: link_recipe_steps(<slug>, ...) to fill gaps.")
        if incomplete_linking:
            for r in incomplete_linking:
                lines.append(f"  {r['slug']}  —  {r['name']}")
        else:
            lines.append("  (none)")

        scanned = len(summaries) - skipped
        summary_line = (
            f"=== {scanned} recipes scanned · {len(needs_cleanup)} need cleanup · "
            f"{len(needs_linking)} need linking · {len(incomplete_linking)} incomplete"
            + (f" · {skipped} skipped (fetch error)" if skipped else "")
            + " ==="
        )
        lines += ["", summary_line]
        return "\n".join(lines)

    @mcp.tool()
    async def fix_ingredient(
        recipe_slug: str,
        reference_id: str,
        food_name: str,
        unit_name: str = "",
        quantity: float | None = None,
        note: str = "",
    ) -> str:
        """
        Surgically fix a single ingredient on a recipe without touching the rest.

        Use this when cleanup_recipe or manual inspection reveals a broken or
        misidentified ingredient. It finds the ingredient by referenceId, resolves
        (or creates) the food and optional unit in the Mealie database, applies
        any quantity/note override, then PUTs the recipe back.

        Args:
            recipe_slug:  The recipe slug or ID.
            reference_id: The ingredient's referenceId (shown in cleanup_recipe output).
            food_name:    Corrected food name. Looked up or created in the food DB.
            unit_name:    Corrected unit name (optional). Looked up or created.
            quantity:     Corrected quantity (optional). Leave None to keep existing.
            note:         Corrected note/display text (optional). Leave empty to keep existing.

        Returns:
            Plain-text summary of what changed and whether the PATCH succeeded.
        """
        try:
            recipe = await _get_recipe(client, recipe_slug)
        except Exception as exc:
            return f"ERROR: could not fetch recipe '{recipe_slug}': {exc}"

        ingredients: list[dict] = recipe.get("recipeIngredient") or []
        target = next((i for i in ingredients if i.get("referenceId") == reference_id), None)
        if target is None:
            return f"ERROR: no ingredient with referenceId '{reference_id}' in recipe '{recipe_slug}'"

        foods = await _get_all(client, "/api/foods")
        units = await _get_all(client, "/api/units")
        food_map = {_normalize_food(f["name"]): f for f in foods}
        unit_map = {_normalize_unit(u["name"]): u for u in units}

        lines: list[str] = [f"=== Fix Ingredient [{reference_id}] in '{recipe.get('name', recipe_slug)}' ===", ""]

        food, action = await _find_or_create_food(client, food_name, food_map)
        if food is None:
            return f"ERROR: could not find or create food '{food_name}'"
        target["food"] = food
        lines.append(f"  food: '{food['name']}' [{action}]")

        if unit_name:
            unit, u_action = await _find_or_create_unit(client, unit_name, unit_map)
            if unit is None:
                lines.append(f"  WARN: could not find or create unit '{unit_name}' — unit left unchanged")
            else:
                target["unit"] = unit
                lines.append(f"  unit: '{unit['name']}' [{u_action}]")

        if quantity is not None:
            target["quantity"] = quantity
            lines.append(f"  quantity: {quantity}")

        if note:
            target["note"] = note
            lines.append(f"  note: '{note}'")

        lines.append("")
        ok = await _put_recipe(client, recipe_slug, recipe)
        lines.append("PATCH OK" if ok else "WARN: patch failed — check Mealie logs")
        lines.append("\n=== Done ===")
        return "\n".join(lines)

    @mcp.tool()
    async def import_and_cleanup_recipe(url: str) -> str:
        """
        Import a recipe from a URL and immediately run cleanup_recipe on it.

        Combines Mealie's URL scraper with ingredient/unit resolution in one call:
          1. POST to /api/recipes/create-from-url to scrape and import the recipe.
          2. Run cleanup_recipe on the result to resolve foods and units, and
             return data for ingredient-to-step linking.

        Args:
            url: The public URL of the recipe page to import.

        Returns:
            The cleanup_recipe output for the newly imported recipe, or an error
            message if the import fails.
        """
        try:
            resp = await client.post("/api/recipes/create-from-url", json={"url": url})
            resp.raise_for_status()
            data = resp.json()
            slug = data if isinstance(data, str) else data.get("slug") or data.get("id", "")
        except Exception as exc:
            return f"ERROR: import failed for {url!r}: {exc}"

        if not slug:
            return f"ERROR: import succeeded but no slug returned for {url!r}"

        return await _cleanup_recipe_impl(client, slug)
