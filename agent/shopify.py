"""Read-only Shopify Admin GraphQL lookups: one order (identity-checked) and a product search.

Two rules that matter more than the queries:
  * "the request failed" is never reported as "there is no such order". A failed read raises
    ShopifyError and the agent escalates, because telling a real customer their order does not
    exist is worse than saying a person will check.
  * a lookup that finds an order under a DIFFERENT email returns the same answer as no order at
    all. Distinguishing them would let anyone enumerate which order numbers exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .config import SHOPIFY_API_VERSION, Settings


class ShopifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderFacts:
    name: str
    created_at: str
    financial_status: str
    fulfillment_status: str
    total: str
    currency: str
    items: tuple[str, ...]
    tracking: tuple[str, ...]  # "carrier number" strings, empty until it ships

    def as_text(self) -> str:
        lines = [
            "Order %s, placed %s" % (self.name, self.created_at[:10]),
            "Payment status: %s" % self.financial_status,
            "Fulfillment status: %s" % self.fulfillment_status,
            "Total: %s %s" % (self.total, self.currency),
            "Items: " + "; ".join(self.items),
        ]
        lines.append("Tracking: " + ("; ".join(self.tracking) if self.tracking else "none yet, it has not shipped"))
        return "\n".join(lines)


@dataclass(frozen=True)
class ProductFacts:
    title: str
    price: str
    currency: str
    in_stock: bool
    description: str

    def as_text(self) -> str:
        return "Product: %s | Price: %s %s | %s | %s" % (
            self.title, self.price, self.currency, "in stock" if self.in_stock else "out of stock", self.description[:240])


@dataclass(frozen=True)
class StaffOrderView:
    email: str
    facts: OrderFacts


class Shop(Protocol):
    def find_order(self, order_name: str, email: str) -> OrderFacts | None: ...
    def search_products(self, text: str, limit: int = 3) -> list[ProductFacts]: ...


ORDER_QUERY = """
query($q: String!) { orders(first: 1, query: $q) { nodes {
  name email createdAt displayFinancialStatus displayFulfillmentStatus
  totalPriceSet { shopMoney { amount currencyCode } }
  lineItems(first: 10) { nodes { title quantity } }
  fulfillments { trackingInfo { number company } } } } }
"""

PRODUCT_QUERY = """
query($q: String!, $n: Int!) { products(first: $n, query: $q) { nodes {
  title description totalInventory
  priceRangeV2 { minVariantPrice { amount currencyCode } } } } }
"""


class ShopifyAdmin:
    def __init__(self, settings: Settings, timeout: float = 30.0):
        self._url = "https://%s/admin/api/%s/graphql.json" % (settings.shop, SHOPIFY_API_VERSION)
        self._headers = {"X-Shopify-Access-Token": settings.shopify_token, "Content-Type": "application/json"}
        self._timeout = timeout

    def _run(self, query: str, variables: dict) -> dict:
        try:
            r = httpx.post(self._url, headers=self._headers, json={"query": query, "variables": variables},
                           timeout=self._timeout)
        except httpx.HTTPError as e:
            raise ShopifyError("transport error: %s" % type(e).__name__) from e
        if r.status_code != 200:
            raise ShopifyError("HTTP %s" % r.status_code)
        body = r.json()
        # A throttle arrives as HTTP 200 with an errors array, the quiet way a sync returns nothing.
        if body.get("errors"):
            raise ShopifyError("GraphQL error: %s" % str(body["errors"])[:160])
        return body["data"]

    def find_order(self, order_name: str, email: str) -> OrderFacts | None:
        name = order_name if order_name.startswith("#") else "#" + order_name
        nodes = self._run(ORDER_QUERY, {"q": "name:%s" % name})["orders"]["nodes"]
        if not nodes:
            return None
        n = nodes[0]
        if (n.get("email") or "").strip().lower() != email.strip().lower():
            return None  # same answer as "no such order", on purpose
        money = n["totalPriceSet"]["shopMoney"]
        tracking = tuple(
            ("%s %s" % (t.get("company") or "carrier", t["number"])).strip()
            for f in n["fulfillments"] for t in f["trackingInfo"] if t.get("number"))
        return OrderFacts(
            name=n["name"], created_at=n["createdAt"], financial_status=n["displayFinancialStatus"].lower(),
            fulfillment_status=n["displayFulfillmentStatus"].lower(), total=str(money["amount"]),
            currency=money["currencyCode"],
            items=tuple("%sx %s" % (i["quantity"], i["title"]) for i in n["lineItems"]["nodes"]),
            tracking=tracking)

    def find_order_for_staff(self, order_name: str) -> "StaffOrderView | None":
        """Staff-side lookup by number, no customer email needed. Never called from the customer-facing agent:
        the agent only has find_order, which enforces the email match. Callers must audit the access."""
        name = order_name if order_name.startswith("#") else "#" + order_name
        nodes = self._run(ORDER_QUERY, {"q": "name:%s" % name})["orders"]["nodes"]
        if not nodes:
            return None
        n = nodes[0]
        money = n["totalPriceSet"]["shopMoney"]
        tracking = tuple(
            ("%s %s" % (t.get("company") or "carrier", t["number"])).strip()
            for f in n["fulfillments"] for t in f["trackingInfo"] if t.get("number"))
        return StaffOrderView(
            email=(n.get("email") or ""),
            facts=OrderFacts(
                name=n["name"], created_at=n["createdAt"], financial_status=n["displayFinancialStatus"].lower(),
                fulfillment_status=n["displayFulfillmentStatus"].lower(), total=str(money["amount"]),
                currency=money["currencyCode"],
                items=tuple("%sx %s" % (i["quantity"], i["title"]) for i in n["lineItems"]["nodes"]),
                tracking=tracking))

    def search_products(self, text: str, limit: int = 3) -> list[ProductFacts]:
        # status:active, always: the store holds draft and archived products, and a shopper must never be
        # told about something they cannot buy.
        nodes = self._run(PRODUCT_QUERY, {"q": "status:active %s" % text, "n": limit})["products"]["nodes"]
        out = []
        for n in nodes:
            price = n["priceRangeV2"]["minVariantPrice"]
            out.append(ProductFacts(
                title=n["title"], price=str(price["amount"]), currency=price["currencyCode"],
                in_stock=(n.get("totalInventory") or 0) > 0, description=n.get("description") or ""))
        return out
