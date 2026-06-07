"""System cleanup tool — deduplicates and normalises foods and units."""

from __future__ import annotations

from typing import Any

import httpx

from utils import (
    STANDARD_UNITS,
    canonical_food,
    get_all,
    normalize_food,
    normalize_unit,
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
