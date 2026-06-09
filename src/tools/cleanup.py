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


# ── Name-lookup helpers ───────────────────────────────────────────────────────

def _find_food(foods: list[dict], query: str) -> dict | None:
    """Find a food by name: exact → case-insensitive → normalized."""
    for f in foods:
        if f["name"] == query:
            return f
    for f in foods:
        if f["name"].lower() == query.lower():
            return f
    normalized = normalize_food(query)
    for f in foods:
        if normalize_food(f["name"]) == normalized:
            return f
    return None


def _find_unit(units: list[dict], query: str) -> dict | None:
    """Find a unit by name: exact → case-insensitive → normalized."""
    for u in units:
        if u["name"] == query:
            return u
    for u in units:
        if u["name"].lower() == query.lower():
            return u
    normalized = normalize_unit(query)
    for u in units:
        if normalize_unit(u["name"]) == normalized:
            return u
    return None


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
    async def set_food_label(food_name: str, label_name: str) -> str:
        """
        Assign a label to a food by name.

        Labels on foods flow automatically to shopping list items when recipes
        are expanded, grouping them visually in the shopping UI (e.g. "Produce",
        "Dairy", "Meat"). Both food and label are matched case-insensitively.

        The label must already exist — create one first via the
        create_multi_purpose_label tool if needed.

        Args:
            food_name:  Name of the food to label (case-insensitive).
            label_name: Name of the label to assign (case-insensitive).

        Returns:
            Success message or error description.
        """
        foods = await get_all(client, "/api/foods")
        food = next(
            (f for f in foods if normalize_food(f["name"]) == normalize_food(food_name)),
            None,
        )
        if food is None:
            return f"ERROR: food '{food_name}' not found"

        r = await client.get("/api/groups/labels", params={"perPage": 200})
        if not r.is_success:
            return f"ERROR: could not fetch labels — HTTP {r.status_code}"
        labels = r.json().get("items", [])
        label = next((lbl for lbl in labels if lbl["name"].lower() == label_name.lower()), None)
        if label is None:
            available = ", ".join(lbl["name"] for lbl in labels[:10])
            hint = f" Available: {available}" if available else ""
            return f"ERROR: label '{label_name}' not found.{hint}"

        food_r = await client.get(f"/api/foods/{food['id']}")
        if not food_r.is_success:
            return f"ERROR: could not fetch food details — HTTP {food_r.status_code}"
        full_food = food_r.json()
        full_food["labelId"] = label["id"]

        put_r = await client.put(f"/api/foods/{food['id']}", json=full_food)
        if put_r.is_success:
            return f"Assigned label '{label['name']}' to food '{full_food['name']}'"
        return f"ERROR: update failed — HTTP {put_r.status_code}"

    @mcp.tool()
    async def assign_food_labels(assignments: dict[str, list[str]]) -> str:
        """
        Assign labels to multiple foods in bulk.

        More efficient than repeated set_food_label calls because all foods and
        all labels are fetched only once. Labels and foods are matched
        case-insensitively. Labels must already exist — create them first via
        the create_multi_purpose_label tool.

        Labels on foods flow automatically to shopping list items when recipes
        are expanded, grouping them visually in the shopping UI.

        Args:
            assignments: Dict mapping each label name to the list of food names
                         to assign it to. Example:
                           {"Produce": ["Carrot", "Onion", "Garlic"],
                            "Dairy":   ["Milk", "Butter", "Cheese"]}

        Returns:
            Plain-text report of each food updated, with errors for any foods
            or labels that could not be found.
        """
        foods = await get_all(client, "/api/foods")

        r = await client.get("/api/groups/labels", params={"perPage": 200})
        if not r.is_success:
            return f"ERROR: could not fetch labels — HTTP {r.status_code}"
        labels = r.json().get("items", [])
        label_map = {lbl["name"].lower(): lbl for lbl in labels}

        lines = ["=== Assign Food Labels ===", ""]
        ok_count = err_count = 0

        for label_name, food_names in assignments.items():
            label = label_map.get(label_name.lower())
            if label is None:
                lines.append(f"## '{label_name}' — NOT FOUND (skipping {len(food_names)} food(s))")
                err_count += len(food_names)
                lines.append("")
                continue

            lines.append(f"## {label['name']}")
            for food_name in food_names:
                food = _find_food(foods, food_name)
                if food is None:
                    lines.append(f"  '{food_name}' — NOT FOUND")
                    err_count += 1
                    continue

                food_r = await client.get(f"/api/foods/{food['id']}")
                if not food_r.is_success:
                    lines.append(f"  '{food_name}' — fetch failed (HTTP {food_r.status_code})")
                    err_count += 1
                    continue

                full_food = food_r.json()
                full_food["labelId"] = label["id"]
                put_r = await client.put(f"/api/foods/{food['id']}", json=full_food)
                if put_r.is_success:
                    lines.append(f"  '{full_food['name']}' — OK")
                    ok_count += 1
                else:
                    lines.append(f"  '{full_food['name']}' — update failed (HTTP {put_r.status_code})")
                    err_count += 1
            lines.append("")

        lines.append(f"=== {ok_count} assigned · {err_count} failed ===")
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
        keep = _find_food(foods, keep_name)
        remove = _find_food(foods, remove_name)

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
        keep = _find_unit(units, keep_name)
        remove = _find_unit(units, remove_name)

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
    async def create_food(name: str, label_name: str | None = None) -> str:
        """
        Create a new food entry, optionally assigning a label in the same call.

        Use this when a food doesn't exist yet and you need it before labeling or
        before running assign_food_labels. If the food already exists the call
        still succeeds and returns the existing entry's name rather than creating
        a duplicate.

        Args:
            name:       Display name for the food (stored as given; Title Case
                        recommended for consistency).
            label_name: Optional. Name of an existing label to assign immediately.
                        The label must already exist — create one via
                        create_multi_purpose_label if needed. Case-insensitive.

        Returns:
            Success message describing what was created/found and labeled, or an
            error description.
        """
        foods = await get_all(client, "/api/foods")
        existing = _find_food(foods, name)

        if existing:
            food = existing
            created = False
        else:
            r = await client.post("/api/foods", json={"name": name.strip()})
            if not r.is_success:
                return f"ERROR: could not create food '{name}' — HTTP {r.status_code}"
            food = r.json()
            created = True

        action = "Created" if created else "Found existing"
        msg = f"{action} food '{food['name']}'"

        if label_name is None:
            return msg

        r = await client.get("/api/groups/labels", params={"perPage": 200})
        if not r.is_success:
            return f"{msg} — ERROR: could not fetch labels (HTTP {r.status_code})"
        labels = r.json().get("items", [])
        label = next((lbl for lbl in labels if lbl["name"].lower() == label_name.lower()), None)
        if label is None:
            available = ", ".join(lbl["name"] for lbl in labels[:10])
            hint = f" Available: {available}" if available else ""
            return f"{msg} — ERROR: label '{label_name}' not found.{hint}"

        food_r = await client.get(f"/api/foods/{food['id']}")
        if not food_r.is_success:
            return f"{msg} — ERROR: could not fetch food details (HTTP {food_r.status_code})"
        full_food = food_r.json()
        full_food["labelId"] = label["id"]
        put_r = await client.put(f"/api/foods/{food['id']}", json=full_food)
        if put_r.is_success:
            return f"{msg} and assigned label '{label['name']}'"
        return f"{msg} — ERROR: label assignment failed (HTTP {put_r.status_code})"

    @mcp.tool()
    async def update_food(
        food_name: str,
        new_name: str | None = None,
        label_name: str | None = None,
    ) -> str:
        """
        Rename a food and/or reassign its label without touching the raw API.

        At least one of new_name or label_name must be provided. Use this to fix
        typos (e.g. "Tumeric" → "Turmeric") or to move a food to a different label
        group. The food is matched case-insensitively.

        Args:
            food_name:  Current name of the food to update (case-insensitive).
            new_name:   Optional new display name. Replaces the existing name.
            label_name: Optional label name to assign. Must already exist.
                        Case-insensitive. Pass an existing label name to reassign,
                        or call create_multi_purpose_label first if the label is new.

        Returns:
            Success message listing what changed, or an error description.
        """
        if new_name is None and label_name is None:
            return "ERROR: supply at least one of new_name or label_name"

        foods = await get_all(client, "/api/foods")
        food = _find_food(foods, food_name)
        if food is None:
            return f"ERROR: food '{food_name}' not found"

        food_r = await client.get(f"/api/foods/{food['id']}")
        if not food_r.is_success:
            return f"ERROR: could not fetch food details — HTTP {food_r.status_code}"
        full_food = food_r.json()

        changes: list[str] = []

        if new_name is not None:
            old = full_food["name"]
            full_food["name"] = new_name.strip()
            changes.append(f"name '{old}' → '{new_name.strip()}'")

        if label_name is not None:
            r = await client.get("/api/groups/labels", params={"perPage": 200})
            if not r.is_success:
                return f"ERROR: could not fetch labels — HTTP {r.status_code}"
            labels = r.json().get("items", [])
            label = next((lbl for lbl in labels if lbl["name"].lower() == label_name.lower()), None)
            if label is None:
                available = ", ".join(lbl["name"] for lbl in labels[:10])
                hint = f" Available: {available}" if available else ""
                return f"ERROR: label '{label_name}' not found.{hint}"
            full_food["labelId"] = label["id"]
            changes.append(f"label → '{label['name']}'")

        put_r = await client.put(f"/api/foods/{food['id']}", json=full_food)
        if put_r.is_success:
            return f"Updated food '{food['name']}': {', '.join(changes)}"
        return f"ERROR: update failed — HTTP {put_r.status_code}"

    @mcp.tool()
    async def delete_food(food_name: str) -> str:
        """
        Delete a food entry by name.

        Use this to surgically remove junk entries — things like "Fried",
        "Extra-Virgin", or "Garnishes" that aren't real food entities and
        pollute autocomplete. The food is matched case-insensitively.

        Note: deleting a food that is still referenced by recipe ingredients
        will leave those ingredients without a food link. Prefer merge_foods
        when there is a canonical entry to redirect references to.

        Args:
            food_name: Name of the food to delete (case-insensitive).

        Returns:
            Success message or error description.
        """
        foods = await get_all(client, "/api/foods")
        food = _find_food(foods, food_name)
        if food is None:
            return f"ERROR: food '{food_name}' not found"

        ok = await _delete(client, "/api/foods", food["id"])
        if ok:
            return f"Deleted food '{food['name']}' ({food['id'][:8]}…)"
        return "ERROR: delete failed — check Mealie logs"

    @mcp.tool()
    async def delete_many_foods(food_names: list[str]) -> str:
        """
        Delete multiple food entries by name in a single call.

        Equivalent to calling delete_food repeatedly, but with one fetch of the
        food list. Use this to bulk-remove junk entries identified by
        list_unlabeled_foods or a manual review. Foods are matched
        case-insensitively.

        Note: deleting foods still referenced by recipe ingredients leaves those
        ingredients without a food link. Prefer merge_many_foods when a canonical
        entry exists.

        Args:
            food_names: List of food names to delete.

        Returns:
            Plain-text report with one line per food plus a summary count.
        """
        foods = await get_all(client, "/api/foods")
        lines: list[str] = ["=== Delete Many Foods ===", ""]
        deleted = not_found = failed = 0

        for name in food_names:
            food = _find_food(foods, name)
            if food is None:
                lines.append(f"  '{name}' — NOT FOUND")
                not_found += 1
                continue
            ok = await _delete(client, "/api/foods", food["id"])
            if ok:
                lines.append(f"  '{food['name']}' — deleted")
                deleted += 1
            else:
                lines.append(f"  '{food['name']}' — delete failed")
                failed += 1

        parts = []
        if deleted:
            parts.append(f"{deleted} deleted")
        if not_found:
            parts.append(f"{not_found} not found")
        if failed:
            parts.append(f"{failed} failed")
        lines += ["", f"=== {' · '.join(parts) or 'nothing to do'} ==="]
        return "\n".join(lines)

    @mcp.tool()
    async def merge_many_foods(merges: list[dict]) -> str:
        """
        Merge multiple food pairs in a single call.

        Equivalent to calling merge_foods repeatedly, but fetches the food list
        only once. Each pair redirects all recipe ingredients from remove_name
        to keep_name, then removes the duplicate. Use this after identifying
        batches of duplicates via cleanup_system or manual review.

        Args:
            merges: List of merge specs, each a dict with:
                      keep_name   — the food name to keep (surviving entry)
                      remove_name — the food name to absorb (will be deleted)
                    Example:
                      [{"keep_name": "Garlic", "remove_name": "garlic clove"},
                       {"keep_name": "Milk",   "remove_name": "whole milk"}]

        Returns:
            Plain-text report with one line per pair plus a summary count.
        """
        foods = await get_all(client, "/api/foods")
        lines: list[str] = ["=== Merge Many Foods ===", ""]
        merged = failed = 0

        for spec in merges:
            keep_name = spec.get("keep_name", "")
            remove_name = spec.get("remove_name", "")
            keep = _find_food(foods, keep_name)
            remove = _find_food(foods, remove_name)

            if keep is None:
                lines.append(f"  '{keep_name}' / '{remove_name}' — ERROR: keep not found")
                failed += 1
                continue
            if remove is None:
                lines.append(f"  '{keep_name}' / '{remove_name}' — ERROR: remove not found")
                failed += 1
                continue
            if keep["id"] == remove["id"]:
                lines.append(f"  '{keep_name}' / '{remove_name}' — ERROR: same entry")
                failed += 1
                continue

            ok = await _merge_foods(client, remove["id"], keep["id"])
            if ok:
                lines.append(f"  '{remove['name']}' → '{keep['name']}' — merged")
                merged += 1
                # refresh the food list so subsequent pairs see the updated state
                foods = [f for f in foods if f["id"] != remove["id"]]
            else:
                lines.append(f"  '{remove['name']}' → '{keep['name']}' — merge failed")
                failed += 1

        parts = []
        if merged:
            parts.append(f"{merged} merged")
        if failed:
            parts.append(f"{failed} failed")
        lines += ["", f"=== {' · '.join(parts) or 'nothing to do'} ==="]
        return "\n".join(lines)

    @mcp.tool()
    async def update_many_foods(updates: list[dict]) -> str:
        """
        Rename and/or relabel multiple foods in a single call.

        Equivalent to calling update_food repeatedly, but fetches the food list
        and label list only once. At least one of new_name or label_name must be
        present in each update spec. Foods and labels are matched
        case-insensitively.

        Args:
            updates: List of update specs, each a dict with:
                       food_name  — current name of the food (required)
                       new_name   — optional new display name
                       label_name — optional label name to assign
                     Example:
                       [{"food_name": "Tumeric",  "new_name": "Turmeric"},
                        {"food_name": "Capsicum", "new_name": "Bell Pepper",
                         "label_name": "Produce"}]

        Returns:
            Plain-text report with one line per food plus a summary count.
        """
        foods = await get_all(client, "/api/foods")

        r = await client.get("/api/groups/labels", params={"perPage": 200})
        labels_ok = r.is_success
        label_map = {lbl["name"].lower(): lbl for lbl in r.json().get("items", [])} if labels_ok else {}

        lines: list[str] = ["=== Update Many Foods ===", ""]
        updated = failed = 0

        for spec in updates:
            food_name = spec.get("food_name", "")
            new_name = spec.get("new_name")
            label_name = spec.get("label_name")

            if new_name is None and label_name is None:
                lines.append(f"  '{food_name}' — SKIP: no new_name or label_name provided")
                failed += 1
                continue

            food = _find_food(foods, food_name)
            if food is None:
                lines.append(f"  '{food_name}' — NOT FOUND")
                failed += 1
                continue

            food_r = await client.get(f"/api/foods/{food['id']}")
            if not food_r.is_success:
                lines.append(f"  '{food_name}' — fetch failed (HTTP {food_r.status_code})")
                failed += 1
                continue
            full_food = food_r.json()
            changes: list[str] = []

            if new_name is not None:
                old = full_food["name"]
                full_food["name"] = new_name.strip()
                changes.append(f"name '{old}' → '{new_name.strip()}'")

            if label_name is not None:
                if not labels_ok:
                    lines.append(f"  '{food_name}' — ERROR: could not fetch labels")
                    failed += 1
                    continue
                label = label_map.get(label_name.lower())
                if label is None:
                    lines.append(f"  '{food_name}' — ERROR: label '{label_name}' not found")
                    failed += 1
                    continue
                full_food["labelId"] = label["id"]
                changes.append(f"label → '{label['name']}'")

            put_r = await client.put(f"/api/foods/{food['id']}", json=full_food)
            if put_r.is_success:
                lines.append(f"  '{food['name']}' — {', '.join(changes)}")
                updated += 1
                # keep local list consistent for any subsequent lookups
                food["name"] = full_food["name"]
            else:
                lines.append(f"  '{food['name']}' — update failed (HTTP {put_r.status_code})")
                failed += 1

        parts = []
        if updated:
            parts.append(f"{updated} updated")
        if failed:
            parts.append(f"{failed} failed")
        lines += ["", f"=== {' · '.join(parts) or 'nothing to do'} ==="]
        return "\n".join(lines)

    @mcp.tool()
    async def list_unlabeled_foods() -> str:
        """
        Return all foods that have no label assigned (labelId is null).

        Use this as a diagnostic after a bulk assign_food_labels pass to see
        what still needs labeling, without having to scan the full food list.

        Returns:
            Plain-text list of unlabeled food names sorted alphabetically, with
            a count summary. Returns a "none found" message if all foods are labeled.
        """
        foods = await get_all(client, "/api/foods")
        unlabeled = sorted(
            (f["name"] for f in foods if not f.get("labelId")),
            key=str.casefold,
        )
        if not unlabeled:
            return f"All {len(foods)} food(s) have a label assigned."
        lines = [
            f"=== Unlabeled Foods ({len(unlabeled)} of {len(foods)}) ===",
            "",
        ]
        lines.extend(f"  {name}" for name in unlabeled)
        lines += ["", f"=== {len(unlabeled)} unlabeled ==="]
        return "\n".join(lines)

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
