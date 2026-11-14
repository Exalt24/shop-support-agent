# xAI Grok chat provider and Grok Imagine client

The agent can answer with xAI's Grok instead of Groq, Claude or IBM watsonx.ai. `LLM_PROVIDER=xai` moves every path at once: the `/support` endpoint, the chat widget backend and the support console all build their model through `build_llm`, and Groq stays the default when nothing is set. A small second client, `agent/xai_imagine.py`, covers Grok Imagine: images from a prompt and a video from a still image.

**Register: written from xAI's documentation and tested against a stand-in server; never run against the live xAI API. No xAI key exists here, and image and video generation are paid per request, so no field name, header, status, URL or answer shape in this repo has been confirmed by the real service.** The stand-in is a server I wrote from the saved pages ([vendor-contract/xai-*.txt](vendor-contract/), retrieved 2026-11-15). A first live run needs only `XAI_API_KEY` and is the first thing to do with an account.

## Switch to the chat provider

```
LLM_PROVIDER=xai
XAI_API_KEY=...                          # or a line XAI_API_KEY=... in the saved secrets folder, xai_key.txt
XAI_MODEL=grok-4.7                       # optional, this is the default
XAI_BASE_URL=https://api.x.ai/v1         # optional, this is the default; the pages also show https://us.api.x.ai/v1
SUPPORT_AGENT_KEY=demo-key ./.venv/Scripts/python.exe -m uvicorn serve:app --port 8008
```

A missing key stops the process at start and names the variable; the key is never in the message. A base URL that is not https (plain http is accepted for this machine only) is refused, so the key cannot cross the network in the clear.

## What the chat client does

`agent/xai_llm.py` (the model client) and `agent/xai_http.py` (the HTTP layer both xAI clients share), httpx only: no xAI SDK, nothing new in `requirements.txt`. It implements the same `complete(system, user, max_tokens=...) -> str` interface as the other providers.

**Taken from xAI's pages**

- **Request.** `POST https://api.x.ai/v1/chat/completions` with `Authorization: Bearer <key>` and a body of `model`, `messages` (a system message then the user message, roles in lower case), `max_completion_tokens` (the reference marks `max_tokens` deprecated) and `temperature: 0`. No tools, no streaming, no `stop`.
- **Reasoning.** The pages say `grok-4.7`, `grok-4.6` and `grok-4.5` take `reasoning_effort`, that it defaults to `"high"` and that reasoning cannot be turned off. A support turn is latency-sensitive, so the client sends `"low"` to those three models and sends nothing to any other model.
- **Answer.** `choices[0].message.content`, trimmed. `message.refusal` is "the reason given by model if the model is unable to generate a response", so a refusal is a named error. `usage` carries `cost_in_usd_ticks` (ten billion ticks to the dollar), which an optional `on_usage` callback receives with the token counts.
- **Statuses** from the Debugging Errors page: 400, 401, 403, 404, 405, 415 and 422 are named with their documented cause, 429 is "reached the rate limit". The Rate Limits page says "implement exponential backoff" and its sample waits `2 ** attempt` seconds.

**This repo's choices (no saved page says them)**

- **Two retries, 1 s then 2 s,** for a 429 and a 503 only. The 503 is named once in the Videos reference ("Engine overload ... surfaces here as service_unavailable (HTTP 503)"). Any other 5xx is not retried, because no saved page lists one. The sample retries five times, which is too long for a shopper who is waiting.
- **No error body is ever read or shown.** No saved page gives the JSON body of an HTTP error, a `Retry-After` header or any rate limit header, so a failure shows the status and the documented cause and nothing else. That also means a body that echoes the key cannot reach a log, the audit file or the console. A `Retry-After` header is not honoured, because nothing says it exists.
- **A reply that finished for any reason other than `stop` is an error.** The reference lists `stop` and `length`, and one sentence that also mentions `end_turn` without saying when it is sent in a non-streaming call, so an `end_turn` ends in a person instead of in a guess.
- **The default model.** `grok-4.7` is the model of the chat guide's own example; whether a given account can use it is not on any page. `XAI_MODEL` changes it.
- **A read timeout is not repeated** (the model was already working for the whole 60 s). A refused or dropped connection is, for chat.

