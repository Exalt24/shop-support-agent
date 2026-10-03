"""Tenant isolation, attacked through the real app role. Every denial is paired with a control proving the same
request succeeds for the right party, since a database that refused everything would pass all of it."""
import psycopg
import pytest

from console_api import db


def ids(conn, table):
    return {str(r["id"]) for r in conn.execute("select id from %s" % table).fetchall()}


def test_the_app_role_is_not_superuser_not_bypassrls_and_not_the_owner(world):
    with db.tx() as c:
        db.assert_role_is_safe(c)  # raises if any of the three is true
        row = c.execute("select rolsuper, rolbypassrls from pg_roles where rolname = current_user").fetchone()
        assert (row["rolsuper"], row["rolbypassrls"]) == (False, False)


def test_a_superuser_connection_is_refused_by_the_safety_check(world, admin):
    with psycopg.connect(db.APP_DSN.replace("console_app:localapp-demo", "postgres:localsuper"), row_factory=psycopg.rows.dict_row) as c:
        with pytest.raises(db.UnsafeDatabaseRole):
            db.assert_role_is_safe(c)


@pytest.mark.parametrize("table", ["conversations", "messages", "handoffs", "audit_log", "tenants"])
def test_an_unbound_request_reads_nothing(world, table):
    with db.tx() as c:
        assert c.execute("select count(*) as n from %s" % table).fetchone()["n"] == 0


def test_a_tenant_sees_only_its_own_rows(world):
    with db.tx(tenant_id=str(world.ta), user_id=str(world.agent_a), role="agent") as c:
        assert ids(c, "conversations") == {str(world.conv_a)}
        assert {r["body"] for r in c.execute("select body from messages").fetchall()} == {"hello a"}
        assert c.execute("select count(*) as n from handoffs").fetchone()["n"] == 1
    with db.tx(tenant_id=str(world.tb), user_id=str(world.agent_b), role="agent") as c:
        assert ids(c, "conversations") == {str(world.conv_b)}  # control: B sees B


def test_a_cross_tenant_write_is_refused_by_the_check_clause(world):
    with db.tx(tenant_id=str(world.ta), user_id=str(world.agent_a), role="agent") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("insert into conversations (tenant_id, customer_email) values (%s, 'x@x.com')", (world.tb,))


def test_a_cross_tenant_update_changes_nothing_and_says_so_by_rowcount(world, admin):
    # The RLS phantom: an UPDATE filtered by USING returns success with ZERO rows rather than raising.
    with db.tx(tenant_id=str(world.ta), user_id=str(world.agent_a), role="agent") as c:
        cur = c.execute("update conversations set status = 'resolved' where id = %s", (world.conv_b,))
        assert cur.rowcount == 0
        cur = c.execute("update conversations set status = 'resolved' where id = %s", (world.conv_a,))
        assert cur.rowcount == 1  # control: the same statement works on its own tenant's row
    assert admin.execute("select status from conversations where id = %s", (world.conv_b,)).fetchone()[0] == "escalated"


def test_a_pooled_connection_does_not_carry_the_previous_tenant(world, monkeypatch):
    monkeypatch.setenv("CONSOLE_POOL_MAX", "1")  # one connection: the next request MUST reuse it
    db.close_pool()
    with db.tx(tenant_id=str(world.ta), user_id=str(world.agent_a), role="agent") as c:
        assert c.execute("select count(*) as n from conversations").fetchone()["n"] == 1
    with db.tx() as c:
        assert c.execute("select count(*) as n from conversations").fetchone()["n"] == 0
        assert c.execute("select current_setting('app.tenant_id', true) as t").fetchone()["t"] in ("", None)


