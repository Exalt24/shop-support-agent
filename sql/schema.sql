-- Support console schema. Run as a superuser by console_api/migrate.py (never by the app).
--
-- Isolation model, and the three ways it quietly fails if you copy it carelessly:
--   * the API connects as console_app: NOT a superuser, NOT BYPASSRLS, NOT the table owner.
--     A superuser bypasses RLS unconditionally and FORCE does not contain it; an owner bypasses
--     unless FORCE is set. Either one makes every policy below decoration.
--   * the tenant is bound per TRANSACTION with set_config(..., true). A session-level setting
--     survives a pooled connection and hands the previous request's tenant to the next one.
--   * an unbound setting reads as the empty string, not NULL, so every policy goes through
--     nullif(...) and an unbound request matches nothing instead of erroring.

do $$ begin
  if not exists (select from pg_roles where rolname = 'console_owner') then
    create role console_owner nologin;
  end if;
  if not exists (select from pg_roles where rolname = 'console_app') then
    create role console_app login nosuperuser nobypassrls nocreatedb nocreaterole;
  end if;
end $$;

grant usage, create on schema public to console_owner;
create extension if not exists pgcrypto;

set role console_owner;

create function app_tenant() returns uuid language sql stable as
  $$ select nullif(current_setting('app.tenant_id', true), '')::uuid $$;
create function app_user() returns uuid language sql stable as
  $$ select nullif(current_setting('app.user_id', true), '')::uuid $$;
create function app_role() returns text language sql stable as
  $$ select nullif(current_setting('app.role', true), '') $$;