| What comes back | Error | Retried |
|---|---|---|
| `stop` with text | the reply | |
| `stop`, no text | `XaiEmptyError` | no |
| `length`, with or without text | `XaiTruncatedError` (`has_text` says which) | no |
| any other `finish_reason`, a tool call, a missing one | `XaiBadResponseError` | no |
| a `refusal` | `XaiRefusalError` | no |
| a 200 with no `choices`, or a body that is not a JSON object | `XaiBadResponseError` | no |
| 400, 401, 403, 404, 405, 415, 422 | `XaiAPIError` with the status and its documented cause | no |
| 429 and 503 | `XaiAPIError` | yes, 1 s then 2 s |
| any other status, a 202 included | `XaiAPIError` | no |
| connection refused or dropped | `XaiTransportError` | yes, for chat and for polls |
| read timeout | `XaiTransportError` | no |

All of them are `LLMError`s, so the agent hands the case to a person as it does for every other provider.

## Grok Imagine: images and image to video

`agent/xai_imagine.py` is a library. It is not wired into the support agent, which answers customers and makes no pictures.

```python
from agent.xai_imagine import XaiImagine

api = XaiImagine(key)                                             # base URL and model names are the defaults from agent/config.py
batch = api.generate("A watercolor lighthouse at dawn", n=2, aspect_ratio="16:9", resolution="1k", quality="low")
print([i.url for i in batch.images], batch.cost_usd)              # temporary URLs, cost from usage.cost_in_usd_ticks

job = api.start_deferred("A serene Japanese garden")              # returns a request_id at once
batch = api.wait_images(job, expected=1)                          # polls GET /v1/images/{request_id} every 5 s

clip = api.wait_video(api.start_video("Make the water crash down", image_url="https://example.com/still.png", duration=8))
print(clip.url, clip.duration)
```

**Taken from xAI's pages**

- **Images.** `POST /v1/images/generations` with model `grok-imagine-image-2.0`, `prompt`, and the optional `n` (1 to 10), `aspect_ratio`, `resolution` (`1k`, `2k`, and `1.5k` from the reference), `quality` (`low`, `medium`, `auto`, "only supported for grok-imagine-image-2.0") and `response_format` (`url` by default, or `b64_json`). The price on the overview page is $0.04 an image, and the stand-in charges 400,000,000 ticks an image to match.
- **Deferred images.** `"deferred": true` returns a `request_id`; `GET /v1/images/{request_id}` answers 202 `pending`, then 200 `done` with `data` and `usage`, or 200 `failed` with `error.code` and `error.message` ("a failed generation is a body, not an HTTP error, so pollers do not retry-storm"). Deferred requests take `response_format: url` only, and results are kept 24 hours.
- **Video.** `POST /v1/videos/generations` with model `grok-imagine-video-1.5`, `prompt`, `image` (`{"url": ...}`, a public URL or a base64 data URI), `duration` (an integer, 1 to 15, default 8), `aspect_ratio` and `resolution` (`480p`, `720p`, `1080p`) answers `{"request_id"}`. `GET /v1/videos/{request_id}` answers a `status` of `pending` (with a `progress` from 0 to 99), `done` (with `video.url`, `video.duration` and `video.respect_moderation`) or `failed`. A `respect_moderation` of false means the URL is empty, so it is a named error.
- **Polling.** The samples sleep 5 s between polls and the xAI SDK's default timeout for a video is 10 minutes. Both are the defaults here, and both are constructor arguments.

**This repo's choices (no saved page says them)**

