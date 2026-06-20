"""Shared data, normalization helpers, and Mealie API utilities.

Used by multiple tool modules. Nothing in here registers MCP tools.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger("mealie-mcp")

# ── Unit data ─────────────────────────────────────────────────────────────────

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


# Maps canonical unit name → (family, factor_to_base).
# Factor is "how many base units are in this unit".
# Units not listed here are count/descriptive and cannot be converted.
_UNIT_FAMILIES: dict[str, tuple[str, float]] = {
    # US Volume — base: teaspoon
    "teaspoon":    ("us_volume", 1.0),
    "tablespoon":  ("us_volume", 3.0),
    "cup":         ("us_volume", 48.0),
    "fluid ounce": ("us_volume", 6.0),
    "pint":        ("us_volume", 96.0),
    "quart":       ("us_volume", 192.0),
    "gallon":      ("us_volume", 768.0),
    # Metric Volume — base: milliliter
    "milliliter":  ("metric_volume", 1.0),
    "liter":       ("metric_volume", 1000.0),
    # Imperial Weight — base: ounce
    "ounce":       ("imperial_weight", 1.0),
    "pound":       ("imperial_weight", 16.0),
    # Metric Weight — base: gram
    "milligram":   ("metric_weight", 0.001),
    "gram":        ("metric_weight", 1.0),
    "kilogram":    ("metric_weight", 1000.0),
}


def unit_family_and_factor(canonical_name: str) -> tuple[str, float] | None:
    """Return (family, factor_to_base) for a canonical unit name, or None."""
    return _UNIT_FAMILIES.get(canonical_name)


# ── Normalization ──────────────────────────────────────────────────────────────

def normalize_food(name: str) -> str:
    return name.strip().lower()


def canonical_food(normalized_key: str, originals: list[str]) -> str:
    for n in originals:
        if n.strip() == n.strip().title():
            return n.strip()
    return normalized_key.title()


def normalize_unit(name: str) -> str:
    raw = name.strip()
    if raw in _UNIT_ALIASES:
        return _UNIT_ALIASES[raw]
    lower = raw.lower()
    return _UNIT_ALIASES.get(lower, lower)


_CANONICAL_UNIT_NAMES: frozenset[str] = frozenset(u["name"] for u in STANDARD_UNITS)


def detect_unit_in_text(text: str) -> str | None:
    """Return the canonical unit name if text contains a recognized unit word, else None.

    Checks two-word phrases first (e.g. 'fl oz') then individual tokens.
    Single-char abbreviations like 'g', 'c', 'L' are included.
    """
    if not text:
        return None
    words = re.split(r"[\s,]+", text)
    # Two-word phrases first (covers "fl oz", "fl. oz.", "fluid ounce", etc.)
    for i in range(len(words) - 1):
        phrase = f"{words[i]} {words[i + 1]}"
        canonical = normalize_unit(phrase)
        if canonical in _CANONICAL_UNIT_NAMES:
            return canonical
    # Single tokens
    for word in words:
        token = word.strip(".()")
        if not token:
            continue
        canonical = normalize_unit(token)
        if canonical in _CANONICAL_UNIT_NAMES:
            return canonical
    return None


# ── Generic HTTP helpers ───────────────────────────────────────────────────────

async def get_all(client: httpx.AsyncClient, path: str) -> list[dict]:
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


# ── Food / unit helpers ────────────────────────────────────────────────────────

async def find_or_create_food(
    client: httpx.AsyncClient,
    name: str,
    food_map: dict[str, dict],
) -> tuple[dict | None, str]:
    """Return (food_object, "found"|"created"|"failed")."""
    normalized = normalize_food(name)
    if normalized in food_map:
        return food_map[normalized], "found"
    display = name.strip().title()
    try:
        r = await client.post("/api/foods", json={"name": display})
        if r.is_success:
            food = r.json()
            food_map[normalize_food(food["name"])] = food
            return food, "created"
    except Exception:
        pass
    return None, "failed"


async def find_or_create_unit(
    client: httpx.AsyncClient,
    name: str,
    unit_map: dict[str, dict],
) -> tuple[dict | None, str]:
    """Return (unit_object, "found"|"created"|"failed")."""
    canonical = normalize_unit(name)
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
            unit_map[normalize_unit(unit["name"])] = unit
            return unit, "created"
    except Exception:
        pass
    return None, "failed"


# ── Recipe helpers ─────────────────────────────────────────────────────────────

# In-process cache: slug/uuid → uuid. UUIDs are stable; slugs only change on
# rename, which is rare within a single MCP session. Avoids extra GETs in
# planning and shopping tools that resolve the same recipes repeatedly.
_id_cache: dict[str, str] = {}


async def get_recipe(client: httpx.AsyncClient, slug: str) -> dict:
    resp = await client.get(f"/api/recipes/{slug}")
    resp.raise_for_status()
    return resp.json()


async def resolve_recipe_id(client: httpx.AsyncClient, slug_or_id: str) -> str:
    """Return the UUID for a recipe given its slug or UUID."""
    if slug_or_id in _id_cache:
        return _id_cache[slug_or_id]
    recipe = await get_recipe(client, slug_or_id)
    recipe_id = recipe["id"]
    _id_cache[recipe_id] = recipe_id
    _id_cache[recipe.get("slug", slug_or_id)] = recipe_id
    return recipe_id


async def put_recipe(client: httpx.AsyncClient, slug: str, recipe: dict) -> bool:
    try:
        r = await client.put(f"/api/recipes/{slug}", json=recipe)
        return r.is_success
    except Exception:
        return False


async def parse_ingredient_text(client: httpx.AsyncClient, text: str) -> dict | None:
    """Call Mealie's NLP parser. Returns the parsed ingredient dict or None."""
    try:
        r = await client.post("/api/parser/ingredient", json={"ingredient": text})
        if r.is_success:
            return r.json().get("ingredient")
    except Exception:
        pass
    return None


