# Claude API provider

The agent can answer with Anthropic's Claude API instead of Groq. `LLM_PROVIDER=anthropic` moves every path at once: the `/support` endpoint, the chat widget backend and the support console all build their model through `build_llm`, and Groq stays the default when nothing is set.

**Register: built and tested against a stand-in server written from Anthropic's documentation (saved in [vendor-contract/anthropic/](vendor-contract/anthropic/README.md) on 2026-11-06). Never called the live API: no Anthropic key or credit was used.** The first real call is the next step, and it needs a key.

## Switch to it

```
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...                  # or a line ANTHROPIC_API_KEY=... in the saved secrets folder, anthropic_key.txt
ANTHROPIC_WORKSPACE_ID=wrkspc_...      # only for a key that is not scoped to one workspace
ANTHROPIC_MODEL=claude-sonnet-5-5      # optional, this is the default
SUPPORT_AGENT_KEY=demo-key ./.venv/Scripts/python.exe -m uvicorn serve:app --port 8008
```

The same variables work for `demo.py`, `demo_memory.py` and the console (`console_api`). `LLM_PROVIDER=groq` or unset is exactly what ran before. A wrong provider name stops the process at start with a clear message.

## What the client does

`agent/anthropic_llm.py`, about 250 lines, httpx only (no vendor SDK). It implements the same `complete(system, user, max_tokens=...) -> str` interface as the Groq client. Each rule below is from a saved page.

- **Request.** `POST https://api.anthropic.com/v1/messages` with `Authorization: Bearer <key>` and `anthropic-version: 2026-10-21`; body `model`, `max_tokens`, `system`, `messages`. **No `temperature`:** the saved versioning page says that in API version 2026-10-21 "The temperature, top_p, and top_k request parameters are removed. A request that sends any of them returns a 400 error on every model." The Groq client sends `temperature: 0`; this one cannot.
- **Thinking.** Claude Sonnet 5.5 thinks adaptively when the field is left out. A support reply is short, so for `claude-sonnet-5-5` the client sends `thinking: {"type": "between_tools"}` ("the lowest thinking setting"), and sends nothing for any other model because the saved errors page says any other model answers it with a 400.
- **The reply** is the `text` blocks of `content`, joined and trimmed. Thinking blocks are not part of the answer.
- **Every way it can fail is a named error, never an empty reply.** All of them are `LLMError`s, so the agent hands the case to a person as it does for Groq.

| What comes back | Error | Retried |
|---|---|---|
| `end_turn` with text | the reply | |
| `end_turn`, no text | `LLMEmptyError` | no |
| `max_tokens` or `model_context_window_exceeded`, with or without text | `LLMTruncatedError` (`has_text` says which) | no |
| `refusal` (an HTTP 200) | `LLMRefusalError`, with `stop_details.category` | no |
| `tool_use`, `pause_turn`, `stop_sequence`, no `stop_reason`, a value added later | `LLMBadResponseError` | no |
| a 200 that is not JSON or not a message object | `LLMBadResponseError` | no |
| 400, 401, 402, 403, 404, 413 and other 4xx | `LLMAPIError` with the vendor's error type and a truncated message | no |
| 429 rate limit | `LLMAPIError` | yes, waits the `retry-after` seconds when it is longer than the backoff |
| 429 with `details.error_code` `enforced_spend_limit_reached` (the monthly cap) | `LLMSpendLimitError` | never, the saved page says it fails until access resumes |
| 500, 504, 529 and other 5xx | `LLMAPIError` | yes |
| connection refused or dropped | `LLMTransportError` | yes |
| read timeout | `LLMTransportError` | no, the model was already working for the whole timeout |

A retry is at most two ("twice by default" on the saved errors page), with a 1 s then 2 s backoff. A `retry-after` longer than 30 s fails fast instead of making a shopper wait; the 1 s base and the 30 s cap are this repo's choices, not the vendor's.

**What a failure shows.** A short line: status, the vendor's error type, a vendor message cut to 200 characters, and the `request-id`. The line ends up in the agent's steps, the audit log and the console, so it never holds the key (masked if the vendor's text echoes it), a gateway page, or a response body beyond that truncated message.

## How it is tested

`tests/anthropic_standin.py` is a real HTTP server on 127.0.0.1 written from the saved pages. It refuses what the pages say a caller must not send (no version header, a `temperature`, `between_tools` on another model, a last message that is not from the user, an unknown key) and answers in the documented shapes, so a test can break the client in either direction without a key. `tests/test_anthropic_llm.py` and `tests/test_anthropic_chat_path.py` cover it:

- a positive control before every negative test (the same request is answered normally first);
- the `stop_reason` matrix and the status matrix as tables;
- 429 then 200 with `retry-after`, 529 then 200, retries exhausted, a `retry-after` over the cap, the spend cap never retried, a dropped connection, a read timeout;
- 40 seeded random fault sequences checked against a plainly written oracle for the number of requests and the waits;
- the key never in an error, a log line or a `repr` of the settings;
- the whole agent on Claude through `/support`, the widget backend (`serve.chat_from_env`) and the console engine, including a Claude outage ending in a person and the grounding guard still refusing an invented figure in a Claude reply.

`tests/mutation_check.py` has 74 mutants for this provider, each one deleting or bending one guard (the Bearer scheme, the version header, no `temperature`, between_tools only on Sonnet 5.5, each row of the tables above, the retry count, the backoff, `retry-after` and its cap, the spend-cap code, the key mask, the truncation, the provider switch in each of the four entry points). A mutant that survives is a missing test, and fails the run.

## Measured, 2026-11-06 (Windows, Python 3.13)

See the saved output in [test-runs/](test-runs/): `2026-11-06-claude-provider-pytest.txt` and `2026-11-06-claude-provider-mutation.txt`.

- The two new test files: 142 tests, about 4 s.
- The engine suite: 625 tests (483 before this change), all passing, about 20 s.
- The console suite: 63 tests, all passing.
- The mutation harness: 205 mutants (131 before this change, 74 new), all caught.

## Not done

- **A live call.** No Anthropic key or credit was used, so no field name, header or error shape here has been checked against the real service. Everything above is checked against the saved documentation only.
- **Streaming.** The agent waits for a whole reply, so the client does not use `stream: true`. The saved pages describe streaming, but nothing here reads it.
- **Tools, caching, batches, citations, structured outputs, effort.** Not used, so not built. The reply is plain text and the grounding guard checks it afterwards.
- **A fallback to another model after a refusal.** The saved pages describe one; here a refusal goes to a person.
- **Token usage and cost.** The `usage` object is not read or logged.
- **`x-api-key`.** The saved overview calls it a "Legacy fallback" and the client sends `Authorization: Bearer`.
- **Thinking settings for other models.** Only Sonnet 5.5's `between_tools` is set. Another model id is sent with the field left out (adaptive thinking), which the saved pages say nothing more about.
- **Whether thinking tokens count against `max_tokens`.** Not stated on the saved pages; a `max_tokens` stop with no text is a named error either way.
- **`retry-after` as a date, and a base delay for the backoff.** Not on the saved pages; see [vendor-contract/anthropic/README.md](vendor-contract/anthropic/README.md).
- **A per-tenant model or key in the console.** One provider and one key for the whole process.
