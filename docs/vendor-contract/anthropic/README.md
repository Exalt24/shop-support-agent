# Anthropic API vendor pages

*The Anthropic pages the Claude API provider was written against, saved as text on 2026-11-06 from a real Chromium load.*

The stand-in server in `tests/anthropic_standin.py` and the client in `agent/anthropic_llm.py` follow these files, not memory. Each file starts with its URL. Quotes in the code and in [../../claude-api-provider.md](../../claude-api-provider.md) are from these files.

| File | What the client takes from it |
|---|---|
| `api-messages-create.txt` | `POST /v1/messages`; required body fields `max_tokens`, `messages`, `model`; optional `system`, `thinking`; the response fields `content`, `stop_reason`, `stop_details`, `usage`; the `anthropic-version` and `anthropic-workspace-id` headers; the example call at `https://api.anthropic.com/v1/messages` |
| `api-overview-authentication-headers.txt` | the request headers: `Authorization: Bearer <token>` (required unless `x-api-key` is set), `x-api-key` as a "Legacy fallback", `anthropic-version` required, `anthropic-workspace-id` for a key that is not scoped to a workspace; the `request-id` response header |
| `api-versioning.txt` | `anthropic-version: 2026-10-21`; in that version "The temperature, top_p, and top_k request parameters are removed. A request that sends any of them returns a 400 error on every model." |
| `api-errors.txt` | the status codes (400, 401, 402, 403, 404, 413, 429, 500, 504, 529) and their `type`s; the error object `{"type": "error", "error": {"type", "message"}, "request_id"}`; the SDK's retry rule ("twice by default, honoring the retry-after header"); the Sonnet 5.5 thinking rules (`between_tools`) |
| `api-rate-limits.txt` | `retry-after` ("wait that many seconds before retrying"); the spend-cap 429 with `error.details.error_code` `enforced_spend_limit_reached`, which has no `retry-after` and fails until access resumes |
| `handling-stop-reasons.txt` | the `stop_reason` values (`end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `pause_turn`, `refusal`, `model_context_window_exceeded`); a refusal is an HTTP 200 |
| `models-overview.txt` | the model id `claude-sonnet-5-5` (Claude Sonnet 5.5); thinking is adaptive on it; `between_tools` is accepted by it |

## What the pages do not say

These choices come from the client's own design, so they are named here and are not claims about the saved pages.

- **Backoff base and cap.** The pages say "exponential backoff, twice by default" and give no first delay. The client waits 1 s, then 2 s, and it refuses to wait for a `retry-after` over 30 s (a shopper is waiting), which is this repo's choice.
- **`retry-after` as a date.** The saved rate limits page says the header is a number of seconds. A value that is not a finite non-negative number is ignored and the backoff is used.
- **Whether thinking tokens count against `max_tokens`.** Not stated on the saved pages. The client asks Sonnet 5.5 for `between_tools` (the lowest thinking setting) and treats a `max_tokens` stop with no text as a named error either way.
- **How to join several text blocks.** The pages do not say. The client joins them with nothing between them.
- **The exact wording of a missing-header or bad-`max_tokens` error.** Not on the saved pages. The stand-in's wording for those two is its own, and the client does not depend on it.
- **The `Empty responses with end_turn` section** of the stop reasons page was collapsed when the page was loaded, so its text is not in the saved file. The client treats an empty completion as an error whatever the reason.
- **`x-api-key`.** The overview page lists `Authorization: Bearer` first and calls `x-api-key` a "Legacy fallback for Authorization, still supported". The client sends `Authorization: Bearer`, as the Messages reference's own example call does.
