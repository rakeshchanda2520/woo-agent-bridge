"""Contract tests against the real Docker WooCommerce store. Opt-in: `make test-live` (needs `make up seed`)."""

import json
from pathlib import Path

import pytest

from woo_agent_bridge.server import build_server

pytestmark = pytest.mark.live
REFS_FILE = Path(__file__).parents[1] / "docker/out/seed_refs.json"


@pytest.fixture(scope="module")
def refs():
    if not REFS_FILE.exists():
        pytest.skip("store not seeded; run `make up seed`")
    return json.loads(REFS_FILE.read_text())


@pytest.fixture
def server():
    return build_server()  # real settings from docker/out/keys.env


async def call(server, name, args):
    res = await server.call_tool(name, args)
    return res.structured_content


async def test_live_payment_verdicts(server, refs):
    expected = {
        "S1-completed": "paid",
        "S2-mismatch": "mismatch",
        "S3-pending": "payment_pending",
        "cod-processing": "not_razorpay",
        "refunded": "refunded",
        "cancelled-unpaid": "cancelled_unpaid",
    }
    for ref, verdict in expected.items():
        out = await call(server, "get_order_payment_context", {"order_id": refs[ref]})
        assert out["verdict"] == verdict, ref


async def test_live_search_by_payment_id(server, refs):
    out = await call(server, "search_orders", {"razorpay_payment_id": "pay_SEEDS2000000002"})
    assert [o["order_id"] for o in out["orders"]] == [refs["S2-mismatch"]] and out["scan_complete"]


async def test_live_stock(server):
    assert (await call(server, "get_stock_status", {"sku": "CC-MASALA-250"}))["verdict"] == "low_stock"
    assert (await call(server, "get_stock_status", {"sku": "CC-TULSI-500"}))["verdict"] == "out_of_stock"
    assert (await call(server, "get_stock_status", {"sku": "CC-KAHWA-100"}))["verdict"] == "backorder"
    assert (await call(server, "get_stock_status", {"sku": "CC-GIFT-BOX"}))["verdict"] == "in_stock"


async def test_live_pagination(server):
    p1 = await call(server, "list_orders", {"per_page": 10})
    p2 = await call(server, "list_orders", {"per_page": 10, "page": 2})
    assert p1["total"] >= 44 and p1["has_more"]
    assert not {o["order_id"] for o in p1["orders"]} & {o["order_id"] for o in p2["orders"]}
