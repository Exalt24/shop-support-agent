# Notion ticket log

**Register: built and tested against a stand-in.** The Notion API is an in-process stand-in (`agent/notion_fake.py`) written from the pages saved in `docs/vendor-contract/notion-api-*.txt`. The code has not been run against a live workspace. The saved create-a-page page shows the SDK call and not the URL, so `POST /v1/pages` is the endpoint that call wraps, and a live run confirms it along with the property names of the real data source.

## The flow

1. An escalated turn becomes a ticket (`ticket_from_result`).
2. `NotionTicketLog.record` looks the ticket up in the data source by its `Ticket` title. If the row exists it does nothing. If not, it creates a page with the ticket id, queue, reason, intent and status Open.
3. A Take or Resolve click in Discord calls `InteractionDesk.on_change`, which can call `set_status` to move the row to Taken or Resolved. A failing log never turns a click into an error.

Notion has no idempotency key, so the look-up is the idempotency. It also runs before every retry of a write, because a lost answer does not say whether Notion applied the write.

## Rules, each with the line it comes from

| Rule | Source page | Pinned by |
|---|---|---|
| `Notion-Version` is required on every request, latest 2026-03-11 | `notion-api-versioning.txt` | `test_the_version_header_is_always_sent` |
| A page in a data source uses `parent.data_source_id` and property names that match its schema | `notion-api-create-a-page.txt` | `test_a_new_ticket_is_looked_up_then_created_with_the_documented_headers_and_body` |
| A data source is queried at `/data_sources/{id}/query` with a filter object | `notion-api-query-a-data-source.txt` | same test, `test_two_look_alike_tickets_are_two_rows` |
| A 429 or 529 waits the integer `Retry-After`; `public_api_request_blocked` is not retried | `notion-api-request-limits.txt` | `test_a_429_waits_the_retry_after_header_then_succeeds`, `test_a_blocked_connection_is_not_retried` |

## What is kept

The ticket id, queue, reason, intent and status. Never the customer's words, email or phone: `test_the_row_holds_no_customer_words_email_or_phone` and the random-world test assert it.

## Proof

```
./.venv/Scripts/python.exe -m pytest tests/test_notion_log.py -q     # 65 tests
./.venv/Scripts/python.exe tests/mutation_check.py Notion             # 14 mutants, each guard removed on purpose
```

The random-world test builds 40 seeded worlds with several processes sharing one data source, lost answers, 429, 529, 5xx, 403 and dropped connections. It checks one row per ticket and no personal data on the wire. Its first run found a real bug: a create whose answer was lost was repeated without looking the ticket up again, so the ticket got two rows. The look-up now runs before every retry.

## Limits

Properties are the five in `agent/notion_fake.py` (`Ticket`, `Queue`, `Reason`, `Intent`, `Status`), so a real data source needs the same names. One query page, no pagination. Not wired into `serve.py` yet.
