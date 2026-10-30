"""Tenants: one chat backend, several websites. Each site is a tenant with its own look, greeting, allowed origins, routing
table (which intents go to which owner queue) and Zoho settings key.

The site key a widget sends is PUBLIC (it sits in the page's script tag). It is used for one thing: choosing which tenant a
request is about. Nothing is trusted because of it. What a request may do is decided by the tenant it selects and by the Origin
header the browser attached, which a page cannot forge.

The file is JSON (see widget/demo/tenants.demo.json). It is checked on load and a bad file stops the server at start:
a wildcard origin, a colour that is not #rrggbb, a queue name with odd characters, two tenants with one key.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Mapping

from .enquiry import LEAD_INTENTS, EnquiryDesk, routes_with_queues
from .leads import LeadCapture
from .zoho_crm import CrmClient, ZohoSettings

TENANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SITE_KEY = re.compile(r"^pk_[A-Za-z0-9_]{8,64}$")
QUEUE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
INITIALS = re.compile(r"^[A-Za-z0-9]{1,3}$")
ZOHO_KEY = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
ORIGIN = re.compile(r"^(https?)://([a-z0-9]([a-z0-9.\-]*[a-z0-9])?)(:[0-9]{1,5})?$")


class TenantConfigError(ValueError):
    pass


def normalise_origin(value: object) -> str | None:
    """`scheme://host[:port]`, lower-cased, no path, no wildcard, or None. This is what a browser sends in Origin."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if ORIGIN.match(v) else None


@dataclass(frozen=True)
class Tenant:
    id: str
    site_key: str
    name: str
    initials: str
    primary: str
    on_primary: str
    greeting: str
    allowed_origins: frozenset[str]
    routing: Mapping[str, str]            # lead intent -> owner queue
    assignment_rules: Mapping[str, str]   # owner queue -> Zoho assignment rule id (optional)
    zoho: str                             # which Zoho settings to use: ZOHO_<KEY>_CLIENT_ID ...

    def public(self) -> dict:
        """What the widget may know. Never the origins, the routing or the Zoho key."""
        return {"name": self.name, "initials": self.initials, "primary": self.primary, "on_primary": self.on_primary,
                "greeting": self.greeting}


def _text(raw: dict, name: str, lo: int, hi: int, where: str) -> str:
    v = raw.get(name)
    if not isinstance(v, str) or not (lo <= len(v.strip()) <= hi):
        raise TenantConfigError("%s: %s must be text of %d to %d characters" % (where, name, lo, hi))
    return v.strip()


