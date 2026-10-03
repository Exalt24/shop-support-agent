"""ShopifyAdmin against canned HTTP responses: identity check, outage handling, throttle-as-200."""
import httpx
import pytest

from agent.config import Settings
from agent.shopify import ShopifyAdmin, ShopifyError

SETTINGS = Settings(groq_key="x", shop="demo.myshopify.com", shopify_token="t")

ORDER_NODE = {
    "name": "#1019", "email": "Miguel@Example.com", "createdAt": "2026-09-20T10:00:00Z",
    "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "FULFILLED",
    "totalPriceSet": {"shopMoney": {"amount": "250.0", "currencyCode": "USD"}},
    "lineItems": {"nodes": [{"title": "USB Audio Interface", "quantity": 2}]},
    "fulfillments": [{"trackingInfo": [{"number": "TRK123", "company": "DHL"}]}],
}


def fake_post(status=200, body=None):
    def _post(url, headers=None, json=None, timeout=None):
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))
    return _post


def test_the_matching_email_returns_the_order_with_tracking(monkeypatch):
    monkeypatch.setattr(httpx, "post", fake_post(body={"data": {"orders": {"nodes": [ORDER_NODE]}}}))
    o = ShopifyAdmin(SETTINGS).find_order("1019", "miguel@example.com")  # case-insensitive match
    assert o and o.tracking == ("DHL TRK123",) and o.fulfillment_status == "fulfilled"


def test_a_different_email_is_the_same_as_no_order(monkeypatch):
    monkeypatch.setattr(httpx, "post", fake_post(body={"data": {"orders": {"nodes": [ORDER_NODE]}}}))
    assert ShopifyAdmin(SETTINGS).find_order("1019", "someone.else@example.com") is None


def test_no_nodes_is_none(monkeypatch):
    monkeypatch.setattr(httpx, "post", fake_post(body={"data": {"orders": {"nodes": []}}}))
    assert ShopifyAdmin(SETTINGS).find_order("1019", "a@example.com") is None


def test_a_throttle_delivered_as_http_200_raises_instead_of_reading_as_empty(monkeypatch):
    monkeypatch.setattr(httpx, "post", fake_post(body={"errors": [{"message": "Throttled"}]}))
    with pytest.raises(ShopifyError):
        ShopifyAdmin(SETTINGS).find_order("1019", "a@example.com")


def test_product_search_only_asks_for_active_products(monkeypatch):
    seen = {}

    def _post(url, headers=None, json=None, timeout=None):
        seen["q"] = json["variables"]["q"]
        return httpx.Response(200, json={"data": {"products": {"nodes": []}}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", _post)
    ShopifyAdmin(SETTINGS).search_products("keyboard")
    assert seen["q"].startswith("status:active ")


def test_a_server_error_raises(monkeypatch):
    monkeypatch.setattr(httpx, "post", fake_post(status=502, body={}))
    with pytest.raises(ShopifyError):
        ShopifyAdmin(SETTINGS).find_order("1019", "a@example.com")
