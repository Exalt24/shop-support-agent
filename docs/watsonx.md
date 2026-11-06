# IBM watsonx.ai provider

The agent can answer with IBM watsonx.ai instead of Groq or Claude. `LLM_PROVIDER=watsonx` moves every path at once: the `/support` endpoint, the chat widget backend and the support console all build their model through `build_llm`, and Groq stays the default when nothing is set.

**Register: written from IBM's documentation and tested only against a stand-in server I wrote from the saved pages (in [vendor-contract/ibm-watsonx/](vendor-contract/ibm-watsonx/README.md), retrieved 2026-11-06). Never called a live IBM Cloud account: we hold none, so no field name, header, version date, model id or error shape here has been confirmed by the real service.** A live run needs `WATSONX_API_KEY` and `WATSONX_PROJECT_ID` in the environment, and it is the first thing to do with an account.

## Switch to it

```
LLM_PROVIDER=watsonx
WATSONX_API_KEY=...                    # an IBM Cloud API key, or a line WATSONX_API_KEY=... in the saved secrets folder, watsonx_key.txt
WATSONX_PROJECT_ID=...                 # the watsonx.ai project the key can run prompts in (Admin or Editor role)
WATSONX_URL=https://us-south.ml.cloud.ibm.com   # optional, this Dallas endpoint is the default
WATSONX_MODEL_ID=ibm/granite-4-h-small          # optional, this is the default
SUPPORT_AGENT_KEY=demo-key ./.venv/Scripts/python.exe -m uvicorn serve:app --port 8008
```

The same variables work for `demo.py`, `demo_memory.py` and the console (`console_api`). A missing key or project id stops the process at start and names the variable; the key is never in the message. A `WATSONX_URL` that is not https (plain http is accepted for this machine only) is refused, so the key and the token cannot cross the network in the clear.

## What the client does

`agent/watsonx_llm.py`, about 340 lines, httpx only (no IBM SDK, nothing new in `requirements.txt`). It implements the same `complete(system, user, max_tokens=...) -> str` interface as the Groq and Claude clients.

**Taken from IBM's pages**

- **Token.** `POST https://iam.cloud.ibm.com/identity/token`, form encoded, `grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey=<key>`; the bearer token is the `access_token` field. The pages say tokens last 60 minutes and "you are responsible for refreshing the token when it expires".
- **Chat.** `POST {WATSONX_URL}/ml/v1/text/chat?version=2025-10-25` with `Authorization: Bearer <token>` and a body of `model_id`, `project_id`, `messages` (roles in lower case, a system message then the user message), `max_completion_tokens` and `temperature: 0`. The reply is `choices[0].message.content`, trimmed; `reasoning_content` is ignored.
- **A 401.** IBM's own SDKs treat `authentication_token_expired` as "refresh the token and retry once". This client does the same for any 401: it drops the token, fetches a new one and repeats the call once. A second 401 is raised. The refresh does not use up the retry budget below.
- **Transient failures.** The SDKs retry 429, 502, 503, 504 and 520 with exponential backoff. This client retries a 429, any 5xx and a dropped connection.
- **Errors.** The body is `{"errors": [{"code", "message", "more_info"}], "trace": "..."}`. A failure shows as `HTTP 403 authorization_rejected: <message, cut to 200 characters> (trace <id>)`.

**This repo's choices (no saved page says them)**

