# Support console: plan

Goal: turn the proven agent engine into something a founder can open, click through and trust, and that can be pointed at the next client's store without a rewrite. The engine (guard, escalation in code, identity-checked lookups, 49 tests, 11 mutants) is NOT rebuilt. It becomes the backend's brain.

## Stack (checked 2026-10-03, not assumed)
- **Frontend:** Next.js 16 (current LTS line, App Router, `proxy.ts` replaces middleware, async `cookies()` and `params`), TypeScript, Tailwind CSS 4.
- **Backend:** FastAPI (already the engine's HTTP layer) + PostgreSQL 17 via psycopg 3.
- **Tenancy:** one database, `tenant_id` on every tenant table, **RLS enabled AND forced**, the API connects as a dedicated non-superuser, non-owner role, and every request binds the tenant with a **transaction-scoped** `set_config(..., true)`. A session-level setting survives a pooled connection and leaks one tenant's id into the next request, and an unbound setting is the empty string, not NULL (policies cast with `nullif`).
- **Why not Supabase here:** the engine is Python and the free tier's two-project cap is already used; a local Postgres in Docker proves the same RLS design (it is the same Postgres) and costs nothing. Moving to Supabase later is a connection string, not a redesign.

## What a user can do (and which role)
| Screen | Agent | Admin |
|---|---|---|
| Sign in, switch store | yes | yes |
| Dashboard (volume, auto-resolved vs handed to a person, open handoffs) | yes | yes |
| Inbox: browse, filter by status and intent, search, open a conversation | yes | yes |
| Conversation: thread, what the agent looked up and why, order panel | yes | yes |
| Handoff queue: claim (one owner), reply as a person, resolve | yes | yes |
| Live chat as a customer (simulator and embeddable widget endpoint) | yes | yes |
| Order and product lookup (staff view) | yes | yes |
| Audit log | no | yes |
| Settings: edit the store's policy, see the connection (token never shown) | no | yes |

## Phases, each with a real check before the next
1. **Data + API.** `sql/schema.sql` (tables, RLS, grants), auth (bcrypt, signed cookie sessions, login rate limit), endpoints, engine wiring per tenant. **Check:** RLS negative tests through the app role (tenant A cannot read tenant B, agent cannot read the audit log, no binding reads nothing, a pooled connection does not carry a tenant), API tests.
2. **UI.** Reference-driven: screenshot best-in-class inboxes first, extract the proven layout (list | thread | context), build ours, screenshot ours at 390px and 1280px, compare, iterate. Skeleton loading, empty states with guidance, pending buttons, status never colour alone.
3. **Prove it.** Playwright walkthrough as admin and as agent with screenshots read back; a second tenant to show isolation on screen.
4. **Present it.** Only after he is happy with the app: `/demo-video`.

## Honest scope
Built to be reusable, not yet multi-client in production: one engine instance, tenants share the database, each tenant has its own Shopify connection and policy. The second demo tenant reuses the same dev store with a different policy to show isolation. No helpdesk integration (handoff stays inside the console), no email channel, no billing, no SSO. Logins are email and password for the demo; OTP email is the production choice and is a configuration step, not a redesign.
