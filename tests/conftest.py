import json
from pathlib import Path

import pytest

from woo_agent_bridge.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://shop.test"
API = f"{BASE}/wp-json/wc/v3"


def load(name: str):
    """Real WooCommerce 11.2 responses recorded from the Docker store (fake data)."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings() -> Settings:
    # _env_file=None: tests must never pick up real keys from .env / docker/out.
    return Settings(
        _env_file=None, base_url=BASE, consumer_key="ck_test", consumer_secret="cs_test", rate_per_sec=1000, burst=1000
    )


async def no_sleep(_: float) -> None:
    return None
