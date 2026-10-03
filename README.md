# shop-support-agent

A customer-support agent for a Shopify store. It looks up orders and products through the Admin GraphQL API, answers from the store's own policy file, and hands anything it should not decide to a person. Built as a working proof of the first of the five agents in an e-commerce AI-agent brief: support.

**Register: built, tested and run live against a Shopify development store (fake customers, no real data). Not deployed, zero real users, one platform (Shopify).** The human handoff produces a ticket-shaped payload; it is not yet wired to a helpdesk.

## What it does

`SupportAgent.handle(message, email)` runs in a fixed order, and the order is the design:

1. **Escalation floor first.** A legal threat, a chargeback, a safety report or a request for a person is matched in code before the model sees the message. The model also gets a vote (angry, wants a human), but it can only add escalations, never remove one.
2. **Triage** (the model): intent, order number, product query. It never sees order data.
3. **Facts** come from Shopify and `policy.md`, never from the model's memory. Orders are **identity-checked**: the email on the message must match the email on the order.
4. **The model writes the wording only.** A grounding guard refuses any reply containing a figure not in the facts (digits *and* numbers written as words), a commitment the agent has no authority to make (refund, discount, replacement), or an outcome the facts do not state ("the tracking details were emailed to you").
5. **Anything that fails ends in a person**: a store outage, a model outage, a reply that fails the guard twice. A failed lookup is never reported as "no such order".
6. **Every turn is logged** (`logs/audit.jsonl`): intent, order, decision, escalation reason, steps. Emails are masked.

## Proof

```
./.venv/Scripts/python.exe -m pytest tests -q              # 49 tests, offline (scripted model, fake shop)
./.venv/Scripts/python.exe tests/mutation_check.py         # 11 mutants: each guard broken on purpose
./.venv/Scripts/python.exe demo.py                         # nine messages, live store + live model
```

- **Mutation harness:** each of 11 guards is deleted from the real source, the suite is run, and the file is restored byte-exact. All 11 mutants are caught. The identity check, the throttle-as-HTTP-200 handling, the legal-threat rule, the grounding guard, the number-word check and `status:active` product filtering each have a test that fails without them.
- **Live transcript:** `demo_transcript.md`. Nine messages including three hostile ones (a refund demand, a prompt injection asking for a 50% discount code, a legal threat). 7 answered, 2 escalated, and no promise, invented figure or foreign order detail reached a customer.
- **Through n8n:** `n8n/workflow.json` (webhook, agent, "needs a person?" branch, handoff ticket, reply) was imported into a real n8n 2.41.6 server, published, and called over its webhook with an answered message, a wrong-email message and a legal threat. Regenerate it with `python n8n/build_workflow.py`.

## What the live runs taught (and what changed)

- Retrieval ranked the shipping section above Returns because "return" did not match "Returns". Fixed with light stemming; a test pins it.
- The store holds draft and archived products. Product search is now `status:active` only.
- The first live run produced two statements with no figure in them that the figure check passed: "the tracking details were emailed to you" (nothing said an email was sent) and "the payment will be returned" after a cancellation (the policy never says so). The guard and prompt now refuse unsupported outcomes.
- A model writing "two business days" got past a digit-only check. Number words followed by a unit are now figures too.

## The support console (login, roles, many stores)

A web console around the same engine, so a team can use it and a new store is a new row rather than a new build. `console_api/` (FastAPI) holds the rules and `console_web/` (Next.js) is the screen.

- **Roles and stores.** Sign in with email and password. An **admin** sees everything for a store, plus the audit log and the policy editor. An **agent** sees the inbox, handoffs, live chat and lookup. One person can belong to several stores and switch between them. A store you do not belong to answers 404, never 403, so its existence is not confirmed.
- **Pages.** Dashboard, a three-pane inbox (list, thread, order and agent steps), a handoff queue with one owner per case (claim, reply, resolve), a live chat that talks to the real agent as a customer, order and product lookup, an admin audit log, and a policy editor. Every status is an icon plus a word. It works at phone width.
- **Isolation lives in the database.** One Postgres database, a `tenant_id` on every tenant table, row level security enabled and forced. The API connects as `console_app`, which is not a superuser, does not bypass row security and does not own the tables; startup refuses to serve otherwise. The tenant is bound per transaction. The audit log is append-only and admin-readable. The Shopify token is stored encrypted and is never sent to the browser.
- **Login.** bcrypt, a signed session cookie (httponly, SameSite=Lax), a lockout after five failures per ten minutes per email and address, and a CSRF guard on writes.

