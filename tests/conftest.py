"""Session-scoped fixtures that spin up a real Mealie instance via Testcontainers."""

from __future__ import annotations

import time

import httpx
import pytest
from testcontainers.core.container import DockerContainer

from config import Settings
from server import build_server

MEALIE_IMAGE = "ghcr.io/mealie-recipes/mealie:latest"
MEALIE_PORT = 9000
MEALIE_EMAIL = "changeme@example.com"
MEALIE_PASSWORD = "MyPassword"


def _wait_ready(base_url: str, timeout: int = 120) -> None:
    """Poll /api/app/about until Mealie responds 200 or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/api/app/about", timeout=5)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f"Mealie did not become ready at {base_url} within {timeout}s")


def _get_token(base_url: str) -> str:
    resp = httpx.post(
        f"{base_url}/api/auth/token",
        data={"username": MEALIE_EMAIL, "password": MEALIE_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def mealie_base_url():
    with (
        DockerContainer(MEALIE_IMAGE)
        .with_exposed_ports(MEALIE_PORT)
        .with_env("DEFAULT_EMAIL", MEALIE_EMAIL)
        .with_env("DEFAULT_PASSWORD", MEALIE_PASSWORD)
        .with_env("ALLOW_SIGNUP", "false")
    ) as container:
        port = container.get_exposed_port(MEALIE_PORT)
        url = f"http://localhost:{port}"
        _wait_ready(url)
        yield url


@pytest.fixture(scope="session")
def mealie_settings(mealie_base_url):
    token = _get_token(mealie_base_url)
    return Settings(
        mealie_base_url=mealie_base_url,
        mealie_api_token=token,
        host="0.0.0.0",
        port=8000,
        transport="http",
        log_level="DEBUG",
    )


@pytest.fixture
def mcp_server(mealie_settings):
    return build_server(mealie_settings)