def test_agents_cannot_read_the_audit_log_but_can_write_to_it(world):
    with db.tx(tenant_id=str(world.ta), user_id=str(world.agent_a), role="agent") as c:
        assert c.execute("select count(*) as n from audit_log").fetchone()["n"] == 0
        c.execute("insert into audit_log (tenant_id, actor, action) values (%s, 'agent-a@x.com', 'handoff.claim')", (world.ta,))
    with db.tx(tenant_id=str(world.ta), user_id=str(world.admin_a), role="admin") as c:
        actions = {r["action"] for r in c.execute("select action from audit_log").fetchall()}
        assert actions == {"chat.escalated", "handoff.claim"}  # control: the admin reads A's log, and only A's


def test_the_audit_log_is_append_only(world):
    with db.tx(tenant_id=str(world.ta), user_id=str(world.admin_a), role="admin") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("update audit_log set action = 'edited'")
    with db.tx(tenant_id=str(world.ta), user_id=str(world.admin_a), role="admin") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("delete from audit_log")


def test_nothing_in_the_conversation_tables_can_be_deleted(world):
    with db.tx(tenant_id=str(world.ta), user_id=str(world.admin_a), role="admin") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("delete from conversations")


def test_a_user_sees_only_their_own_stores_without_any_tenant_bound(world):
    with db.tx(user_id=str(world.agent_a)) as c:
        assert {r["slug"] for r in c.execute("select slug from tenants").fetchall()} == {"store-a"}
    with db.tx(user_id=str(world.agent_b)) as c:
        assert {r["slug"] for r in c.execute("select slug from tenants").fetchall()} == {"store-b"}


def test_memberships_show_only_the_signed_in_users_own_rows(world):
    # who else belongs to a store, and with which role, is not something a user can read
    with db.tx(user_id=str(world.agent_a)) as c:
        assert {str(r["user_id"]) for r in c.execute("select user_id from memberships").fetchall()} == {str(world.agent_a)}
    with db.tx(tenant_id=str(world.ta), user_id=str(world.admin_a), role="admin") as c:
        assert {str(r["user_id"]) for r in c.execute("select user_id from memberships").fetchall()} == {str(world.admin_a)}


def test_an_audit_entry_cannot_be_written_under_another_tenant(world, admin):
    with db.tx(tenant_id=str(world.ta), user_id=str(world.agent_a), role="agent") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("insert into audit_log (tenant_id, actor, action) values (%s, 'forged', 'handoff.resolve')", (world.tb,))
    assert admin.execute("select count(*) from audit_log where actor = 'forged'").fetchone()[0] == 0


def test_only_an_admin_of_that_store_can_edit_its_policy(world):
    with db.tx(tenant_id=str(world.ta), user_id=str(world.agent_a), role="agent") as c:
        assert c.execute("update tenants set policy_md = 'agent edit' where id = %s", (world.ta,)).rowcount == 0
    with db.tx(tenant_id=str(world.ta), user_id=str(world.admin_a), role="admin") as c:
        assert c.execute("update tenants set policy_md = 'admin edit' where id = %s", (world.ta,)).rowcount == 1
        assert c.execute("update tenants set policy_md = 'sneaky' where id = %s", (world.tb,)).rowcount == 0


def test_the_store_token_is_not_writable_or_deletable_from_the_app_role(world):
    with db.tx(tenant_id=str(world.ta), user_id=str(world.admin_a), role="admin") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("update tenants set shopify_token_enc = 'stolen' where id = %s", (world.ta,))


def test_login_lookup_finds_one_exact_email_and_the_table_cannot_be_scanned(world):
    with db.tx() as c:
        row = c.execute("select * from login_lookup('ADMIN-A@x.com')").fetchone()
        assert row and row["name"] == "Admin A"
        assert c.execute("select count(*) as n from login_lookup('nobody@x.com')").fetchone()["n"] == 0
        assert c.execute("select count(*) as n from users").fetchone()["n"] == 0  # unbound: the table reads empty
    with db.tx(user_id=str(world.agent_a)) as c:
        assert {r["email"] for r in c.execute("select email from users").fetchall()} == {"agent-a@x.com"}
