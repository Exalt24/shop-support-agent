"""Support console API. Run: uvicorn console_api.main:app --port 8010

Authorisation is layered on purpose: the route checks membership and role, and the database re-checks tenant and
role through RLS, so a bug in one layer is not a leak. Every statement that touches tenant data runs inside
db.tx(), which binds the tenant for that transaction only.
"""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from agent.shopify import ShopifyError

from . import auth, db
from .auth import Member, admin_only, member
from .engine import build_agent, store_shop
from .memory_store import PgMemory, forget_customer, forget_one, list_for_staff

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
ORDER_RE = re.compile(r"^#?\d{1,10}$")
HELD_REPLY = "A member of our team is already looking at this and will reply here. You don't need to send it again."


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.pool()  # opens the pool and refuses to serve if the role would bypass RLS
    yield
    db.close_pool()


app = FastAPI(title="support-console", lifespan=lifespan, dependencies=[Depends(auth.csrf_guard)])
app.state.agent_factory = build_agent
app.state.shop_factory = store_shop


# ---- models ---------------------------------------------------------------------------------------------------

class LoginBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=200)


class ChatBody(BaseModel):
    email: str = Field(max_length=320)
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class ResolveBody(BaseModel):
    reply: str = Field(min_length=1, max_length=4000)
    note: str | None = Field(default=None, max_length=1000)


class PolicyBody(BaseModel):
    policy_md: str = Field(max_length=20000)


# ---- helpers --------------------------------------------------------------------------------------------------

def scoped(m: Member):
    return db.tx(tenant_id=m.tenant_id, user_id=m.user_id, role=m.role)


def audit(conn, m_or_tenant, actor: str, action: str, detail: dict | None = None) -> None:
    tid = m_or_tenant.tenant_id if isinstance(m_or_tenant, Member) else m_or_tenant
    conn.execute("insert into audit_log (tenant_id, actor, action, detail) values (%s, %s, %s, %s)",
                 (tid, actor, action, Jsonb(detail or {})))


def user_email(m: Member) -> str:
    with db.tx(user_id=m.user_id) as c:
        return c.execute("select email from users where id = %s", (m.user_id,)).fetchone()["email"]


def tenant_row(m: Member) -> dict:
    with scoped(m) as c:
        return c.execute("select slug, name, shop_domain, shopify_token_enc, policy_md from tenants where id = %s",
                         (m.tenant_id,)).fetchone()


def page_args(page: int, per: int) -> tuple[int, int]:
    per = max(1, min(per, 50))
    return per, (max(page, 1) - 1) * per


# ---- auth -----------------------------------------------------------------------------------------------------

def _me(uid: str) -> dict:
    with db.tx(user_id=uid) as c:
        u = c.execute("select id, email, name from users where id = %s", (uid,)).fetchone()
        stores = c.execute(
            "select t.slug, t.name, m.role from tenants t join memberships m on m.tenant_id = t.id "
            "where m.user_id = %s order by t.name", (uid,)).fetchall()
    if not u:
        raise HTTPException(401, "Please sign in.")
    return {"user": u, "stores": stores}


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    client = request.client.host if request.client else "?"
    auth.check_not_locked(body.email, client)
    user = auth.authenticate(body.email, body.password)
    if not user:
        auth.record_failure(body.email, client)
        raise HTTPException(401, "Wrong email or password.")
    auth.clear_failures(body.email, client)
    response.set_cookie(auth.COOKIE, auth.make_session(user["id"]), max_age=auth.SESSION_SECONDS, httponly=True,
                        samesite="lax", secure=bool(os.environ.get("CONSOLE_COOKIE_SECURE")), path="/")
    return _me(user["id"])


