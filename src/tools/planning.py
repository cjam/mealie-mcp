"""Meal planning tools."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx


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
                          recipe_id  (str, Mealie recipe UUID, required)
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
            payload = {
                "date": entry["date"],
                "entryType": entry.get("entry_type", "dinner"),
                "recipeId": entry.get("recipe_id"),
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
