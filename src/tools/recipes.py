"""Recipe tools — cleanup, step linking, and import."""

from __future__ import annotations

from typing import Any

import httpx

from utils import (
    cleanup_recipe_impl,
    get_all,
    get_recipe,
    put_recipe,
    find_or_create_food,
    find_or_create_unit,
    normalize_food,
    normalize_unit,
)

_SUGGEST_PATH = "/api/recipes/suggestions"


def register_recipe_tools(mcp: Any, client: httpx.AsyncClient) -> None:

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
        return await cleanup_recipe_impl(client, recipe_slug)

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
            recipe = await get_recipe(client, recipe_slug)
        except Exception as exc:
            return f"ERROR: could not fetch recipe '{recipe_slug}': {exc}"

        recipe_name = recipe.get("name", recipe_slug)
        lines: list[str] = [f"=== Step Linking: {recipe_name} ===", ""]

        steps: list[dict] = recipe.get("recipeInstructions") or []
        applied = 0
        unknown_steps = set(step_ingredient_map) - {s["id"] for s in steps}
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
            ok = await put_recipe(client, recipe_slug, recipe)
            lines.append(
                f"  Applied {applied} step(s). "
                + ("PATCH OK" if ok else "WARN: patch failed — check Mealie logs")
            )
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
        summaries = await get_all(client, "/api/recipes")

        needs_cleanup: list[dict] = []
        needs_linking: list[dict] = []
        incomplete_linking: list[dict] = []
        skipped = 0

        for summary in summaries:
            slug = summary.get("slug") or summary.get("id", "")
            name = summary.get("name", slug)
            try:
                recipe = await get_recipe(client, slug)
            except Exception:
                skipped += 1
                continue

            ingredients: list[dict] = recipe.get("recipeIngredient") or []
            if not ingredients:
                continue

            has_unresolved = any(
                not (ing.get("food") or {}).get("id")
                and (
                    (ing.get("note") or "").strip()
                    or (ing.get("food") or {}).get("name")
                )
                for ing in ingredients
            )

            if has_unresolved:
                needs_cleanup.append({"slug": slug, "name": name})
                continue

            steps: list[dict] = recipe.get("recipeInstructions") or []
            if not steps:
                continue

            total_references = sum(len(step.get("ingredientReferences") or []) for step in steps)

            if total_references == 0:
                needs_linking.append({"slug": slug, "name": name})
                continue

            ingredient_ref_ids = {
                ing["referenceId"] for ing in ingredients if ing.get("referenceId")
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
        for r in needs_cleanup:
            lines.append(f"  {r['name']}  →  cleanup_recipe('{r['slug']}')")
        if not needs_cleanup:
            lines.append("  (none)")

        lines += [""]
        lines.append(f"## Needs Step Linking ({len(needs_linking)} recipes)")
        for r in needs_linking:
            lines.append(f"  {r['name']}  →  link_recipe_steps('{r['slug']}', {{step_id: [ref_ids], ...}})")
        if not needs_linking:
            lines.append("  (none)")

        lines += [""]
        lines.append(f"## Incomplete Step Linking ({len(incomplete_linking)} recipes)")
        lines.append("  Some ingredients not referenced in any step.")
        for r in incomplete_linking:
            lines.append(f"  {r['name']}  →  link_recipe_steps('{r['slug']}', {{step_id: [ref_ids], ...}})")
        if not incomplete_linking:
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
            recipe = await get_recipe(client, recipe_slug)
        except Exception as exc:
            return f"ERROR: could not fetch recipe '{recipe_slug}': {exc}"

        ingredients: list[dict] = recipe.get("recipeIngredient") or []
        target = next((i for i in ingredients if i.get("referenceId") == reference_id), None)
        if target is None:
            return f"ERROR: no ingredient with referenceId '{reference_id}' in recipe '{recipe_slug}'"

        foods = await get_all(client, "/api/foods")
        units = await get_all(client, "/api/units")
        food_map = {normalize_food(f["name"]): f for f in foods}
        unit_map = {normalize_unit(u["name"]): u for u in units}

        lines: list[str] = [
            f"=== Fix Ingredient [{reference_id}] in '{recipe.get('name', recipe_slug)}' ===", ""
        ]

        food, action = await find_or_create_food(client, food_name, food_map)
        if food is None:
            return f"ERROR: could not find or create food '{food_name}'"
        target["food"] = food
        lines.append(f"  food: '{food['name']}' [{action}]")

        if unit_name:
            unit, u_action = await find_or_create_unit(client, unit_name, unit_map)
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
        ok = await put_recipe(client, recipe_slug, recipe)
        lines.append("PATCH OK" if ok else "WARN: patch failed — check Mealie logs")
        lines.append("\n=== Done ===")
        return "\n".join(lines)

    @mcp.tool()
    async def import_and_cleanup_recipe(url: str) -> str:
        """
        Import a recipe from a URL and immediately run cleanup_recipe on it.

        Combines Mealie's URL scraper with ingredient/unit resolution in one call:
          1. POST to /api/recipes/create/url to scrape and import the recipe.
          2. Run cleanup_recipe on the result to resolve foods and units, and
             return data for ingredient-to-step linking.

        Args:
            url: The public URL of the recipe page to import.

        Returns:
            The cleanup_recipe output for the newly imported recipe, or an error
            message if the import fails.
        """
        try:
            resp = await client.post("/api/recipes/create/url", json={"url": url})
            resp.raise_for_status()
            data = resp.json()
            slug = data if isinstance(data, str) else data.get("slug") or data.get("id", "")
        except Exception as exc:
            return f"ERROR: import failed for {url!r}: {exc}"

        if not slug:
            return f"ERROR: import succeeded but no slug returned for {url!r}"

        return await cleanup_recipe_impl(client, slug)

    @mcp.tool()
    async def create_recipe(
        name: str,
        description: str = "",
        servings: int | None = None,
    ) -> str:
        """
        Create a new recipe from scratch.

        Creates a recipe with the given name, then optionally sets description
        and servings count. Returns the slug for use with cleanup_recipe,
        update_recipe, link_recipe_steps, and meal planning tools.

        Args:
            name:        Recipe name.
            description: Optional description / notes.
            servings:    Optional number of servings.

        Returns:
            Plain-text confirmation with the recipe slug.
        """
        try:
            r = await client.post("/api/recipes", json={"name": name})
            r.raise_for_status()
            slug = r.json()
        except Exception as exc:
            return f"ERROR: failed to create recipe '{name}': {exc}"

        if not isinstance(slug, str):
            slug = (slug or {}).get("slug") or (slug or {}).get("id", "")

        if description or servings is not None:
            try:
                recipe = await get_recipe(client, slug)
                if description:
                    recipe["description"] = description
                if servings is not None:
                    recipe["recipeYield"] = str(servings)
                await put_recipe(client, slug, recipe)
            except Exception:
                pass

        return f"Created recipe '{name}'\nSlug: {slug}"

    @mcp.tool()
    async def update_recipe(
        recipe_slug: str,
        name: str | None = None,
        description: str | None = None,
        servings: int | None = None,
    ) -> str:
        """
        Update metadata fields on an existing recipe.

        Only the fields you provide are changed — omit a field to leave it as-is.
        Use this to fix a recipe's name or description after importing, or to set
        servings on a newly created recipe.

        If name is changed, Mealie regenerates the slug — the new slug is returned
        so downstream calls (link_recipe_steps, meal planning) use the right value.

        Args:
            recipe_slug: The recipe slug or ID.
            name:        New recipe name (optional).
            description: New description (optional).
            servings:    New number of servings (optional).

        Returns:
            Plain-text summary of changes and the (potentially new) slug.
        """
        try:
            recipe = await get_recipe(client, recipe_slug)
        except Exception as exc:
            return f"ERROR: could not fetch recipe '{recipe_slug}': {exc}"

        changes: list[str] = []
        if name is not None:
            recipe["name"] = name
            changes.append(f"name → '{name}'")
        if description is not None:
            recipe["description"] = description
            changes.append("description updated")
        if servings is not None:
            recipe["recipeYield"] = str(servings)
            changes.append(f"servings → {servings}")

        if not changes:
            return "No fields to update — provide at least one of: name, description, servings"

        ok = await put_recipe(client, recipe_slug, recipe)
        if not ok:
            return "ERROR: update failed — check Mealie logs"

        # Re-fetch to get the authoritative slug (name change regenerates it).
        new_slug = recipe_slug
        try:
            updated = await get_recipe(client, recipe_slug)
            new_slug = updated.get("slug", recipe_slug)
        except Exception:
            pass

        result = f"Updated '{recipe.get('name', recipe_slug)}': {', '.join(changes)}"
        if new_slug != recipe_slug:
            result += f"\nNew slug: {new_slug}"
        return result

    @mcp.tool()
    async def suggest_recipes_by_name(food_names: list[str], limit: int = 10) -> str:
        """
        Suggest recipes based on food/ingredient names you have available.

        Resolves each food name to its Mealie database ID, then queries the
        recipe suggestions endpoint. Use this for "what can I cook with X, Y, Z"
        queries. Food names are matched case-insensitively.

        Args:
            food_names: List of ingredient/food names to match against.
            limit:      Maximum number of suggestions to return (default 10).

        Returns:
            Plain-text list of suggested recipes with name and slug, or an error
            if none of the food names could be resolved.
        """
        foods = await get_all(client, "/api/foods")
        food_map = {normalize_food(f["name"]): f for f in foods}

        resolved_ids: list[str] = []
        unresolved: list[str] = []
        for name in food_names:
            food = food_map.get(normalize_food(name))
            if food:
                resolved_ids.append(food["id"])
            else:
                unresolved.append(name)

        if not resolved_ids:
            return (
                f"ERROR: none of the provided food names found in the database: {food_names}\n"
                "Run cleanup_system first to populate the food database, or check "
                "spelling against the foods list."
            )

        try:
            params: list[tuple[str, str | int]] = [("foods", fid) for fid in resolved_ids]
            params.append(("limit", limit))
            r = await client.get(_SUGGEST_PATH, params=params)
            r.raise_for_status()
            data = r.json()
            suggestions = data if isinstance(data, list) else data.get("items", [])
        except Exception as exc:
            return f"ERROR: suggestion query failed: {exc}"

        lines = [f"=== Recipe Suggestions for: {', '.join(food_names)} ===", ""]
        if unresolved:
            lines.append(f"Note: foods not found in database (skipped): {', '.join(unresolved)}")
            lines.append("")

        if not suggestions:
            lines.append("No suggestions found.")
            return "\n".join(lines)

        for recipe in suggestions:
            rname = recipe.get("name", "?")
            slug = recipe.get("slug") or recipe.get("id", "?")
            lines.append(f"  {rname}  (slug: {slug})")

        lines.append(f"\n{len(suggestions)} suggestion(s)")
        return "\n".join(lines)