_MISPARSE_PHRASES: tuple[str, ...] = (
    "at room temperature",
    "store-bought",
    "store bought",
    "or homemade",
    ", divided",
    "divided",
    "optional",
    "to taste",
    "as needed",
)


def misparse_reason(ing: dict) -> str | None:
    """Return a human-readable reason if the ingredient looks like a parse failure, else None."""
    food_obj = ing.get("food")
    unit_obj = ing.get("unit")
    food_name = (food_obj or {}).get("name") if isinstance(food_obj, dict) else None

    if food_obj is None:
        if unit_obj:
            return "food is null (unit present — likely a parsing failure)"
        return "food is null"

    if food_name:
        lower = food_name.lower()
        for phrase in _MISPARSE_PHRASES:
            if phrase in lower:
                return f"food name contains modifier ({phrase!r})"
        if " " not in food_name and food_name[:1].isupper():
            w = food_name.lower()
            if w.endswith("ed") or w.endswith("en"):
                return "food name looks like an adjective/state — probably truncated"

    return None


async def cleanup_recipe_impl(client: httpx.AsyncClient, recipe_slug: str) -> str:
    """Core recipe cleanup logic — shared by cleanup_recipe and import_and_cleanup_recipe."""
    try:
        recipe = await get_recipe(client, recipe_slug)
    except Exception as exc:
        return f"ERROR: could not fetch recipe '{recipe_slug}': {exc}"

    recipe_slug = recipe.get("slug", recipe_slug)
    recipe_name = recipe.get("name", recipe_slug)
    lines: list[str] = [f"=== Recipe Cleanup: {recipe_name} ===", ""]

    foods, units, existing_tags, existing_cats = await asyncio.gather(
        get_all(client, "/api/foods"),
        get_all(client, "/api/units"),
        get_all(client, "/api/organizers/tags"),
        get_all(client, "/api/organizers/categories"),
    )
    food_map = {normalize_food(f["name"]): f for f in foods}
    unit_map = {normalize_unit(u["name"]): u for u in units}

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
        if food_obj is None and note:
            parsed = await parse_ingredient_text(client, note)
            if parsed:
                p_food = parsed.get("food") or {}
                p_unit = parsed.get("unit") or {}
                food_name = p_food.get("name") if isinstance(p_food, dict) else None
                unit_name = p_unit.get("name") if isinstance(p_unit, dict) else None
                p_qty = parsed.get("quantity")

                if food_name:
                    food, action = await find_or_create_food(client, food_name, food_map)
                    if food:
                        ing["food"] = food
                        if p_qty:
                            ing["quantity"] = p_qty
                        parts.append(f"food '{food['name']}' [{action}]")
                        changes += 1
                    else:
                        parts.append(f"food '{food_name}' [FAILED]")

                if unit_name:
                    unit, action = await find_or_create_unit(client, unit_name, unit_map)
                    if unit:
                        ing["unit"] = unit
                        parts.append(f"unit '{unit['name']}' [{action}]")
                        changes += 1
                    else:
                        parts.append(f"unit '{unit_name}' [FAILED]")

        # Case 2: food/unit objects present but not yet DB-linked (partial objects).
        elif food_name and not food_id:
            food, action = await find_or_create_food(client, food_name, food_map)
            if food:
                ing["food"] = food
                parts.append(f"food '{food['name']}' [{action}]")
                changes += 1
            else:
                parts.append(f"food '{food_name}' [FAILED]")

            if unit_name and not unit_id:
                unit, action = await find_or_create_unit(client, unit_name, unit_map)
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
        ok = await put_recipe(client, recipe_slug, recipe)
        lines.append("  " + ("PATCH OK" if ok else "WARN: patch failed — check Mealie logs"))
        if ok:
            # Mealie regenerates step IDs on PUT — re-fetch to get the current IDs.
            try:
                recipe = await get_recipe(client, recipe_slug)
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
    flagged: list[tuple[str, str, str]] = []
    for ing in ingredients:
        ref   = ing.get("referenceId", "?")
        qty   = ing.get("quantity") or ""
        uname = (ing.get("unit") or {}).get("name") or ""
        fname = (ing.get("food") or {}).get("name") or (ing.get("note") or "").strip() or "?"
        qty_str = f"{qty} {uname}".strip()
        reason = misparse_reason(ing)
        if reason:
            flagged.append((ref, fname, reason))
            lines.append(f"  [{ref}]  {qty_str} {fname}  ⚠️ {reason}".rstrip())
        else:
            lines.append(f"  [{ref}]  {qty_str} {fname}".rstrip())

    lines += ["", "Steps:"]
    for step in recipe.get("recipeInstructions") or []:
        sid  = step.get("id", "?")
        text = (step.get("text") or "").replace("\n", " ")[:150]
        lines.append(f"  [{sid}]  {text}")

    tag_names = sorted(t["name"] for t in existing_tags)
    cat_names = sorted(c["name"] for c in existing_cats)

    lines += ["", "## Next Steps"]
    if flagged:
        lines.append("Fix flagged ingredients first (call fix_ingredient for each ⚠️ above):")
        for ref, fname, reason in flagged:
            lines.append(
                f"   fix_ingredient('{recipe_slug}', '{ref}', food_name='<corrected name>')"
                f"  # {fname} — {reason}"
            )
        lines.append("")

    lines.append("Then apply all enrichments in one call:")
    lines.append(f"   enrich_recipe('{recipe_slug}',")
    lines.append("     step_ingredient_map={step_id: [ref_ids], ...},")
    lines.append("     tags=['...'],")
    lines.append("     categories=['...'],")
    lines.append("   )")
    if tag_names:
        lines.append(f"   Existing tags: {', '.join(tag_names)}")
    else:
        lines.append("   No tags exist yet — any name you provide will be added automatically.")
    if cat_names:
        lines.append(f"   Existing categories: {', '.join(cat_names)}")
    else:
        lines.append("   No categories exist yet — any name you provide will be added automatically.")

    lines += ["", "=== Done ==="]
    return "\n".join(lines)
