# Discord handoff

**Register: built and tested against a stand-in.** The Discord server is an in-process stand-in (`agent/discord_fake.py`) written from the pages saved in `docs/vendor-contract/discord-*.txt`. The code has not been run against a live server, so the signed bytes and the button payloads are the two things a first live run confirms.

## The flow

1. The agent escalates a turn (`core.Result.action == "escalated"`). `ticket_from_result` turns it into `{id, queue, reason, intent}`, or `None` when the agent answered.
2. `DiscordHandoff.post` sends one message to the staff channel with two buttons, Take and Resolve.
3. A person clicks. Discord calls the interactions endpoint. `InteractionDesk.handle` verifies the signature, then moves the ticket and edits the message in place.

## Rules, each with the line it comes from

| Rule | Source page | Pinned by |
|---|---|---|
| Content is at most 2000 characters, the nonce at most 25, `enforce_nonce` returns the first message for a repeated nonce | `discord-resource-message.txt` | `test_the_nonce_fits_25_characters_and_is_stable_per_ticket`, `test_a_lost_answer_is_retried_without_a_second_message` |
| A 429 is retried after `retry_after`; 401, 403 and 400 are not retried | `discord-rate-limits.txt` | `test_a_429_waits_the_retry_after_the_body_gives_then_succeeds`, `test_a_refusal_a_retry_cannot_fix_is_not_retried` |
| A button sits in an Action Row, needs a `custom_id` of 1 to 100 characters | `discord-components-reference.txt` | `test_post_sends_the_documented_url_header_and_body` |
| Each interaction carries `X-Signature-Ed25519` and `X-Signature-Timestamp`; a failed check is a 401; a PING is answered with a PONG | `discord-interactions-overview.txt` | `test_a_forged_click_changes_nothing`, `test_a_ping_is_answered_with_a_pong` |
| A component click is type 3 and is answered with type 7 (edit the message) or type 4 with flag 64 (a private reply) | `discord-interactions-receiving-and-responding.txt` | `test_take_updates_the_message_disables_take_and_records_who` |

The saved overview page does not print which bytes are signed. The code signs and verifies the timestamp followed by the raw body, which is Discord's published example. That is the rule to fix first if a live server disagrees.

## What goes in the channel

The ticket id, the queue, the rule that fired and the intent. Never the customer's words, email, phone or order. A staff channel is a chat room, so personal data stays in the helpdesk the ticket points to. `test_the_channel_never_sees_the_customers_words_email_or_phone` and the random-world test both assert it.

## Proof

```
./.venv/Scripts/python.exe -m pytest tests/test_discord_handoff.py -q     # 76 tests
./.venv/Scripts/python.exe tests/mutation_check.py Discord                 # 19 mutants, each guard removed on purpose
```

The random-world test builds 40 seeded worlds of tickets, posts with 429, 5xx, dropped and lost-answer faults, and signed clicks by several users with replays. It checks one message per ticket, a ticket never moving backwards, one person taking a ticket and no personal data on the wire.

## Limits

One process: the ledger of posted tickets and the ticket states are in memory. One channel. Take and Resolve only. The post is not wired into `serve.py` yet.
