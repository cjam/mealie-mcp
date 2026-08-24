"""Meal planning tools."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx

from utils import resolve_recipe_id


async def _get_mealplans_range(
    client: httpx.AsyncClient, start_date: str, end_date: str
) -> list[dict]:
    """Fetch all meal plan entries in [start_date, end_date] (YYYY-MM-DD)."""
    resp = await client.get(
        "/api/households/mealplans",
        params={"start_date": start_date, "end_date": end_date, "perPage": 500},
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", data) if isinstance(data, dict) else data


def register_planning_tools(mcp: Any, client: httpx.AsyncClient) -> None:

    @mcp.tool()
    async def get_mealplans(start_date: str, end_date: str) -> str:
        """
        Fetch all meal plan entries within a date range.

        Use this to see what was planned recently (e.g. the last 1–2 weeks) before
        building a new plan, so you can avoid repeating meals.

        Args:
            start_date: First day of the range, inclusive (YYYY-MM-DD).
            end_date:   Last day of the range, inclusive (YYYY-MM-DD).

        Returns:
            Plain-text listing of every meal plan entry in the range, one per line,
            showing date, entry type, and recipe name (or custom title).
        """
        entries = await _get_mealplans_range(client, start_date, end_date)
        if not entries:
            return f"No meal plan entries between {start_date} and {end_date}."
        lines = [f"=== Meal Plan: {start_date} → {end_date} ===", ""]
        for e in sorted(entries, key=lambda x: (x.get("date", ""), x.get("entryType", ""))):
            d = e.get("date", "?")
            etype = e.get("entryType", "?")
            recipe = e.get("recipe") or {}
            name = recipe.get("name") or e.get("title") or "(no title)"
            eid = e.get("id", "")
            lines.append(f"  {d}  [{etype}]  {name}  (id: {eid})")
        lines.append(f"\n{len(entries)} entries total")
        return "\n".join(lines)

    @mcp.tool()
    async def replace_week_meal_plan(week_start: str, entries: list[dict]) -> str:
        """
        Atomically replace all meal plan entries for a 7-day week.

        Fetches the existing entries for [week_start, week_start+6 days], deletes
        them all, then creates the new entries you supply. Safe to call even if
        the week is currently empty.

        Args:
            week_start: First day of the week (YYYY-MM-DD). The window covers
                        this day through the following 6 days.
            entries:    List of dicts, each with:
                          date       (str, YYYY-MM-DD, required)
                          recipe_id  (str, Mealie recipe UUID or slug, required)
                          entry_type (str, optional — "breakfast"/"lunch"/"dinner"/
                                      "side", defaults to "dinner")
                          title      (str, optional — shown when recipe_id absent)

        Returns:
            Plain-text summary of deleted and created entries.
        """
        start = date.fromisoformat(week_start)
        week_end = (start + timedelta(days=6)).isoformat()

        existing = await _get_mealplans_range(client, week_start, week_end)
        lines = [f"=== Replace Week Meal Plan: {week_start} → {week_end} ===", ""]

        deleted = 0
        for entry in existing:
            eid = entry.get("id")
            if eid:
                r = await client.delete(f"/api/households/mealplans/{eid}")
                if r.is_success:
                    deleted += 1
                else:
                    lines.append(f"  WARN: delete failed for entry {eid} — HTTP {r.status_code}")
        lines.append(f"  Deleted {deleted}/{len(existing)} existing entries")

        created = 0
        for entry in entries:
            recipe_id = entry.get("recipe_id")
            if recipe_id:
                try:
                    recipe_id = await resolve_recipe_id(client, recipe_id)
                except Exception:
                    lines.append(f"  WARN: could not resolve recipe '{recipe_id}' — skipping")
                    continue
            payload = {
                "date": entry["date"],
                "entryType": entry.get("entry_type", "dinner"),
                "recipeId": recipe_id,
                "title": entry.get("title", ""),
            }
            r = await client.post("/api/households/mealplans", json=payload)
            if r.is_success:
                created += 1
            else:
                lines.append(
                    f"  WARN: create failed for {entry.get('date')} "
                    f"{entry.get('recipe_id')} — HTTP {r.status_code}"
                )
        lines.append(f"  Created {created}/{len(entries)} new entries")
        lines.append("\n=== Done ===")
        return "\n".join(lines)

    @mcp.tool()
    async def find_recipes_using_ingredients(ingredients: list[str], max_results: int = 5) -> str:
        """
        Find recipes that use one or more of the given ingredients — useful for
        using up what's already in the fridge/pantry when building a meal plan.

        Searches recipe text (name, description, ingredient lines) for each
        ingredient and ranks matching recipes by how many of the given
        ingredients they contain. Matches raw/unlinked ingredient text too, so
        it works even on recipes that haven't been run through cleanup_recipe.

        Don't build the whole week around these results — pick 1–2 of the
        top matches and mix them into an otherwise normal plan.

        Args:
            ingredients: Ingredient names to search for, e.g. ["cabbage", "eggs", "chicken"].
            max_results: Maximum number of recipes to return (default 5).

        Returns:
            Plain-text ranked list of matching recipes with slug/id and which
            of the given ingredients each one matched.
        """
        terms = [i.strip() for i in ingredients if i.strip()]
        if not terms:
            return "No ingredients provided."

        matches: dict[str, dict] = {}
        for term in terms:
            resp = await client.get("/api/recipes", params={"search": term, "perPage": 50})
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", data) if isinstance(data, dict) else data
            for r in items:
                slug = r.get("slug")
                if not slug:
                    continue
                entry = matches.setdefault(
                    slug, {"name": r.get("name", slug), "id": r.get("id", ""), "matched": set()}
                )
                entry["matched"].add(term)

        if not matches:
            return f"No recipes found matching: {', '.join(terms)}."

        ranked = sorted(
            matches.items(), key=lambda kv: (-len(kv[1]["matched"]), kv[1]["name"])
        )[:max_results]

        lines = [f"=== Recipes matching ingredients: {', '.join(terms)} ===", ""]
        for slug, info in ranked:
            matched = ", ".join(sorted(info["matched"]))
            lines.append(f"  {info['name']}  (slug: {slug}, id: {info['id']})  —  matches: {matched}")
        lines.append("")
        lines.append(
            "Tip: pick 1–2 of these to use up existing ingredients rather than "
            "basing the whole week's plan around them."
        )
        return "\n".join(lines)