```
docker run -d --name support-console-pg -e POSTGRES_PASSWORD=localsuper -e POSTGRES_DB=console -p 54931:5432 postgres:17
./.venv/Scripts/python.exe -m console_api.migrate --reset        # schema, roles, forced RLS
./.venv/Scripts/python.exe -m console_api.seed                   # demo stores, two logins, a week of conversations
CONSOLE_ALLOWED_ORIGINS=http://localhost:3100 ./.venv/Scripts/python.exe -m uvicorn console_api.main:app --port 8010
cd console_web && npm install && npx next dev --port 3100        # then open http://localhost:3100
```

Demo logins (local only, written in `console_api/seed.py`): `admin@demo.test` / `demo-admin-pass` (both stores), `agent@demo.test` / `demo-agent-pass` (one store).

```
./.venv/Scripts/python.exe -m pytest tests_console -q             # 36 tests: isolation, roles, login, the API
./.venv/Scripts/python.exe tests_console/mutation_check.py        # 13 mutants: each database rule broken on purpose
cd console_web && node tests/e2e.js                               # 37 browser checks at 1280 and 390, both roles
```

Run the engine and console suites as two commands, not one (two `conftest.py` files collide). The mutation harness rebuilds the local `console` database, so reseed afterwards and never point it at real data.

**Before any real use:** `python -m console_api.seed --purge` removes every demo row, then set real passwords and connect a real store. The seed passwords are public in this file on purpose and must never reach a live database.

**Reusing it for another store:** insert a `tenants` row (slug, name, shop domain, encrypted token via `console_api/secrets_store.py`, policy markdown) and a `memberships` row per person. The engine reads that store's orders and policy; nothing else changes. A different shop platform means swapping `agent/shopify.py`, not the console.

## The demo video

`demo/` regenerates a 3 minute narrated walkthrough of the console. `beats.json` is the script, `record.mjs` drives the real UI with Playwright and takes a mark at each moment a scene is actually on screen, `assemble.py` places the voice at those marks, cuts dead air and burns the subtitles into a bar below the frame, and `frames.py` pulls a frame at every mark so each one can be checked against its subtitle. Voice generation uses the shared tools in `Operations/tools/narration/` (three candidates per beat, an ASR gate, then a finishing pass). Record against a production build, not `next dev`.

```
python -m console_api.seed                                  # clean fake data; the run claims and resolves a case
cd console_web && npm run build && npx next start -p 3100   # and the API on 8010 with CONSOLE_ALLOWED_ORIGINS=http://localhost:3100
node demo/record.mjs && python demo/assemble.py && python demo/frames.py mark
```

The narration is Daniel's own voice, cloned and generated. The recording touches only the local app and the development store, and `record.mjs` refuses a non-local URL.

## Honest limits

- The console has not been deployed, has no real users, and is local only. Session lockout counters are in memory (one process).
- The guard checks figures, a list of commitment phrases and unsupported outcomes. It cannot judge every sentence; wording-level claims beyond those patterns rely on the prompt and on human review of escalations.
- Policy retrieval is word overlap over a handful of sections, which is enough here and would not be for a large knowledge base (that is what a vector store is for).
- Out-of-scope messages are escalated rather than answered, which is the conservative choice and will produce more human work than a looser agent.
- Klaviyo, Gorgias, Zendesk and the ad platforms are not integrated.

## Run it

```
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -r requirements.txt
# keys: GROQ_API_KEY, SHOPIFY_SHOP, SHOPIFY_ACCESS_TOKEN in the environment (or the saved secret files, see agent/config.py)
./.venv/Scripts/python.exe seed_fulfillment.py              # marks one dev-store order shipped with tracking
SUPPORT_AGENT_KEY=demo-key ./.venv/Scripts/python.exe -m uvicorn serve:app --port 8008
```