- **Two retries, 1 s then 2 s,** and a `Retry-After` header, when there is one, is honoured as a number of seconds; one longer than 30 s fails fast because a shopper is waiting. IBM's SDKs retry ten times, which is too long for a chat reply.
- **A 429 with `token_quota_reached` is never retried.** The code is on the saved Java SDK page; that it means "do not wait for it" is my reading.
- **The token is renewed 120 s early** and kept in memory only. It is never logged, never written to a file and masked if the vendor's text echoes it. One lock means eight threads that start together fetch one token.
- **The token expiry** is read from `expires_in` first (a relative figure, so a clock that disagrees with IBM's cannot expire a fresh token), then `expiration` (epoch seconds), then the documented hour.
- **No tools.** The agent calls the model with plain text and checks the reply afterwards, so the client sends no `tools` field and a `tool_calls` answer is a named error. The shape of a tool-call answer is known only from a search result quoting IBM's tool-calling page, which would not load, so none of it is built.
- **A read timeout is not repeated** (the model was already working for the whole 60 s), a refused or dropped connection is.

| What comes back | Error | Retried |
|---|---|---|
| `stop` with text | the reply | |
| `stop`, no text | `WatsonxEmptyError` | no |
| `length` or `time_limit`, with or without text | `WatsonxTruncatedError` (`has_text` says which) | no |
| `tool_calls`, `cancelled`, `error`, no `finish_reason`, a value added later | `WatsonxBadResponseError` | no |
| a 200 with no `choices` (moderation can leave none), or a body that is not JSON | `WatsonxBadResponseError` | no |
| 400, 403, 404 and other 4xx | `WatsonxAPIError` with the vendor's code and a truncated message | no |
| 401 | one token refresh and one repeat, then `WatsonxAPIError` | once |
| 429 | `WatsonxAPIError` | yes, waits `Retry-After` when it is longer than the backoff |
| 429 `token_quota_reached` | `WatsonxQuotaError` | never |
| 500, 502, 503, 504, 520 and other 5xx | `WatsonxAPIError` | yes |
| the token request refused (a wrong or revoked key) | `WatsonxAuthError` with IAM's code | no, unless IAM itself answered 429 or 5xx |
| connection refused or dropped | `WatsonxTransportError` | yes |
| read timeout | `WatsonxTransportError` | no |

All of them are `LLMError`s, so the agent hands the case to a person as it does for Groq.

## What had to be assumed

Listed in full in [vendor-contract/ibm-watsonx/README.md](vendor-contract/ibm-watsonx/README.md). The ones that could break a first live call:

- **The default model id `ibm/granite-4-h-small`.** IBM's chat page names it only as the model of a sample notebook. If the account or region does not have it, the service answers 400 `model_not_supported` (the code is on the saved Java SDK page) and `WATSONX_MODEL_ID` fixes it.
- **The version date `2025-10-25`.** It is the date of the chat page's reasoning-model example. An older date from the same page is the fallback if the service refuses it.
- **The Dallas host** is from the brief, not from a saved page.
- **The token endpoint's error fields** (`errorCode`, `errorMessage`, for example `BXNIM0415E`) and the `expires_in` and `expiration` fields come from IBM's IAM page as a search result quoted it. The page itself would not load in a browser here, and neither would the watsonx.ai API reference, so the reference was read through a summarising fetch and nothing is quoted from it.
- **`temperature: 0` together with `max_completion_tokens`** is accepted by the reference as read; a reasoning model that rejects a temperature would answer 400 `invalid_input_argument`.
- **`Retry-After` on a 429.** The pages say a 429 response header carries "information about the limit" and do not name it.

## How it is tested

`tests/watsonx_standin.py` is a real HTTP server on 127.0.0.1 (one port, IAM and the chat API) written from the saved pages. It refuses what the pages say a caller must not send (a JSON token request, a wrong grant type, an unknown key, a capital-letter role, a version date the page does not show, both or neither of `project_id` and `space_id`, a model id the pages do not name, an expired or never-issued token) and answers in the documented shapes. It shares a fake clock with the client, so a token really expires on the server when a test moves time. `tests/test_watsonx_llm.py` and `tests/test_watsonx_chat_path.py` cover it:

- a positive control before every negative test (the same request is answered normally first);
- the token fetched once and reused, renewed one second before the margin and not before, expiry read from `expires_in`, then `expiration`, then the default hour, a malformed IAM answer, a wrong key, IAM itself failing, eight threads fetching one token;
- a 401 refreshed once and retried once, a second 401 raised, a refresh that does not spend the retry budget;
- the finish-reason table and the status table, 429 with `Retry-After` (waited, shorter than the backoff, at the cap, over the cap, unusable), the quota 429 never retried, retries stopping at the cap, a dropped connection, a refused connection, read timeouts on both calls;
- 40 seeded random fault sequences (429, 5xx, drops, 400, quota, 401 mixed) checked against a plainly written oracle for the number of requests, the waits and the number of tokens fetched;
- the key and the token never in an error, a log line, a `repr` of an exception, the settings, a file, stdout or stderr, even when the vendor's text, a gateway page, the error code, the trace id or a `finish_reason` echoes them;
- the whole agent on watsonx through `/support`, the widget backend (`serve.chat_from_env`) and the console engine, including an outage ending in a person, a token that expires between two messages, and the grounding guard still refusing an invented figure in a watsonx reply.

`tests/mutation_check.py` has 120 mutants for this provider, each one deleting or bending one guard. A mutant that survives is a missing test, and fails the run. It was run in a copy of the repo, because an automatic sync can commit a working copy in the middle of a run.

## Measured, 2026-11-06 (Windows, Python 3.13)

See the saved output in [test-runs/](test-runs/): `2026-11-06-watsonx-provider-pytest.txt` and `2026-11-06-watsonx-provider-mutation.txt`.

- The two new test files: 211 tests, about 7 s.
- The engine suite: 836 tests (625 before this change), all passing, about 27 s. This includes the browser tests.
- The mutation harness, run in a copy of the repo: 325 mutants (205 before this change, 120 new), all caught. The first pass over the 120 new ones left 7 alive; each was a missing test (a trailing slash on a local URL, the `expiration` epoch used alone, whitespace around the token, the IAM backoff doubling, the IAM error code in the message, a non-object error body, whitespace around `WATSONX_MODEL_ID`), tests were added, and the whole harness was run again.
- Not run: the console suite (`tests_console`, 63 tests) needs the local Postgres, and this change does not touch `console_api` beyond the shared `build_llm` it already calls.

## Not done

- **A live call.** No IBM Cloud account, key or project was used, so nothing above has been checked against the real service. Everything is checked against the saved documentation and a stand-in only.
- **Tools.** The agent does not use them, so the client sends none and builds no tool-call round trip.
- **Streaming.** The agent waits for a whole reply; the client does not use `/ml/v1/text/chat_stream`.
- **`time_limit`, guardrails and moderation settings, `space_id`.** Not sent. A project id is required.
- **Token usage and cost.** `WatsonxLLM` can pass the `usage` object to an `on_usage` callback; nothing in the agent sets one yet.
- **The AWS and on-premises IBM sign-in routes** (the credentials page lists an AWS token endpoint). Only IBM Cloud IAM is built.
- **A per-tenant model, key or project in the console.** One provider and one key for the whole process.
