# Mealie MCP — Development Guide

## Project layout

```
src/
  utils.py          # shared constants, normalization, HTTP helpers, recipe impl
  tools/
    __init__.py     # register_all_tools() — wired into server.py
    cleanup.py      # cleanup_system (food/unit deduplication)
    recipes.py      # cleanup_recipe, link_recipe_steps, fix_ingredient, …
    planning.py     # get_mealplans, replace_week_meal_plan
    shopping.py     # replace_shopping_list_from_recipes
  server.py         # FastMCP setup, OpenAPI spec fetch, middleware
  config.py         # Settings dataclass, env var loading
  routes.py         # INCLUDE_PATTERNS — curated OpenAPI route whitelist
tests/
  conftest.py       # Testcontainers fixtures (mealie_base_url, mcp_server, …)
  test_routes.py    # Validates every INCLUDE_PATTERN against the live spec
  test_integration.py
  test_cleanup.py
  test_recipe_cleanup.py
  test_token_forwarding.py
  test_server_transport.py
```

## Adding a new feature

### 1. Pick the right file

| What you're adding | Where it goes |
|---|---|
| New MCP tool for meal planning | `src/tools/planning.py` |
| New MCP tool for shopping lists | `src/tools/shopping.py` |
| New MCP tool for recipe management | `src/tools/recipes.py` |
| New MCP tool for system/DB cleanup | `src/tools/cleanup.py` |
| New domain of tools (e.g. households) | New `src/tools/<domain>.py` + add `register_<domain>_tools` to `src/tools/__init__.py` |
| Helper used by more than one tool module | `src/utils.py` |
| Helper used by only one tool module | Top of that tool module (prefixed `_`) |

### 2. Exposing a raw Mealie endpoint

If the new tool is a thin wrapper over an existing Mealie API endpoint, add its path pattern to `INCLUDE_PATTERNS` in `src/routes.py` instead of writing a composed tool. FastMCP will generate the tool automatically from the OpenAPI spec.

**Always verify the path exists first.** Run `tests/test_routes.py` — it fetches the live spec and asserts every pattern matches at least one real path. If you add a pattern that doesn't match, that test will fail and print the full path list so you can find the correct one.

### 3. Composed tools

For tools that combine multiple API calls (e.g. fetch → transform → write), register them inside a `register_<domain>_tools(mcp, client)` function in the appropriate domain file. Follow the existing closure pattern — `client` is captured from the outer scope, not passed per-call.

### 4. Test-driven development

**Write the test first, then implement.**

Every new tool needs at least:
- A registration test — assert the tool name appears in `client.list_tools()`
- A happy-path test — call the tool and assert the result matches expectations
- An error/edge-case test — bad input, empty state, or missing resource

Use the fixtures in `tests/conftest.py`:
- `mcp_server` — a live FastMCP instance backed by a real Mealie container
- `mealie_http` (defined in `test_cleanup.py`, copy the pattern) — a raw `httpx.AsyncClient` for test setup and teardown
- Tests are `async` by default (`asyncio_mode = "auto"` in `pyproject.toml`)

When adding a new required endpoint to a composed tool, add it to `REQUIRED_ENDPOINTS` in `tests/test_routes.py` so the spec check pins it.

Run tests with:
```
.venv/bin/pytest tests/ -v
```
