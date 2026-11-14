# WhatsApp lane

**Register: built and tested against a stand-in.** The WhatsApp Cloud API is an in-process stand-in (`agent/whatsapp_fake.py`) written from the pages saved in `docs/vendor-contract/whatsapp-*.txt` and `meta-graph-webhooks-getting-started.txt`. The code has not been run against a live business account. The first thing a live run confirms is the bytes that are signed, because the saved page does not say how the payload is escaped before signing.

## The flow

1. Meta calls the webhook with a GET to verify it. `WhatsAppDesk.handshake` echoes `hub.challenge` for a subscribe request with the right verify token and refuses anything else.
2. A customer messages the business number. Meta posts a notification. `WhatsAppDesk.receive` checks `X-Hub-Signature-256` against the App Secret and answers 401 to a forged request.
3. Each text message goes to the responder (the support agent) under a conversation id made from a hash of the sender, never the number. The reply goes back as a service message.
4. An escalated turn also posts one staff ticket through the Discord handoff (`docs/discord-handoff.md`), keyed on a hash of the message id.

## Rules, each with the line it comes from

| Rule | Source page | Pinned by |
|---|---|---|
| The handshake echoes the challenge only for `hub.mode=subscribe` and a matching verify token | `meta-graph-webhooks-getting-started.txt` | `test_any_other_handshake_is_refused` |
| Each notification is signed with the App Secret, `X-Hub-Signature-256: sha256=...`; compare everything after `sha256=` | same page | `test_a_valid_signature_verifies_and_a_changed_byte_does_not`, `test_an_unsigned_or_forged_notification_gets_a_401_and_nothing_is_sent` |
| Answer every verified notification with 200; retries come for up to 7 days and can duplicate | same page, `whatsapp-cloud-set-up-webhooks.txt` | `test_the_same_message_id_delivered_again_is_answered_once`, `test_a_failing_responder_is_still_a_200_so_meta_does_not_retry_for_a_week` |
| Messages sit in `entry[].changes[].value.messages[]` with `from`, `id`, `type` and `text.body`; a `statuses` value needs no reply | `whatsapp-cloud-webhook-components.txt` | `test_a_status_only_notification_needs_no_reply`, `test_odd_shapes_are_a_200_and_never_an_exception` |
| The send is a POST to `/{phone_number_id}/messages` with a Bearer token and `messaging_product`, `to`, `type`, `text.body`; a free-form reply is allowed inside the customer service window the customer's message opens | `whatsapp-cloud-send-messages.txt` | `test_a_text_message_is_answered_to_the_sender_in_the_documented_shape` |

## What is kept

Counts and kinds only. The audit never holds the number or the words, and the staff ticket carries a hash of the message id. `test_the_audit_holds_kinds_and_counts_never_the_number_or_the_words` and the random-world test assert it.

## Proof

```
./.venv/Scripts/python.exe -m pytest tests/test_whatsapp_lane.py -q     # 82 tests
./.venv/Scripts/python.exe tests/mutation_check.py WhatsApp              # 17 mutants, each guard removed on purpose
```

The random-world test builds 40 seeded worlds with forged and valid notifications, duplicates, sends that fail or drop and escalations. It checks each message id is handled once, one staff ticket per escalated message, replies only to real senders and no number or words in the audit or the staff channel.

## Limits

One process: the set of handled message ids (the last 5,000) and the Discord ledger are in memory. Text messages only; any other type gets one fixed reply. No templates, so a reply outside the 24 hour window is not attempted. Not wired into `serve.py` yet.
