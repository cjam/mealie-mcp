"""Runtime configuration loaded from environment.

Fail fast at startup if required secrets are missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # load .env for local/dev; no-op if absent or in prod with real env


@dataclass(frozen=True)
class Settings:
    """Immutable server settings sourced from env vars."""

    mealie_base_url: str
    mealie_api_token: str
    host: str
    port: int
    transport: str
    log_level: str

    @property
    def openapi_url(self) -> str:
        return f"{self.mealie_base_url}/openapi.json"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in your .env / container environment."
        )
    return value


def load_settings() -> Settings:
    """Build Settings from the environment, validating required fields."""
    base_url = _require("MEALIE_BASE_URL").rstrip("/")
    token = _require("MEALIE_API_TOKEN")

    return Settings(
        mealie_base_url=base_url,
        mealie_api_token=token,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
        transport=os.environ.get("MCP_TRANSPORT", "http"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
