"""Verify that every INCLUDE_PATTERN in routes.py matches at least one real
path in the Mealie OpenAPI spec, and that the specific endpoints our composed
tools depend on are present."""

from __future__ import annotations

import re

import httpx
import pytest

from routes import INCLUDE_PATTERNS

# Endpoints that our hand-written tools call directly (not just via route maps).
# Tuple of (HTTP method, path template) — path must appear in the spec.
REQUIRED_ENDPOINTS: list[tuple[str, str]] = [
    # get_mealplans / replace_week_meal_plan
    ("get",    "/api/households/mealplans"),
    ("post",   "/api/households/mealplans"),
    ("delete", "/api/households/mealplans/{item_id}"),
    # replace_shopping_list_from_recipes — item fetch + bulk delete
    ("get",    "/api/households/shopping/items"),
    ("delete", "/api/households/shopping/items"),
    # replace_shopping_list_from_recipes — recipe ingredient expansion
    ("post",   "/api/households/shopping/lists/{item_id}/recipe/{recipe_id}"),
    # set_food_label — label lookup and food update
    ("get",    "/api/groups/labels"),
    ("get",    "/api/foods/{item_id}"),
    ("put",    "/api/foods/{item_id}"),
    # get_import_queue_report — test-scrape without creating a recipe
    ("post",   "/api/recipes/test-scrape-url"),
]


@pytest.fixture(scope="module")
def openapi_spec(mealie_base_url):
    resp = httpx.get(f"{mealie_base_url}/openapi.json", timeout=30)
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(scope="module")
def spec_paths(openapi_spec) -> dict[str, dict]:
    """Return the paths dict from the spec (path → {method → operation})."""
    return openapi_spec.get("paths", {})


# ── Pattern coverage ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("pattern", INCLUDE_PATTERNS)
def test_include_pattern_matches_at_least_one_spec_path(pattern, spec_paths):
    """Every INCLUDE_PATTERN must match ≥1 path in the live Mealie spec."""
    compiled = re.compile(pattern)
    matches = [p for p in spec_paths if compiled.match(p)]
    assert matches, (
        f"INCLUDE_PATTERN {pattern!r} matched no paths in the OpenAPI spec.\n"
        f"Available paths:\n" + "\n".join(f"  {p}" for p in sorted(spec_paths))
    )


# ── Required endpoint presence ────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", REQUIRED_ENDPOINTS)
def test_required_endpoint_exists_in_spec(method, path, spec_paths):
    """Each endpoint our composed tools call directly must exist in the spec."""
    assert path in spec_paths, (
        f"Path {path!r} not found in spec.\n"
        f"Closest matches: {[p for p in spec_paths if path.split('{')[0].rstrip('/') in p][:10]}"
    )
    assert method in spec_paths[path], (
        f"{method.upper()} {path} exists in spec but has no {method!r} operation.\n"
        f"Available methods: {list(spec_paths[path])}"
    )
