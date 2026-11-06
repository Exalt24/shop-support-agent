# The support console

*Roles, isolation, login, the demo logins and how to reuse the console for another store. The README keeps the summary and the commands.*

- **Roles and stores.** Sign in with email and password. An **admin** sees everything for a store, plus the audit log and the policy editor. An **agent** sees the inbox, handoffs, live chat and lookup. One person can belong to several stores and switch between them. A store you do not belong to answers 404, never 403, so its existence is not confirmed.
- **Pages.** Dashboard, a three-pane inbox (list, thread, order and agent steps), a handoff queue with one owner per case (claim, reply, resolve), a live chat that talks to the real agent as a customer, order and product lookup, an admin audit log, and a policy editor. Every status is an icon plus a word. It works at phone width.
- **Isolation lives in the database.** One Postgres database, a `tenant_id` on every tenant table, row level security enabled and forced. The API connects as `console_app`, which is not a superuser, does not bypass row security and does not own the tables; startup refuses to serve otherwise. The tenant is bound per transaction. The audit log is append-only and admin-readable. The Shopify token is stored encrypted and is never sent to the browser.
- **Login.** bcrypt, a signed session cookie (httponly, SameSite=Lax), a lockout after five failures per ten minutes per email and address, and a CSRF guard on writes.

Demo logins (local only, written in `console_api/seed.py`): `admin@demo.test` / `demo-admin-pass` (both stores), `agent@demo.test` / `demo-agent-pass` (one store).

**Before any real use:** `python -m console_api.seed --purge` removes every demo row, then set real passwords and connect a real store. The seed passwords are public (here and in `console_api/seed.py`) on purpose and must never reach a live database.

**Reusing it for another store:** insert a `tenants` row (slug, name, shop domain, encrypted token via `console_api/secrets_store.py`, policy markdown) and a `memberships` row per person. The engine reads that store's orders and policy; nothing else changes. A different shop platform means swapping `agent/shopify.py`, not the console.
