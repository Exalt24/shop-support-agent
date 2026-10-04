"""Customer memory, attacked through the real app role. Each denial is paired with a control showing the same statement
works for the right tenant, so a table that refused everything would not pass."""
import psycopg
import pytest

from console_api import db

SHARED = "shared@x.com"


def put(admin, tenant, kind="preference", key="contact_channel", body="email only", ref=None, email=SHARED, superseded_by=None):
    return admin.execute(
        "insert into customer_memories (tenant_id, customer_email, kind, key, body, ref, source, superseded_by) "
        "values (%s, %s, %s, %s, %s, %s, 'customer_said', %s) returning id",
        (tenant, email, kind, key, body, ref, superseded_by)).fetchone()[0]


@pytest.fixture
def seeded(world, admin):
    world.mem_a = put(admin, world.ta, body="email only")
    world.mem_b = put(admin, world.tb, body="sms only")  # the SAME customer email at the other store
    return world


def agent(world, tenant, user):
    return db.tx(tenant_id=str(tenant), user_id=str(user), role="agent")


def test_an_unbound_request_reads_no_memory(seeded):
    with db.tx() as c:
        assert c.execute("select count(*) as n from customer_memories").fetchone()["n"] == 0


def test_a_store_sees_only_its_own_notes_even_for_the_same_customer_email(seeded):
    with agent(seeded, seeded.ta, seeded.agent_a) as c:
        assert [r["body"] for r in c.execute("select body from customer_memories where customer_email = %s", (SHARED,))] == ["email only"]
    with agent(seeded, seeded.tb, seeded.agent_b) as c:
        assert [r["body"] for r in c.execute("select body from customer_memories where customer_email = %s", (SHARED,))] == ["sms only"]


def test_a_note_cannot_be_written_for_another_store(seeded):
    with agent(seeded, seeded.ta, seeded.agent_a) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("insert into customer_memories (tenant_id, customer_email, kind, key, body, source) "
                      "values (%s, 'x@x.com', 'fact', 'forged', 'planted in B', 'customer_said')", (seeded.tb,))


def test_another_stores_note_cannot_be_deleted_or_superseded_and_rowcount_says_so(seeded, admin):
    with agent(seeded, seeded.ta, seeded.agent_a) as c:
        assert c.execute("delete from customer_memories where id = %s", (seeded.mem_b,)).rowcount == 0
        assert c.execute("update customer_memories set superseded_by = gen_random_uuid() where id = %s", (seeded.mem_b,)).rowcount == 0
        assert c.execute("update customer_memories set superseded_by = gen_random_uuid() where id = %s", (seeded.mem_a,)).rowcount == 1  # control
    assert admin.execute("select count(*) from customer_memories where id = %s and superseded_by is null", (seeded.mem_b,)).fetchone()[0] == 1


def test_a_store_can_erase_its_own_note_and_only_its_own(seeded, admin):
    with agent(seeded, seeded.ta, seeded.agent_a) as c:
        assert c.execute("delete from customer_memories where customer_email = %s", (SHARED,)).rowcount == 1
    assert admin.execute("select body from customer_memories where tenant_id = %s", (seeded.tb,)).fetchone()[0] == "sms only"


def test_a_remembered_sentence_cannot_be_edited_only_superseded(seeded):
    with agent(seeded, seeded.ta, seeded.agent_a) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("update customer_memories set body = 'rewritten' where id = %s", (seeded.mem_a,))
    with agent(seeded, seeded.ta, seeded.agent_a) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("update customer_memories set tenant_id = %s where id = %s", (seeded.tb, seeded.mem_a))


def test_a_live_pointer_cannot_carry_a_copy_and_a_note_cannot_lack_its_words(world, admin):
    with pytest.raises(psycopg.errors.CheckViolation):
        put(admin, world.ta, kind="live_ref", key="last_order", body="order total 250", ref="shopify:order:#1019")
    with pytest.raises(psycopg.errors.CheckViolation):
        put(admin, world.ta, kind="live_ref", key="last_order", body=None, ref=None)
    with pytest.raises(psycopg.errors.CheckViolation):
        put(admin, world.ta, kind="preference", key="contact_channel", body=None)
    put(admin, world.ta, kind="live_ref", key="last_order", body=None, ref="shopify:order:#1019")  # control: a real pointer is fine


def test_the_email_must_be_stored_lowercase_and_the_key_must_be_snake_case(world, admin):
    with pytest.raises(psycopg.errors.CheckViolation):
        put(admin, world.ta, email="Shouty@X.com")
    with pytest.raises(psycopg.errors.CheckViolation):
        put(admin, world.ta, key="Bad Key")


def test_only_one_current_note_per_slot_and_history_is_unlimited(world, admin):
    first = put(admin, world.ta)
    with pytest.raises(psycopg.errors.UniqueViolation):
        put(admin, world.ta, body="a second current one")
    put(admin, world.ta, body="old history one", superseded_by=first)  # control: a superseded row does not collide
    put(admin, world.ta, key="other_slot", body="a different slot is fine")


def test_a_pooled_connection_does_not_carry_the_previous_stores_notes(seeded, monkeypatch):
    monkeypatch.setenv("CONSOLE_POOL_MAX", "1")
    db.close_pool()
    with agent(seeded, seeded.ta, seeded.agent_a) as c:
        assert c.execute("select count(*) as n from customer_memories").fetchone()["n"] == 1
    with db.tx() as c:
        assert c.execute("select count(*) as n from customer_memories").fetchone()["n"] == 0
