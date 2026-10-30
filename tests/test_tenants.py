"""Tenants: the config is checked on load, the site key only selects, and each tenant gets its own routing table and desk."""
import copy
import json
from pathlib import Path

import pytest

from agent.enquiry import ROUTES, routes_with_queues, team_word
from agent.tenants import (Tenant, TenantConfigError, TenantRegistry, desk_for, normalise_origin, parse_tenant, zoho_settings_for)
from agent.zoho_crm import ZohoCrmClient, ZohoSettings
from agent.zoho_fake import FakeZoho

DEMO = Path(__file__).resolve().parent.parent / "widget" / "demo" / "tenants.demo.json"


def raw(i=0):
    return copy.deepcopy(json.loads(DEMO.read_text(encoding="utf-8"))["tenants"][i])


def test_the_demo_file_loads_and_has_two_different_tenants():
    reg = TenantRegistry.from_file(DEMO)
    a, b = reg.tenants
    assert (a.id, b.id) == ("harbour-heating", "loomhouse-homewares")
    assert a.routing["quote_request"] != b.routing["quote_request"]
    assert reg.by_site_key("pk_harbour_demo_01") is a and reg.by_site_key("pk_loomhouse_demo_02") is b


def test_the_site_key_only_selects():
    reg = TenantRegistry.from_file(DEMO)
    assert reg.by_site_key("pk_nope_nope_nope") is None
    assert reg.by_site_key(None) is None and reg.by_site_key(["pk_harbour_demo_01"]) is None
    assert set(reg.by_site_key("pk_harbour_demo_01").public()) == {"name", "initials", "primary", "on_primary", "greeting"}


@pytest.mark.parametrize("edit,why", [
    (lambda r: r.update(allowed_origins=["*"]), "wildcard origin"),
    (lambda r: r.update(allowed_origins=["https://a.example/path"]), "origin with a path"),
    (lambda r: r.update(allowed_origins=[]), "no origins"),
    (lambda r: r.update(allowed_origins=["javascript://x"]), "not http"),
    (lambda r: r["theme"].update(primary="red"), "named colour"),
    (lambda r: r["theme"].update(primary="#12345"), "short hex"),
    (lambda r: r["theme"].update(on_primary="url(x)"), "css injection through a colour"),
    (lambda r: r["theme"].update(initials="<b>"), "markup in initials"),
    (lambda r: r.update(site_key="harbour"), "key without prefix"),
    (lambda r: r.update(id="Harbour Heating"), "id with spaces"),
    (lambda r: r.update(routing={"order_support": "support"}), "routing a non-lead intent"),
    (lambda r: r.update(routing={"quote_request": "Sales Team"}), "queue name with spaces"),
    (lambda r: r.update(routing={}), "no routing"),
    (lambda r: r.update(assignment_rules={"nobody": "LAR-1"}), "rule for a queue nothing routes to"),
    (lambda r: r.update(zoho="../etc"), "zoho key that is not a name"),
    (lambda r: r.update(greeting=""), "empty greeting"),
])
def test_a_bad_tenant_is_refused_at_load(edit, why):
    r = raw()
    edit(r)
    with pytest.raises(TenantConfigError):
        parse_tenant(r)


def test_two_tenants_cannot_share_a_key_or_an_id():
    a, b = parse_tenant(raw(0)), parse_tenant(raw(1))
    with pytest.raises(TenantConfigError):
        TenantRegistry([a, a])
    r = raw(1)
    r["site_key"] = raw(0)["site_key"]
    with pytest.raises(TenantConfigError):
        TenantRegistry([a, parse_tenant(r)])
    with pytest.raises(TenantConfigError):
        TenantRegistry([])
    assert TenantRegistry([a, b]).origin_known("http://127.0.0.1:8101") and not TenantRegistry([a, b]).origin_known("http://evil.test")


@pytest.mark.parametrize("value,expected", [("HTTPS://Shop.Example.COM", "https://shop.example.com"), ("http://localhost:8080", "http://localhost:8080"),
                                            ("https://shop.example.com/", None), ("https://*.example.com", None), ("null", None), (5, None),
                                            ("https://a.example:99999999", None), ("ftp://a.example", None)])
def test_origin_normalisation(value, expected):
    assert normalise_origin(value) == expected


def test_a_tenants_routing_changes_only_the_queue():
    routes = routes_with_queues({"quote_request": "trade-accounts"})
    assert routes["quote_request"].queue == "trade-accounts"
    assert routes["quote_request"].task_subject == ROUTES["quote_request"].task_subject and routes["quote_request"].priority == "High"
    assert routes["sales_enquiry"] == ROUTES["sales_enquiry"] and routes["general"] == ROUTES["general"]
    with pytest.raises(ValueError):
        routes_with_queues({"made_up": "x"})
    assert ROUTES["quote_request"].queue == "sales"  # the default table is never edited in place


def test_a_visitor_hears_a_tenants_queue_by_its_name():
    assert team_word("sales") == "sales" and team_word("trade-accounts") == "trade accounts" and team_word("dispatch") == "dispatch"


def test_zoho_settings_come_from_the_tenants_own_variables():
    t = parse_tenant(raw(0))
    env = {"ZOHO_HARBOUR_CLIENT_ID": "id-a", "ZOHO_HARBOUR_CLIENT_SECRET": "secret-a", "ZOHO_HARBOUR_REFRESH_TOKEN": "refresh-a",
           "ZOHO_HARBOUR_REGION": "au", "ZOHO_LOOMHOUSE_CLIENT_ID": "id-b", "ZOHO_CLIENT_ID": "unscoped"}
    s = zoho_settings_for(t, env)
    assert (s.client_id, s.region, s.accounts_url) == ("id-a", "au", "https://accounts.zoho.com.au")
    with pytest.raises(RuntimeError, match="ZOHO_HARBOUR_CLIENT_ID"):
        zoho_settings_for(t, {"ZOHO_HARBOUR_CLIENT_SECRET": "x", "ZOHO_HARBOUR_REFRESH_TOKEN": "y", "ZOHO_CLIENT_ID": "unscoped"})
    with pytest.raises(RuntimeError):  # another tenant's variables and the unscoped ones are never borrowed
        zoho_settings_for(t, {"ZOHO_LOOMHOUSE_CLIENT_ID": "i", "ZOHO_LOOMHOUSE_CLIENT_SECRET": "s", "ZOHO_LOOMHOUSE_REFRESH_TOKEN": "r"})


def test_each_tenants_desk_has_its_own_namespace_routes_and_rules():
    a, b = parse_tenant(raw(0)), parse_tenant(raw(1))
    crm = ZohoCrmClient(ZohoSettings("c", "s", "r"), FakeZoho(), sleep=lambda s: None)
    da, db = desk_for(a, crm), desk_for(b, crm)
    assert (da.tenant, db.tenant) == ("harbour-heating", "loomhouse-homewares")
    assert da.routes["quote_request"].queue == "estimating" and db.routes["quote_request"].queue == "trade-accounts"
    assert da.capture._rules == {"estimating": "LAR-HARBOUR-ESTIMATING", "dispatch": "LAR-HARBOUR-DISPATCH"}
    assert db.capture._rules == {"trade-accounts": "LAR-LOOMHOUSE-TRADE"}
    assert da.tracker is not db.tracker and da.capture is not db.capture
