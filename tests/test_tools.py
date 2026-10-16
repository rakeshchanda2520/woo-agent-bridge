"""Scenario tests through the real MCP tool layer (in-process), with the store faked by respx
using recorded WooCommerce responses."""

import json
from pathlib import Path

import pytest
import respx
from conftest import API, load, no_sleep
from mcp.server.mcpserver.exceptions import ToolError

from woo_agent_bridge.client import WooClient
from woo_agent_bridge.server import build_server


@pytest.fixture
def server(settings):
    return build_server(settings, WooClient(settings, sleep=no_sleep))


async def call(server, name, args):
    res = await server.call_tool(name, args)
    assert not res.is_error, res.content
    return res.structured_content


def pages(items, total_pages=1):
    return {"json": items, "headers": {"X-WP-Total": str(len(items)), "X-WP-TotalPages": str(total_pages)}}


@respx.mock
async def test_s1_get_order_is_slim_and_masked(server):
    respx.get(f"{API}/orders/18").respond(json=load("order_paid"))
    out = await call(server, "get_order", {"order_id": 18})
    assert out["status"] == "completed" and out["razorpay_payment_id"] == "pay_SEEDS1000000001"
    assert out["customer"]["email"] == "aa***@example.com" and out["customer"]["name"] == "Aarav S."
    raw = json.dumps(out)
    for leaked in ("order_key", "payment_url", "customer_ip_address", "wc_order_", "aarav.sharma"):
        assert leaked not in raw
    assert out["created_at"].endswith("Z")


@respx.mock
async def test_unmasked_when_deployment_allows(settings):
    settings.mask_pii = False
    srv = build_server(settings, WooClient(settings, sleep=no_sleep))
    respx.get(f"{API}/orders/18").respond(json=load("order_paid"))
    out = await call(srv, "get_order", {"order_id": 18})
    assert out["customer"]["email"] == "aarav.sharma@example.com"


@respx.mock
async def test_s2_search_by_email_matches_exactly_despite_fuzzy_store_search(server):
    # The store's fuzzy search returned 6 orders; only those with this exact email AND status=failed count.
    route = respx.get(f"{API}/orders").respond(**pages(load("orders_search_diya")))
    out = await call(server, "search_orders", {"email": "Diya.Iyer@example.com", "status": "failed"})
    assert [o["order_id"] for o in out["orders"]] == [19]
    assert out["scan_complete"] is True
    assert route.calls.last.request.url.params["search"] == "Diya.Iyer@example.com"


@respx.mock
async def test_s2_search_by_razorpay_payment_id_scans_pages(server):
    others = [load("order_paid")] * 100
    route = respx.get(f"{API}/orders").mock(
        side_effect=[
            respx.MockResponse(**pages(others, total_pages=2)),
            respx.MockResponse(**pages([load("order_mismatch")], total_pages=2)),
        ]
    )
    out = await call(server, "search_orders", {"razorpay_payment_id": "pay_SEEDS2000000002"})
    assert [o["order_id"] for o in out["orders"]] == [19]
    assert out["scanned_orders"] == 101 and route.call_count == 2
    assert "search" not in route.calls.last.request.url.params  # no fuzzy term for id lookups


@respx.mock
async def test_payment_scan_is_bounded(settings):
    settings.payment_scan_max_orders = 200
    srv = build_server(settings, WooClient(settings, sleep=no_sleep))
    route = respx.get(f"{API}/orders").respond(**pages([load("order_paid")] * 100, total_pages=50))
    out = await call(srv, "search_orders", {"razorpay_payment_id": "pay_NOTTHERE123"})
    assert out["matched"] == 0 and out["scan_complete"] is False and route.call_count == 2


@respx.mock
async def test_order_number_lookup(server):
    respx.get(f"{API}/orders/19").respond(json=load("order_mismatch"))
    respx.get(f"{API}/orders/5").respond(404, json={"code": "woocommerce_rest_shop_order_invalid_id"})
    assert (await call(server, "search_orders", {"order_number": "19"}))["matched"] == 1
    assert (await call(server, "search_orders", {"order_number": "5"}))["matched"] == 0


async def test_search_requires_a_criterion(server):
    with pytest.raises(ToolError) as e:
        await server.call_tool("search_orders", {"status": "failed"})
    assert "BAD_REQUEST" in str(e.value)


@pytest.mark.parametrize(
    "args",
    [
        {"razorpay_payment_id": "pay_x' OR 1=1"},
        {"email": "not-an-email"},
        {"order_number": "19; DROP"},
        {"status": "trash"},  # valid in WooCommerce, but not something an agent should see
    ],
)
async def test_injection_shaped_input_never_reaches_store(server, args):
    with respx.mock(assert_all_called=False) as store:
        route = store.get(url__startswith=API)
        with pytest.raises(ToolError):
            await server.call_tool("search_orders", args)
        assert route.call_count == 0


@respx.mock
async def test_s9_customer_note_is_fenced(server):
    respx.get(f"{API}/orders/26").respond(json=load("order_injection"))
    note = (await call(server, "get_order", {"order_id": 26}))["customer_note"]
    assert note["untrusted"] is True and "ignore previous instructions" in note["text"]


@respx.mock
async def test_s10_not_found_is_structured(server):
    respx.get(f"{API}/orders/999").respond(404, json={"code": "woocommerce_rest_shop_order_invalid_id"})
    with pytest.raises(ToolError) as e:
        await server.call_tool("get_order", {"order_id": 999})
    payload = json.loads(str(e.value).split(": ", 1)[1])
    assert payload["error"]["code"] == "NOT_FOUND" and payload["error"]["retryable"] is False


@respx.mock
async def test_s5_stock_by_sku_for_variable_product(server):
    respx.get(f"{API}/products").respond(json=[load("product_variable")])
    respx.get(f"{API}/products/15/variations").respond(json=load("variations"))
    out = await call(server, "get_stock_status", {"sku": "CC-TULSI"})
    assert out["verdict"] == "varies"


@respx.mock
async def test_s11_list_orders_pagination(server):
    route = respx.get(f"{API}/orders").respond(
        json=[load("order_paid")], headers={"X-WP-Total": "18", "X-WP-TotalPages": "4"}
    )
    out = await call(server, "list_orders", {"status": "completed", "per_page": 5, "created_after": "2026-10-01"})
    assert (out["total"], out["total_pages"], out["has_more"]) == (18, 4, True)
    assert route.calls.last.request.url.params["after"] == "2026-10-01T00:00:00"


async def test_tool_spec_is_in_sync_with_code(server):
    """docs/tool_spec.json is generated (`make spec`); fail if someone changes a tool and forgets."""
    spec = json.loads((Path(__file__).parents[1] / "docs/tool_spec.json").read_text(encoding="utf-8"))
    live = [t.model_dump(by_alias=True, exclude_none=True) for t in await server.list_tools()]
    assert spec["tools"] == live
    assert all(t["annotations"]["readOnlyHint"] for t in live)
