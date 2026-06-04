# mealie-mcp

MCP server for [Mealie](https://github.com/mealie-recipes/mealie), generated
directly from Mealie's OpenAPI spec via [FastMCP](https://gofastmcp.com).
No hand-written endpoint wrappers — tools come from the spec, curated to a
meal-planning subset.

## How it works

At startup the server:
1. Fetches `${MEALIE_BASE_URL}/openapi.json` from your Mealie instance.
2. Maps a curated subset of routes to MCP tools (see [src/routes.py](src/routes.py)).
3. Serves over streamable-HTTP for remote AI clients.

Curated surface (edit `INCLUDE_PATTERNS` in [src/routes.py](src/routes.py)):
recipes, meal plans, shopping lists/items, categories, tags, foods, units.
Everything else (admin, users, backups…) is excluded.

## Configuration

Copy `.env.example` to `.env`:

| Var | Required | Default | Notes |
|-----|----------|---------|-------|
| `MEALIE_BASE_URL` | yes | — | No trailing slash, no `/api`. |
| `MEALIE_API_TOKEN` | yes | — | Mealie > User profile > Manage API Tokens. |
| `MCP_HOST` | no | `0.0.0.0` | |
| `MCP_PORT` | no | `8000` | |
| `MCP_TRANSPORT` | no | `http` | streamable-HTTP. |
| `LOG_LEVEL` | no | `INFO` | |

## Develop (devcontainer)

Open the repo in VS Code → "Reopen in Container". It builds Python 3.12 + uv,
runs `uv sync`, forwards port 8000. Then:

```bash
cp .env.example .env   # fill in MEALIE_BASE_URL + token
uv run src/server.py
```

Server at `http://localhost:8000/mcp`.

## Deploy (NAS / Docker)

```bash
cp .env.example .env   # fill in values
docker compose up -d --build
```

Point your AI client at `http://<nas-host>:8000/mcp`.

## Connect an MCP client

```json
{
  "mcpServers": {
    "mealie": {
      "url": "http://<nas-host>:8000/mcp"
    }
  }
}
```
