"""Mark ONE order on the development store as shipped, with a tracking number, so the demo has a shipped case.

Writes to the dev store only (dac-dev-store, fake customers, no real data). Idempotent: if the order
already carries tracking it does nothing. Refuses to run against any shop that is not the dev store.
"""
from __future__ import annotations

import sys

import httpx

from agent.config import SHOPIFY_API_VERSION, load_settings

ORDER = "#1018"
TRACKING = ("DHL Express", "JD0146000050", "https://www.dhl.com/track?id=JD0146000050")
DEV_STORE = "dac-dev-store.myshopify.com"

LOOKUP = """query($q: String!) { orders(first: 1, query: $q) { nodes { id name email
  fulfillments { trackingInfo { number } }
  fulfillmentOrders(first: 5) { nodes { id status } } } } }"""

CREATE = """mutation($f: FulfillmentInput!) { fulfillmentCreate(fulfillment: $f) {
  fulfillment { id status trackingInfo { number company } } userErrors { field message } } }"""


def main() -> int:
    s = load_settings()
    if s.shop != DEV_STORE:
        print("refusing: %s is not the development store" % s.shop)
        return 2
    url = "https://%s/admin/api/%s/graphql.json" % (s.shop, SHOPIFY_API_VERSION)
    headers = {"X-Shopify-Access-Token": s.shopify_token, "Content-Type": "application/json"}

    def gql(query, variables):
        r = httpx.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=30)
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise SystemExit("GraphQL error: %s" % str(body["errors"])[:300])
        return body["data"]

    node = gql(LOOKUP, {"q": "name:%s" % ORDER})["orders"]["nodes"][0]
    if any(t["number"] for f in node["fulfillments"] for t in f["trackingInfo"]):
        print("%s already has tracking, nothing to do" % ORDER)
        return 0
    open_fos = [fo["id"] for fo in node["fulfillmentOrders"]["nodes"] if fo["status"] in ("OPEN", "IN_PROGRESS")]
    if not open_fos:
        print("no open fulfillment order on %s" % ORDER)
        return 1
    data = gql(CREATE, {"f": {
        "notifyCustomer": False,
        "trackingInfo": {"company": TRACKING[0], "number": TRACKING[1], "url": TRACKING[2]},
        "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fo} for fo in open_fos]}})["fulfillmentCreate"]
    if data["userErrors"]:
        print("refused by Shopify: %s" % data["userErrors"])
        return 1
    print("%s marked shipped: %s" % (ORDER, data["fulfillment"]["trackingInfo"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
