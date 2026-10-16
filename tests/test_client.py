"""HTTP edge behaviour: auth rules, retries, rate limits, error mapping. respx fakes the store."""

import json

import httpx
import pytest
import respx
from conftest import API, no_sleep

from woo_agent_bridge.client import WooClient
from woo_agent_bridge.config import Settings
from woo_agent_bridge.errors import ConnectorError


def client(settings, **kw) -> WooClient:
    return WooClient(settings, sleep=no_sleep, **kw)


@respx.mock
async def test_https_uses_basic_auth_not_query(settings):
    route = respx.get(f"{API}/orders").respond(json=[], headers={"X-WP-Total": "0", "X-WP-TotalPages": "0"})
    await client(settings).get_page("/orders")
    req = route.calls.last.request
    assert req.headers["Authorization"].startswith("Basic ")
    assert "consumer_key" not in str(req.url)


def test_s8_plain_http_refused_by_default():
    s = Settings(_env_file=None, base_url="http://shop.test", consumer_key="ck", consumer_secret="cs")
    with pytest.raises(ConnectorError) as e:
        WooClient(s)
    assert e.value.code == "INSECURE_TRANSPORT"


@respx.mock
async def test_plain_http_opt_in_uses_query_keys():
    s = Settings(
        _env_file=None, base_url="http://shop.test", consumer_key="ck", consumer_secret="cs", allow_insecure_http=True
    )
    route = respx.get("http://shop.test/wp-json/wc/v3/orders").respond(json=[])
    await WooClient(s, sleep=no_sleep).get("/orders")
    assert route.calls.last.request.url.params["consumer_key"] == "ck"


@respx.mock
async def test_s6_429_then_success_honours_retry_after(settings):
    waits: list[float] = []

    async def record(s):
        waits.append(s)

    respx.get(f"{API}/orders/1").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(503),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    woo = WooClient(settings, sleep=record)
    assert await woo.get("/orders/1") == {"id": 1}
    assert 2.0 <= waits[0] <= 2.25  # server's Retry-After (+ small jitter)
    assert woo.stats == {"requests": 3, "retries": 2, "rate_limited": 1}


@respx.mock
async def test_s7_persistent_429_becomes_structured_retryable_error(settings):
    respx.get(f"{API}/orders/1").respond(429, headers={"Retry-After": "7"})
    with pytest.raises(ConnectorError) as e:
        await client(settings).get("/orders/1")
    err = json.loads(e.value.to_json())["error"]
    assert err["code"] == "RATE_LIMITED" and err["retryable"] is True and err["retry_after_s"] == 7


@respx.mock
async def test_network_errors_are_retried(settings):
    respx.get(f"{API}/orders/1").mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})])
    assert await client(settings).get("/orders/1") == {}


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"code": "woocommerce_rest_cannot_view"}, "AUTH_FAILED"),
        (403, {"code": "woocommerce_rest_cannot_view"}, "FORBIDDEN"),
        (404, {"code": "woocommerce_rest_shop_order_invalid_id"}, "NOT_FOUND"),
        (400, {"code": "rest_invalid_param", "message": "Invalid parameter(s): status"}, "BAD_REQUEST"),
        (500, {}, "UPSTREAM_ERROR"),
    ],
)
@respx.mock
async def test_error_mapping_is_not_retried(settings, status, body, code):
    route = respx.get(f"{API}/orders/1").respond(status, json=body)
    with pytest.raises(ConnectorError) as e:
        await client(settings).get("/orders/1")
    assert e.value.code == code
    assert route.call_count == 1  # client errors fail fast


@respx.mock
async def test_401_hint_mentions_the_http_gotcha(settings):
    respx.get(f"{API}/orders/1").respond(401, json={})
    with pytest.raises(ConnectorError) as e:
        await client(settings).get("/orders/1")
    assert "HTTPS" in e.value.hint


@respx.mock
async def test_html_instead_of_json_is_explained(settings):
    respx.get(f"{API}/orders").respond(200, text="<html>Maintenance</html>")
    with pytest.raises(ConnectorError) as e:
        await client(settings).get("/orders")
    assert "permalinks" in e.value.hint


@respx.mock
async def test_pagination_headers(settings):
    respx.get(f"{API}/orders").respond(json=[{"id": 1}], headers={"X-WP-Total": "44", "X-WP-TotalPages": "9"})
    page = await client(settings).get_page("/orders")
    assert (page.total, page.total_pages) == (44, 9)