@app.post("/api/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(uid: str = Depends(auth.current_user_id)) -> dict:
    return _me(uid)


# ---- dashboard ------------------------------------------------------------------------------------------------

@app.get("/api/t/{slug}/dashboard")
def dashboard(m: Member = Depends(member)) -> dict:
    with scoped(m) as c:
        week = c.execute(
            "select count(*) as total, count(*) filter (where status = 'answered') as answered, "
            "count(*) filter (where status = 'escalated') as escalated, "
            "count(*) filter (where status = 'resolved') as resolved "
            "from conversations where created_at >= now() - interval '7 days'").fetchone()
        handoffs = c.execute(
            "select count(*) filter (where status = 'open') as open, count(*) filter (where status = 'claimed') as claimed "
            "from handoffs where status <> 'resolved'").fetchone()
        days = c.execute(
            "select d::date as day, count(c.id) as n from generate_series(current_date - 6, current_date, interval '1 day') d "
            "left join conversations c on c.created_at::date = d::date group by d order by d").fetchall()
        recent = c.execute(
            "select id, customer_email, status, intent, order_name, updated_at from conversations "
            "order by updated_at desc limit 6").fetchall()
    total = week["total"] or 0
    return {"store": m.store_name, "role": m.role, "week": week, "handoffs": handoffs, "days": days, "recent": recent,
            "agent_handled_pct": round(100 * (week["answered"] / total)) if total else None}


# ---- inbox ----------------------------------------------------------------------------------------------------

@app.get("/api/t/{slug}/conversations")
def conversations(m: Member = Depends(member), status: str | None = None, intent: str | None = None,
                  q: str | None = Query(default=None, max_length=100), page: int = 1, per: int = 20) -> dict:
    if status and status not in ("open", "answered", "escalated", "resolved"):
        raise HTTPException(422, "Unknown status.")
    per, offset = page_args(page, per)
    where, args = ["true"], []
    if status:
        where.append("c.status = %s"); args.append(status)
    if intent:
        where.append("c.intent = %s"); args.append(intent)
    if q:
        like = "%" + q.replace("%", "").replace("_", "") + "%"
        where.append("(c.customer_email ilike %s or c.order_name ilike %s or exists "
                     "(select 1 from messages x where x.conversation_id = c.id and x.body ilike %s))")
        args += [like, like, like]
    clause = " and ".join(where)
    with scoped(m) as c:
        total = c.execute("select count(*) as n from conversations c where " + clause, args).fetchone()["n"]
        rows = c.execute(
            "select c.id, c.customer_email, c.status, c.intent, c.order_name, c.updated_at, c.channel, u.name as assignee, "
            "(select left(body, 140) from messages x where x.conversation_id = c.id and x.sender = 'customer' "
            " order by x.created_at desc limit 1) as preview "
            "from conversations c left join users u on u.id = c.assigned_to where " + clause +
            " order by c.updated_at desc limit %s offset %s", args + [per, offset]).fetchall()
    return {"items": rows, "total": total, "page": page, "per": per}


@app.get("/api/t/{slug}/conversations/{conv_id}")
def conversation(conv_id: str, m: Member = Depends(member)) -> dict:
    try:
        with scoped(m) as c:
            conv = c.execute(
                "select c.*, u.name as assignee from conversations c left join users u on u.id = c.assigned_to "
                "where c.id = %s", (conv_id,)).fetchone()
            if not conv:
                raise HTTPException(404, "Conversation not found.")
            msgs = c.execute(
                "select m.id, m.sender, m.body, m.meta, m.created_at, u.name as author from messages m "
                "left join users u on u.id = m.author_id where m.conversation_id = %s order by m.created_at, m.id",
                (conv_id,)).fetchall()
            hand = c.execute(
                "select h.id, h.reason, h.status, h.resolution_note, h.created_at, h.resolved_at, u.name as claimed_by_name, "
                "h.claimed_by from handoffs h left join users u on u.id = h.claimed_by where h.conversation_id = %s",
                (conv_id,)).fetchone()
    except HTTPException:
        raise
    except Exception as e:  # a malformed uuid is a 404, not a 500
        if "invalid input syntax for type uuid" in str(e):
            raise HTTPException(404, "Conversation not found.")
        raise
    return {"conversation": conv, "messages": msgs, "handoff": hand}


# ---- live chat (customer side, run by staff as a simulator) ---------------------------------------------------

@app.post("/api/t/{slug}/chat")
def chat(body: ChatBody, request: Request, m: Member = Depends(member)) -> dict:
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address.")
    message = body.message.strip()
    if not message:
        raise HTTPException(422, "Write a message first.")

    # phase 1: find or open the conversation, store the customer's message
    with scoped(m) as c:
        conv = None
        if body.conversation_id:
            try:
                conv = c.execute("select id, status from conversations where id = %s and customer_email = %s",
                                 (body.conversation_id, email)).fetchone()
            except Exception:
                conv = None
        if not conv:
            conv = c.execute("insert into conversations (tenant_id, customer_email) values (%s, %s) returning id, status",
                             (m.tenant_id, email)).fetchone()
        conv_id, status = str(conv["id"]), conv["status"]
        c.execute("insert into messages (tenant_id, conversation_id, sender, body) values (%s, %s, 'customer', %s)",
                  (m.tenant_id, conv_id, message))
        tenant = c.execute("select slug, name, shop_domain, shopify_token_enc, policy_md from tenants where id = %s",
                           (m.tenant_id,)).fetchone()

    # a case already with a person is not answered over their head
    if status == "escalated":
        with scoped(m) as c:
            c.execute("insert into messages (tenant_id, conversation_id, sender, body, meta) values (%s, %s, 'agent', %s, %s)",
                      (m.tenant_id, conv_id, HELD_REPLY, Jsonb({"action": "held"})))
            c.execute("update conversations set updated_at = now() where id = %s", (conv_id,))
        return {"conversation_id": conv_id, "reply": HELD_REPLY, "action": "held", "escalation_reason": None,
                "intent": None, "steps": ["conversation already with a person: not answered by the agent"]}

    # phase 2: the engine (model and store calls run outside any database transaction)
    agent = request.app.state.agent_factory(tenant)
    if hasattr(agent, "attach_memory"):  # the memory carries this tenant and this staff member for this one turn
        agent.attach_memory(PgMemory(m, conv_id))
    result = agent.handle(message, email)

    # phase 3: record what happened
    meta ={"intent": result.intent, "action": result.action, "order": result.order_name, "steps": result.steps,
            "guard_violations": list(result.guard_violations), "escalation_reason": result.escalation_reason}
    new_status = "escalated" if result.action == "escalated" else "answered"
    with scoped(m) as c:
        c.execute("insert into messages (tenant_id, conversation_id, sender, body, meta) values (%s, %s, 'agent', %s, %s)",
                  (m.tenant_id, conv_id, result.reply, Jsonb(meta)))
        c.execute("update conversations set status = %s, intent = %s, order_name = coalesce(%s, order_name), updated_at = now() "
                  "where id = %s", (new_status, result.intent, result.order_name, conv_id))
        if result.action == "escalated":
            c.execute(
                "insert into handoffs (tenant_id, conversation_id, reason) values (%s, %s, %s) "
                "on conflict (conversation_id) do update set status = 'open', reason = excluded.reason, claimed_by = null, "
                "resolution_note = null, resolved_at = null, created_at = now()",
                (m.tenant_id, conv_id, result.escalation_reason or "unspecified"))
        audit(c, m, "agent", "chat.escalated" if result.action == "escalated" else "chat.reply",
              {"conversation": conv_id, "intent": result.intent, "order": result.order_name,
               "reason": result.escalation_reason, "guard_violations": list(result.guard_violations)})
    return {"conversation_id": conv_id, "reply": result.reply, "action": result.action,
            "escalation_reason": result.escalation_reason, "intent": result.intent, "steps": result.steps}


# ---- handoff queue --------------------------------------------------------------------------------------------

@app.get("/api/t/{slug}/handoffs")
def handoffs(m: Member = Depends(member), status: str = "active") -> dict:
    if status not in ("active", "resolved", "all"):
        raise HTTPException(422, "Unknown filter.")
    cond = {"active": "h.status <> 'resolved'", "resolved": "h.status = 'resolved'", "all": "true"}[status]
    with scoped(m) as c:
        rows = c.execute(
            "select h.id, h.conversation_id, h.reason, h.status, h.created_at, h.resolved_at, c.customer_email, c.intent, "
            "c.order_name, u.name as claimed_by_name, h.claimed_by, "
            "(select left(body, 160) from messages x where x.conversation_id = c.id and x.sender = 'customer' "
            " order by x.created_at desc limit 1) as preview "
            "from handoffs h join conversations c on c.id = h.conversation_id left join users u on u.id = h.claimed_by "
            "where " + cond + " order by h.created_at desc limit 100").fetchall()
    return {"items": rows}


@app.post("/api/t/{slug}/handoffs/{handoff_id}/claim")
def claim(handoff_id: str, m: Member = Depends(member)) -> dict:
    email = user_email(m)
    with scoped(m) as c:
        row = c.execute(
            "update handoffs set status = 'claimed', claimed_by = %s where id = %s and status <> 'resolved' "
            "and (claimed_by is null or claimed_by = %s) returning conversation_id", (m.user_id, handoff_id, m.user_id)).fetchone()
        if not row:
            raise HTTPException(409, "Someone else already has this case, or it is resolved.")
        c.execute("update conversations set assigned_to = %s, updated_at = now() where id = %s", (m.user_id, row["conversation_id"]))
        audit(c, m, email, "handoff.claim", {"handoff": handoff_id})
    return {"ok": True}


@app.post("/api/t/{slug}/handoffs/{handoff_id}/resolve")
def resolve(handoff_id: str, body: ResolveBody, m: Member = Depends(member)) -> dict:
    email = user_email(m)
    with scoped(m) as c:
        h = c.execute("select id, conversation_id, status, claimed_by from handoffs where id = %s", (handoff_id,)).fetchone()
        if not h:
            raise HTTPException(404, "Handoff not found.")
        if h["status"] == "resolved":
            raise HTTPException(409, "Already resolved.")
        if h["claimed_by"] and str(h["claimed_by"]) != m.user_id and m.role != "admin":
            raise HTTPException(409, "This case is claimed by someone else.")
        c.execute("insert into messages (tenant_id, conversation_id, sender, body, author_id) values (%s, %s, 'human', %s, %s)",
                  (m.tenant_id, h["conversation_id"], body.reply.strip(), m.user_id))
        c.execute("update handoffs set status = 'resolved', claimed_by = coalesce(claimed_by, %s), resolution_note = %s, "
                  "resolved_at = now() where id = %s", (m.user_id, body.note, handoff_id))
        c.execute("update conversations set status = 'resolved', updated_at = now() where id = %s", (h["conversation_id"],))
        audit(c, m, email, "handoff.resolve", {"handoff": handoff_id, "conversation": str(h["conversation_id"])})
    return {"ok": True}


# ---- order and product lookup (staff) -------------------------------------------------------------------------

@app.get("/api/t/{slug}/orders/{order_name}")
def order_lookup(order_name: str, request: Request, m: Member = Depends(member)) -> dict:
    if not ORDER_RE.match(order_name):
        raise HTTPException(422, "Order numbers look like #1001.")
    tenant = tenant_row(m)
    email = user_email(m)
    try:
        view = request.app.state.shop_factory(tenant).find_order_for_staff(order_name)
    except ShopifyError:
        raise HTTPException(502, "The store did not answer. Try again in a moment.")
    with scoped(m) as c:
        audit(c, m, email, "lookup.order", {"order": order_name if order_name.startswith("#") else "#" + order_name,
                                            "found": bool(view)})
    if not view:
        return {"found": False}
    f = view.facts
    return {"found": True, "name": f.name, "email": view.email, "placed": f.created_at, "payment": f.financial_status,
            "fulfillment": f.fulfillment_status, "total": f.total, "currency": f.currency, "items": list(f.items),
            "tracking": list(f.tracking)}


@app.get("/api/t/{slug}/products")
def product_lookup(request: Request, q: str = Query(min_length=2, max_length=80), m: Member = Depends(member)) -> dict:
    try:
        found = request.app.state.shop_factory(tenant_row(m)).search_products(q, 6)
    except ShopifyError:
        raise HTTPException(502, "The store did not answer. Try again in a moment.")
    return {"items": [{"title": p.title, "price": p.price, "currency": p.currency, "in_stock": p.in_stock,
                       "description": p.description[:200]} for p in found]}


# ---- customer memory (staff view and erasure) -----------------------------------------------------------------

def _memory_view(n) -> dict:
    use = ("pointer only: the value is read live from the store, no copy is kept" if n.kind == "live_ref"
           else "stale: confirm with the customer before relying on it" if n.stale else "hint: may be out of date")
    return {"id": n.id, "kind": n.kind, "key": n.key, "body": n.body, "ref": n.ref, "source": n.source,
            "observed_at": n.observed_at, "age_days": n.age_days, "stale": n.stale, "use": use}


def _valid_customer(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address.")
    return email


@app.get("/api/t/{slug}/customers/{email}/memory")
def customer_memory(email: str, m: Member = Depends(member)) -> dict:
    email = _valid_customer(email)
    notes = list_for_staff(m, email, actor=user_email(m))
    return {"customer": email, "items": [_memory_view(n) for n in notes]}


@app.delete("/api/t/{slug}/customers/{email}/memory")
def forget_customer_memory(email: str, m: Member = Depends(member)) -> dict:
    email = _valid_customer(email)
    actor = user_email(m)
    n = forget_customer(m, email)
    with scoped(m) as c:
        audit(c, m, actor, "memory.forget", {"scope": "customer", "notes": n})
    return {"forgotten": n}


@app.delete("/api/t/{slug}/memory/{memory_id}")
def forget_memory(memory_id: str, m: Member = Depends(member)) -> dict:
    actor = user_email(m)
    try:
        n = forget_one(m, memory_id)
    except Exception as e:  # a malformed uuid is a 404, never a 500
        if "invalid input syntax for type uuid" in str(e):
            raise HTTPException(404, "Memory not found.")
        raise
    if n != 1:
        raise HTTPException(404, "Memory not found.")  # the same answer for 'no such note' and 'another store's note'
    with scoped(m) as c:
        audit(c, m, actor, "memory.forget", {"scope": "one", "notes": 1})
    return {"ok": True}


# ---- admin: audit and settings --------------------------------------------------------------------------------

@app.get("/api/t/{slug}/audit")
def audit_log(m: Member = Depends(admin_only), page: int = 1, per: int = 30, action: str | None = None) -> dict:
    per, offset = page_args(page, per)
    cond, args = ("true", []) if not action else ("action = %s", [action])
    with scoped(m) as c:
        total = c.execute("select count(*) as n from audit_log where " + cond, args).fetchone()["n"]
        rows = c.execute("select id, actor, action, detail, created_at from audit_log where " + cond +
                         " order by created_at desc, id desc limit %s offset %s", args + [per, offset]).fetchall()
    return {"items": rows, "total": total, "page": page, "per": per}


@app.get("/api/t/{slug}/settings")
def settings(m: Member = Depends(admin_only)) -> dict:
    t = tenant_row(m)
    return {"name": t["name"], "shop_domain": t["shop_domain"], "token_connected": bool(t["shopify_token_enc"]),
            "policy_md": t["policy_md"]}


@app.put("/api/t/{slug}/settings/policy")
def update_policy(body: PolicyBody, m: Member = Depends(admin_only)) -> dict:
    email = user_email(m)
    with scoped(m) as c:
        n = c.execute("update tenants set policy_md = %s, updated_at = now() where id = %s", (body.policy_md, m.tenant_id)).rowcount
        if n != 1:
            raise HTTPException(409, "Not saved.")
        audit(c, m, email, "policy.update", {"chars": len(body.policy_md)})
    return {"ok": True}
