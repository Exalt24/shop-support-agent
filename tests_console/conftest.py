"""Console tests need the local Postgres (docker, port 54931) with the schema applied:
    python -m console_api.migrate --reset
Seeding runs as the superuser (which bypasses RLS on purpose); every assertion runs as console_app.
"""
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg
import pytest

from console_api import db
from console_api.migrate import ADMIN_DSN


@pytest.fixture(autouse=True)
def clean_db():
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute("truncate audit_log, handoffs, messages, conversations, memberships, users, tenants restart identity cascade")
    yield
    db.close_pool()


@pytest.fixture
def admin():
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        yield c


class World:
    """Two stores, an admin and an agent on A, an agent on B, and one conversation per store."""

    def __init__(self, admin):
        def one(sql, *args):
            return admin.execute(sql, args).fetchone()[0]
        self.ta = one("insert into tenants (slug, name, shop_domain, shopify_token_enc, policy_md) "
                      "values ('store-a','Store A','a.myshopify.com','x','## Returns\nthirty days') returning id")
        self.tb = one("insert into tenants (slug, name, shop_domain, shopify_token_enc, policy_md) "
                      "values ('store-b','Store B','b.myshopify.com','x','## Returns\nten days') returning id")
        self.admin_a = one("insert into users (email,name,password_hash) values ('admin-a@x.com','Admin A','h') returning id")
        self.agent_a = one("insert into users (email,name,password_hash) values ('agent-a@x.com','Agent A','h') returning id")
        self.agent_b = one("insert into users (email,name,password_hash) values ('agent-b@x.com','Agent B','h') returning id")
        admin.execute("insert into memberships values (%s,%s,'admin'),(%s,%s,'agent'),(%s,%s,'agent')",
                      (self.admin_a, self.ta, self.agent_a, self.ta, self.agent_b, self.tb))
        self.conv_a = one("insert into conversations (tenant_id, customer_email, status) values (%s,'cust-a@x.com','escalated') returning id", self.ta)
        self.conv_b = one("insert into conversations (tenant_id, customer_email, status) values (%s,'cust-b@x.com','escalated') returning id", self.tb)
        admin.execute("insert into messages (tenant_id, conversation_id, sender, body) values (%s,%s,'customer','hello a'),(%s,%s,'customer','hello b')",
                      (self.ta, self.conv_a, self.tb, self.conv_b))
        admin.execute("insert into handoffs (tenant_id, conversation_id, reason) values (%s,%s,'legal'),(%s,%s,'legal')",
                      (self.ta, self.conv_a, self.tb, self.conv_b))
        admin.execute("insert into audit_log (tenant_id, actor, action) values (%s,'agent','chat.escalated'),(%s,'agent','chat.escalated')",
                      (self.ta, self.tb))


@pytest.fixture
def world(admin):
    return World(admin)