- **A paid POST is never repeated after a dropped connection.** The server may already have processed and billed it, and there is no idempotency key on any saved page. A refused request (429 or 503) was not billed, so that one is retried. A poll is a GET and is repeated freely.
- **Every argument is checked before a request is made**, so a typo costs nothing: the lists above, `n` from 1 to 10, `duration` from 1 to 15, an image source that is an http(s) URL or a jpeg, png or webp data URI, and a `request_id` of letters, digits, hyphens and underscores only (it goes into a URL path).
- **Image URLs must be https.** The pages show no host name for the files, so the stand-in's `stand-in.invalid` host is its own choice.
- **Fewer images than asked for** are returned as they came. No saved page says whether that can happen; more images than asked for is an error.
- **Not built, though the pages describe them:** image editing and multi-image editing, video from text with reference images, voice references, video editing and extension, the Files API, webhooks and signed-URL output. Image to video is built because its contract is on the saved pages in full.

## Saved pages

Each file starts with its URL and was loaded in Chromium on 2026-11-15, then saved as text. They are verbatim, so they keep the page's own punctuation, dashes included.

| File | What the code takes from it |
|---|---|
| `xai-imagine-overview.txt` | the model names `grok-imagine-image-2.0` and `grok-imagine-video-1.5`, the $0.04 image price, up to 10 images a request, video as an asynchronous request polled with a request id |
| `xai-image-generation.txt` | the request and its options, base64 output, deferred generation and its three poll states, the 24 hour retention |
| `xai-api-reference-images.txt` | the field lists and enums, `usage.cost_in_usd_ticks` and the tick definition, the deferred poll answer and its error codes |
| `xai-video-generation.txt` | the start and poll flow, the poll statuses, duration, aspect ratios and resolutions, the sample 5 s sleep and 10 minute timeout |
| `xai-image-to-video.txt` | what counts as image to video and the three ways to give the image |
| `xai-api-reference-videos.txt` | the request and answer fields, `progress`, `respect_moderation`, the job error codes, the 503 for engine overload |
| `xai-api-reference-chat-completions.txt` | the chat body fields, `max_completion_tokens`, `refusal`, `finish_reason`, `usage` |
| `xai-chat-completions-guide.txt` | the base URL, the bearer header, the `grok-4.7` example and the answer shape |
| `xai-reasoning.txt` | `reasoning_effort` and the models that take it, "cannot be disabled", the parameters a reasoning model refuses |
| `xai-rate-limits.txt` | the 429 on an exceeded limit and the sample's exponential backoff; it names no rate limit header |
| `xai-debugging-errors.txt` | the status codes with their causes, the 202 for a pending deferred result |
| `xai-grok-4-7.txt` | the model name, the regional endpoint `https://us.api.x.ai/v1`, the reasoning levels |

## Tests

`tests/xai_standin.py` is a real HTTP server on 127.0.0.1 that checks what the saved pages say a caller must send (a bad key, an unknown field, a value outside a documented list, `stop` on a reasoning model, `quality` on another model, `deferred` with base64 output) and answers in the documented shapes, with scripted faults for 4xx, 429, 503, dropped connections, timeouts and malformed bodies. A request that fails its checks does not consume a scripted fault, so a test cannot pass by accident. Every negative test follows a positive control against the same server.

```
./.venv/Scripts/python.exe -m pytest tests/test_xai_llm.py tests/test_xai_chat_path.py tests/test_xai_imagine.py -q
./.venv/Scripts/python.exe tests/mutation_check.py xAI
```

The seeded random worlds replay many fault sequences (retries and sleeps) and many polling schedules (pending counts, timeouts, poll intervals) against an oracle written separately from the client. The mutation harness bends one guard at a time in the real source (the Bearer scheme, the retry rules, the paid-POST rule, every range and list, the request id check, the polling arithmetic) and the three files must go red.

## What a live run would confirm

The JSON body of an error, whether any header names a limit, the host of the returned file URLs, whether a pending video poll is HTTP 200 or 202, how a deferred image job behaves when it expires, and whether `grok-4.7` accepts `temperature: 0` together with `reasoning_effort: low` on an ordinary key. The client handles both pending statuses, and treats an unknown error body as unknown.
