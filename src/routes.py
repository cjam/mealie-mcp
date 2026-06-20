"""Curated route map for the Mealie MCP server.

The Mealie OpenAPI spec exposes ~200 endpoints. For AI meal planning we only
want a focused subset. RouteMaps are evaluated in order — first match wins — so
we list the endpoints to KEEP as tools, then exclude everything else.

Edit `INCLUDE_PATTERNS` to widen/narrow the surface. No tool code to maintain:
the tools are generated from the spec.
"""

from __future__ import annotations

from fastmcp.server.providers.openapi import MCPType, RouteMap

# Path-prefix regexes (matched against the OpenAPI path) to expose as tools.
INCLUDE_PATTERNS: list[str] = [
    r"^/api/recipes",                     # search, get, create, update recipes
    r"^/api/households/mealplans",        # meal plans
    r"^/api/households/shopping/lists",   # shopping lists (CRUD + recipe expansion)
    r"^/api/households/shopping/items",   # shopping list items
    r"^/api/organizers/categories",       # recipe categories
    r"^/api/organizers/tags",             # recipe tags
    r"^/api/foods",                       # food database
    r"^/api/units",                       # measurement units
    r"^/api/groups/labels",              # multi-purpose labels (shopping grouping)
]


def build_route_maps() -> list[RouteMap]:
    """Curated include-list, then exclude the rest of the Mealie API."""
    maps = [
        # Raw PUT on recipes wipes every field not included in the payload
        # (tags, categories, recipeYield, etc.). Exclude both the bulk PUT
        # (/api/recipes) and the single-recipe PUT (/api/recipes/{slug}) so
        # LLMs use the safe composed tools (update_recipe, enrich_recipe, …).
        RouteMap(
            methods=["PUT"],
            pattern=r"^/api/recipes(/[^/]+)?$",
            mcp_type=MCPType.EXCLUDE,
        ),
    ]
    maps += [RouteMap(pattern=p, mcp_type=MCPType.TOOL) for p in INCLUDE_PATTERNS]
    maps.append(RouteMap(pattern=r".*", mcp_type=MCPType.EXCLUDE))
    return maps
