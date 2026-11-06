# IBM watsonx.ai vendor pages

*The IBM pages the watsonx.ai provider was written against, saved as text on 2026-11-06 from a real Chromium load.*

The stand-in server in `tests/watsonx_standin.py` and the client in `agent/watsonx_llm.py` follow these files, not memory. Each file starts with its URL. Quotes in the code and in [../../watsonx.md](../../watsonx.md) are from these files.

| File | What the client takes from it |
|---|---|
| `chat-api.txt` | `POST https://<region>.<cloud-provider-domain>/ml/v1/text/chat?version=<date>` with `Authorization: Bearer`; the body fields `model_id`, `project_id`, `messages` (role in lower case: "The role parameter used in the chat session messages is case sensitive"), `max_tokens` and the reasoning example's `max_completion_tokens`; the answer's `choices[].message.content`, `reasoning_content`, `finish_reason`, `usage`; the version dates 2024-10-08, 2024-10-09 and 2025-10-25; the model ids `ibm/granite-4-h-small` (named in the notebook list), `openai/gpt-oss-120b` and `meta-llama/llama-3-8b-instruct`; the gateway time-out troubleshooting note |
| `credentials-api-key-iam-token.txt` | the token request: `POST https://iam.cloud.ibm.com/identity/token`, `Content-Type: application/x-www-form-urlencoded`, `grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey=...`; "Retrieve the IAM bearer token from the access_token field"; "You are responsible for refreshing the token when it expires" |
| `authenticating-for-programmatic-access.txt` | "IAM tokens are temporary security credentials that are valid for 60 minutes"; the same token request with `Accept: application/json`; the token is prefixed with `Bearer` |
| `java-sdk-authentication.txt` | the SDK's own token handling: fetch on first use, cache, "checks expiry before each subsequent request, and refreshes silently when needed"; on "401 authentication_token_expired" it "refreshes token and retries (once by default)" |
| `java-sdk-error-handling.txt` | the error codes with their statuses (`authentication_token_expired` 401, `authorization_rejected` 403, `invalid_input_argument` 400, `invalid_request_entity` 400, `json_type_error` 400, `json_validation_error` 400, `model_not_supported` 400, `model_no_support_for_function` 400, `token_quota_reached` 429, `user_authorization_failed` 403); the finish reasons LENGTH, TIME_LIMIT, CANCELLED, ERROR; "The SDK automatically retries 429, 502, 503, 504, and 520 responses with exponential backoff" |
| `java-sdk-retry-settings.txt` | the SDK's defaults: one retry on an expired token, ten on a transient status, exponential backoff from a 20 ms base |
| `python-sdk-rate-limits.txt` | "If the limit is reached, an HTTP 429 Too Many Requests error response is returned. The response header will also contain information about the limit" (the header is not named); the Python SDK retries `[429, 503, 504, 520]`, 10 times, backoff `min(0.5 * 2^attempt, 8)` |
| `node-sdk-api-error-response.txt`, `node-sdk-api-error.txt` | the error body: `{"errors": [ApiError], "trace": string}` and `ApiError = {code, message, more_info?, target?}` |

These files are verbatim, so they keep the page's own punctuation, dashes included.

## Pages that could not be saved

- **The watsonx.ai API reference** (`https://cloud.ibm.com/apidocs/watsonx-ai`) and **IBM Cloud's IAM page** (`https://cloud.ibm.com/docs/iam?topic=iam-iamtoken_from_apikey`) would not load in Chromium here (`net::ERR_HTTP2_PROTOCOL_ERROR`). The API reference was read through a summarising fetch instead, which is not a verbatim page, so nothing in the client is quoted from it. What that read said: `POST /ml/v1/text/chat` takes `model_id` (or the alias `model`), one of `project_id` and `space_id`, `messages`, `tools`, `tool_choice_option`, `max_completion_tokens` ("preferred"; `max_tokens` is "deprecated"), `temperature`, `time_limit` in milliseconds, among others; the response has `choices`, `usage` and `system`. The IAM answer's `expires_in` and `expiration` fields come from a search result quoting that IAM page (a 3600 second sample with `expiration` as an epoch time and `refresh_token` set to `not_supported`).
- **IBM's quick start and tool-calling pages** redirected to a product catalogue, so the tool-call answer shape (`tool_calls` with `id`, `type`, `function.name`, `arguments`; `finish_reason` `tool_calls`; a `tool` role message with a `tool_call_id` for the result) is known only from a search result quoting them. The agent sends no tools, so the client does not depend on it.

## What the pages do not say

These choices come from the client's own design, so they are named here and are not claims about the saved pages.

- **The Dallas host.** `https://us-south.ml.cloud.ibm.com` is from the brief for this work. The saved pages show `https://<region>.<cloud-provider-domain>` and the Java SDK's `CloudRegion.DALLAS` but not the host name.
- **The default model id.** `ibm/granite-4-h-small` is named on the chat page only as the model of a sample notebook. Whether it is enabled for a given account and region is not on any saved page; `WATSONX_MODEL_ID` changes it.
- **The version date.** `2025-10-25` is the date the chat page uses for its reasoning-model example; the other two dates on the page are older. Which dates the service accepts is not stated on any saved page.
- **`Retry-After` on a 429.** The pages say the response header carries "information about the limit" and name no header. The client honours `Retry-After` as a number of seconds when the header is there and falls back to its own backoff when it is not.
- **Whether `token_quota_reached` is permanent.** The pages give the code and the status (429) and say nothing about waiting it out. The client treats it as not worth retrying within a shopper's wait.
- **The error shape of the token endpoint.** The `errorCode` and `errorMessage` fields the client reads from a refused token request (for example `BXNIM0415E`) are from memory of IBM's IAM service and are in no saved page. A body in the `errors` shape is read as well.
- **Backoff base and cap, and the retry count.** The SDKs retry up to 10 times from a 20 ms or 0.5 s base. A shopper is waiting, so this client retries twice, waits 1 s then 2 s, and refuses a `Retry-After` over 30 s. Those are this repo's choices.
- **The 5xx statuses.** The SDKs retry 429, 502, 503, 504 and 520. The client retries every 5xx.
- **When to renew the token.** The pages say a token lasts 60 minutes and that the SDK checks expiry before each call. The client renews 120 s early, which is this repo's choice.
- **`time_limit`.** The chat page says a gateway time-out means the request took too long and to raise `time_limit`. The client does not send it; it gives up after its own 60 s timeout.

Edit to a saved page (2026-11-06): in authenticating-for-programmatic-access.txt lines 81, 115 and 129 IBM's sample API key value was replaced by `example-key-value`, because the push guard reads a labelled `apikey = "..."` as a credential. Nothing else in the saved pages was changed.
