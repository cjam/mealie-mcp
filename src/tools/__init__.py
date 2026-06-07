"""Wire all domain tool modules into a FastMCP instance."""

from __future__ import annotations

from typing import Any

import httpx

from tools.cleanup import register_cleanup_tools
from tools.planning import register_planning_tools
from tools.recipes import register_recipe_tools
from tools.shopping import register_shopping_tools


def register_all_tools(mcp: Any, client: httpx.AsyncClient) -> None:
    register_cleanup_tools(mcp, client)
    register_recipe_tools(mcp, client)
    register_planning_tools(mcp, client)
    register_shopping_tools(mcp, client)