create table tenants (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique check (slug ~ '^[a-z0-9][a-z0-9-]{1,40}$'),
  name text not null,
  shop_domain text not null,
  shopify_token_enc text not null,          -- Fernet ciphertext, the key lives in the environment, never here
  policy_md text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table users (
  id uuid primary key default gen_random_uuid(),
  email text not null unique check (email = lower(email)),
  name text not null,
  password_hash text not null,
  created_at timestamptz not null default now()
);

create table memberships (
  user_id uuid not null references users(id) on delete cascade,
  tenant_id uuid not null references tenants(id) on delete cascade,
  role text not null check (role in ('admin', 'agent')),
  primary key (user_id, tenant_id)
);

create table conversations (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  customer_email text not null,
  channel text not null default 'web' check (channel in ('web', 'email', 'whatsapp')),
  status text not null default 'open' check (status in ('open', 'answered', 'escalated', 'resolved')),
  intent text,
  order_name text,
  assigned_to uuid references users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table messages (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  conversation_id uuid not null references conversations(id) on delete cascade,
  sender text not null check (sender in ('customer', 'agent', 'human')),
  body text not null check (length(body) between 1 and 8000),
  meta jsonb not null default '{}'::jsonb,   -- intent, steps, guard result: what the agent looked up and why
  author_id uuid references users(id),       -- set only for sender = 'human'
  created_at timestamptz not null default now()
);

create table handoffs (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  conversation_id uuid not null unique references conversations(id) on delete cascade,
  reason text not null,
  status text not null default 'open' check (status in ('open', 'claimed', 'resolved')),
  claimed_by uuid references users(id),
  resolution_note text,
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);

create table audit_log (
  id bigserial primary key,
  tenant_id uuid not null references tenants(id) on delete cascade,
  actor text not null,                        -- a user email, or 'agent' / 'customer' for automated turns
  action text not null,
  detail jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- What the agent remembers about a customer, per store. A hint, never a fact (see agent/memory.py):
--   * a live_ref row is a POINTER ("shopify:order:#1019"), so it carries no body: the value is read from the store when needed;
--   * a memory is never edited, a newer observation supersedes it, so the app role may only set superseded_by;
--   * one current row per (store, customer, kind, key), enforced by a partial unique index and not by application code.
create table customer_memories (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  customer_email text not null check (customer_email = lower(customer_email)),
  kind text not null check (kind in ('preference', 'fact', 'episode', 'live_ref')),
  key text not null check (key ~ '^[a-z][a-z0-9_]{1,40}$'),
  body text check (body is null or length(body) between 3 and 160),
  ref text,
  source text not null check (source in ('customer_said', 'agent_note', 'staff_note')),
  conversation_id uuid references conversations(id) on delete set null,
  observed_at timestamptz not null default now(),
  superseded_by uuid,
  created_at timestamptz not null default now(),
  constraint live_ref_is_a_pointer check (
    (kind = 'live_ref' and body is null and ref is not null) or (kind <> 'live_ref' and body is not null and ref is null))
);
create unique index customer_memories_one_current on customer_memories (tenant_id, customer_email, kind, key)
  where superseded_by is null;
create index customer_memories_lookup on customer_memories (tenant_id, customer_email, observed_at desc);

-- every column a policy or a hot query touches is indexed
create index conversations_inbox on conversations (tenant_id, status, updated_at desc);
create index conversations_email on conversations (tenant_id, customer_email);
create index messages_thread on messages (conversation_id, created_at);
create index messages_tenant on messages (tenant_id);
create index handoffs_queue on handoffs (tenant_id, status, created_at);
create index audit_recent on audit_log (tenant_id, created_at desc);
create index memberships_user on memberships (user_id);

-- Login must find a user by exact email without being able to scan the table, so it goes through
-- a definer function instead of a SELECT grant on users.
create function login_lookup(p_email text) returns table (id uuid, name text, password_hash text)
  language sql security definer set search_path = public as
  $$ select u.id, u.name, u.password_hash from users u where u.email = lower(p_email) $$;
revoke all on function login_lookup(text) from public;

alter table tenants enable row level security;      alter table tenants force row level security;
alter table users enable row level security;        alter table users force row level security;
alter table memberships enable row level security;  alter table memberships force row level security;
alter table conversations enable row level security; alter table conversations force row level security;
alter table messages enable row level security;     alter table messages force row level security;
alter table handoffs enable row level security;     alter table handoffs force row level security;
alter table audit_log enable row level security;    alter table audit_log force row level security;
alter table customer_memories enable row level security; alter table customer_memories force row level security;

create policy memberships_own on memberships for select using (user_id = app_user());
create policy users_self on users for select using (id = app_user());
-- FORCE applies to the owner too, so login_lookup (SECURITY DEFINER, runs as console_owner) needs its own way
-- in. console_app cannot SET ROLE to the owner, so this is reachable only through that one function.
create policy users_login_lookup on users for select using (current_user = 'console_owner');

create policy tenants_visible on tenants for select using (
  id = app_tenant() or id in (select tenant_id from memberships where user_id = app_user()));
create policy tenants_admin_edit on tenants for update
  using (id = app_tenant() and app_role() = 'admin')
  with check (id = app_tenant() and app_role() = 'admin');

create policy conversations_tenant on conversations for all
  using (tenant_id = app_tenant()) with check (tenant_id = app_tenant());
create policy messages_tenant on messages for all
  using (tenant_id = app_tenant()) with check (tenant_id = app_tenant());
create policy handoffs_tenant on handoffs for all
  using (tenant_id = app_tenant()) with check (tenant_id = app_tenant());
create policy customer_memories_tenant on customer_memories for all
  using (tenant_id = app_tenant()) with check (tenant_id = app_tenant());

-- the audit log is append-only and admin-readable: agents can cause entries, never read or edit them
create policy audit_admin_read on audit_log for select using (tenant_id = app_tenant() and app_role() = 'admin');
create policy audit_append on audit_log for insert with check (tenant_id = app_tenant());

reset role;

grant usage on schema public to console_app;
grant select on memberships, users to console_app;
grant select on tenants to console_app;
grant update (policy_md, updated_at) on tenants to console_app;
grant select, insert, update on conversations, messages, handoffs to console_app;
grant select, insert on audit_log to console_app;
grant select, insert, delete on customer_memories to console_app;
grant update (superseded_by) on customer_memories to console_app;
grant usage on sequence audit_log_id_seq to console_app;
grant execute on function login_lookup(text), app_tenant(), app_user(), app_role() to console_app;