def parse_tenant(raw: object) -> Tenant:
    if not isinstance(raw, dict):
        raise TenantConfigError("a tenant must be an object")
    where = "tenant %r" % raw.get("id")
    tid = raw.get("id")
    if not isinstance(tid, str) or not TENANT_ID.match(tid):
        raise TenantConfigError("%s: id must be lower-case letters, digits and hyphens" % where)
    key = raw.get("site_key")
    if not isinstance(key, str) or not SITE_KEY.match(key):
        raise TenantConfigError("%s: site_key must look like pk_ followed by 8 to 64 letters, digits or underscores" % where)
    theme = raw.get("theme")
    if not isinstance(theme, dict):
        raise TenantConfigError("%s: theme is missing" % where)
    for c in ("primary", "on_primary"):
        if not isinstance(theme.get(c), str) or not COLOUR.match(theme[c]):
            raise TenantConfigError("%s: theme.%s must be #rrggbb" % (where, c))
    initials = theme.get("initials")
    if not isinstance(initials, str) or not INITIALS.match(initials):
        raise TenantConfigError("%s: theme.initials must be 1 to 3 letters or digits" % where)
    origins = raw.get("allowed_origins")
    if not isinstance(origins, list) or not origins:
        raise TenantConfigError("%s: allowed_origins must be a non-empty list" % where)
    clean = set()
    for o in origins:
        n = normalise_origin(o)
        if not n:
            raise TenantConfigError("%s: %r is not an origin (scheme://host[:port], no path, no wildcard)" % (where, o))
        clean.add(n)
    routing = raw.get("routing")
    if not isinstance(routing, dict) or not routing:
        raise TenantConfigError("%s: routing must map lead intents to queues" % where)
    for intent, queue in routing.items():
        if intent not in LEAD_INTENTS:
            raise TenantConfigError("%s: routing key %r is not a lead intent (%s)" % (where, intent, ", ".join(sorted(LEAD_INTENTS))))
        if not isinstance(queue, str) or not QUEUE_NAME.match(queue):
            raise TenantConfigError("%s: queue for %s must be lower-case letters, digits and hyphens" % (where, intent))
    rules = raw.get("assignment_rules") or {}
    if not isinstance(rules, dict) or not all(isinstance(q, str) and isinstance(r, str) and r.strip() for q, r in rules.items()):
        raise TenantConfigError("%s: assignment_rules must map a queue to a rule id" % where)
    if set(rules) - set(routing.values()):
        raise TenantConfigError("%s: assignment_rules names a queue that no intent routes to" % where)
    zoho = raw.get("zoho")
    if not isinstance(zoho, str) or not ZOHO_KEY.match(zoho):
        raise TenantConfigError("%s: zoho must name a settings key (lower-case letters, digits, underscores)" % where)
    return Tenant(tid, key, _text(theme, "name", 2, 40, where), initials, theme["primary"], theme["on_primary"],
                  _text(raw, "greeting", 2, 240, where), frozenset(clean), dict(routing), dict(rules), zoho)


class TenantRegistry:
    def __init__(self, tenants: list[Tenant]):
        if not tenants:
            raise TenantConfigError("no tenants")
        self._by_key: dict[str, Tenant] = {}
        ids = set()
        for t in tenants:
            if t.id in ids or t.site_key in self._by_key:
                raise TenantConfigError("duplicate tenant id or site key: %s" % t.id)
            ids.add(t.id)
            self._by_key[t.site_key] = t
        self.tenants = tuple(tenants)

    def by_site_key(self, key: object) -> Tenant | None:
        """Selection and nothing else."""
        return self._by_key.get(key) if isinstance(key, str) else None

    def origin_known(self, origin: str) -> bool:
        return any(origin in t.allowed_origins for t in self.tenants)

    @classmethod
    def from_dict(cls, data: object) -> "TenantRegistry":
        items = data.get("tenants") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise TenantConfigError('the file must be {"tenants": [...]}')
        return cls([parse_tenant(t) for t in items])

    @classmethod
    def from_file(cls, path: Path) -> "TenantRegistry":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def zoho_settings_for(tenant: Tenant, env: Mapping[str, str]) -> ZohoSettings:
    """From the environment only: ZOHO_<KEY>_CLIENT_ID, ZOHO_<KEY>_CLIENT_SECRET, ZOHO_<KEY>_REFRESH_TOKEN, ZOHO_<KEY>_REGION,
    with <KEY> the tenant's `zoho` value in upper case."""
    prefix = "ZOHO_%s_" % tenant.zoho.upper()
    scoped = {"ZOHO_" + k[len(prefix):]: v for k, v in env.items() if k.startswith(prefix)}
    try:
        return ZohoSettings.from_env(scoped)
    except RuntimeError as e:
        raise RuntimeError("tenant %s: %s (variables are named %s...)" % (tenant.id, str(e).replace("ZOHO_", prefix), prefix)) from e


def desk_for(tenant: Tenant, crm: CrmClient, today: Callable[[], date] = date.today) -> EnquiryDesk:
    """The lead desk for one tenant: its own routing table, its own assignment rules, its own idempotency namespace."""
    routes = routes_with_queues(dict(tenant.routing))
    capture = LeadCapture(crm, tenant=tenant.id, assignment_rules=dict(tenant.assignment_rules), today=today, routes=routes)
    return EnquiryDesk(capture, tenant=tenant.id, routes=routes)
