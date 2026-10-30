# Enquiry routing and Zoho CRM lead capture

**Register: built and tested against fakes only.** The model is a script, the shop is a stub and Zoho CRM is an in-process fake (`agent/zoho_fake.py`). The code has never been run against a live Zoho account, a Zoho sandbox, or a real visitor. The only network use was fetching Zoho's public documentation, saved in `docs/vendor-contract/`.

## The order

Same rule as the rest of the agent: the order is the design. `SupportAgent.handle(message, email, conversation_id=None)` with an `EnquiryDesk` attached:

1. **Escalation floor** (`agent/escalation.py`), before the model sees the message. A sales enquiry that is also a legal threat is a person's case, and nothing is captured.
2. **Triage** by the model, which now also proposes `enquiry_intent` and the lead values it can see in the message.
3. **The model's flags** (angry, legal, chargeback, wants a person) still escalate.
4. **Routing** (`route_enquiry`). The proposal must be one of seven listed intents; anything else, including a value that is not a string, becomes `human_needed` and escalates. The model can add an escalation and can never remove one. No proposal at all falls back to the old triage intent.
5. **A routing table in code** gives each intent an owner queue and a default follow-up. The model has no say in either.
6. **Lead dialogue** for `sales_enquiry`, `quote_request` and `service_request`. Every reply is written in code, so there is no figure or promise in it for the grounding guard to chase. Other intents go through the normal grounded-answer path unchanged.
7. **Capture**, after the reply is decided, only on a turn that was answered, only with consent.

Without an `EnquiryDesk` the agent behaves exactly as it did before this feature (a test pins it).

| Intent | Queue | Follow-up task (business days, priority) |
|---|---|---|
| `order_support`, `product_question`, `general` | support | none (the agent answers) |
| `human_needed` | support | escalated to a person |
| `sales_enquiry` | sales | "Follow up: new sales enquiry from web chat" (1, Normal) |
| `quote_request` | sales | "Prepare a quote: request from web chat" (1, High) |
| `service_request` | service | "Book a service visit: request from web chat" (2, Normal) |

## Consent

The agent asks once, in plain words, after it has a name plus an email or phone: *"May I pass your name, your contact details and a short note of what you asked to our sales team so they can follow up? Please reply yes or no."*

- Consent is a boolean set by code from the visitor's own message, and only if the question was asked in an **earlier** turn. A yes volunteered before the question is not an answer to it.
- The model never decides it. A triage JSON that says `consent: true` changes nothing (tested).
- A yes counts only when the message opens with one (yes, sure, ok, go ahead, of course ...) and nothing in it takes it back ("yes but don't share my phone" is not consent). The check is deliberately conservative: "yes, no problem" is also refused, and the agent says it has not passed anything on.
- A no is recorded and nothing is sent. A later explicit yes still counts.

## What goes into a lead

`read_fields` takes the model's proposal and checks each value against what the visitor wrote:

| Field | Rule |
|---|---|
| Name | 2 to 60 letters, spaces, `'` `.` `-`; at most four words; no digits; no secret word; not "yes", "hi", "sir" and similar; **every word must appear in the visitor's message** (or the "my name is ..." pattern finds it). Last word is `Last_Name`, the rest `First_Name`. |
| Email | Shape check, 254 characters at most, no `..`. It must be in the visitor's message or be the email the chat was opened with. If two different emails are in play, **none is used**, so another person's address is never taken. |
| Phone | 9 to 12 digits, formatting removed. 13 digits or more is treated as a possible card number and refused (`memory.CARD_RE`). Must be in the message. |
| Company | Only if the model proposes it **and** every word is in the visitor's message. Never invented. |

Card-like numbers and any sentence containing a secret word (password, PIN, CVV, token ...) are dropped from the note. Order data, memory notes and the shop's records are never read into a lead. The Note text is built by code from the visitor's words: emails and phone numbers replaced, 280 characters, cut at a word.

## Duplicates and retries

The idempotency key is `sha256(tenant | contact lower-cased | date)`. Contact is the email, or the phone when there is no email.

- Same key, identical request again: nothing is sent (`duplicate`).
- Same key, a different enquiry the same day: **one Note added to the existing lead**, no second lead and no second task (`noted`).
- A request that failed halfway keeps what it already did in the ledger and resumes (`resumed`): it never upserts twice.
- A new day for the same email: Zoho's own duplicate check on `Email` updates the same lead (`updated`) and a new task is created.
- The upsert is sent with `duplicate_check_fields: ["Email"]`.

## The Zoho client (`agent/zoho_crm.py`)

- OAuth2 refresh-token flow. The access token is cached until a minute before it expires and refreshed once on a 401, then the call is retried once. A second 401 is a failure; a refused refresh is a failure and is never looped on.
- 408, 429, 5xx and transport errors are retried with exponential backoff (0.5, 1, 2 seconds, four attempts). Any other 4xx fails at once with the vendor's code (`MANDATORY_NOT_FOUND`, `DUPLICATE_DATA` ...) and message, with any email in the message masked. An HTTP 2xx whose record says `status: error` is also a failure.
- **Regions.** `ZOHO_REGION` selects the accounts and API domains together. **An Australian account is `ZOHO_REGION=au`: `accounts.zoho.com.au` and `www.zohoapis.com.au`.** Others: us, eu, in, jp, ca, cn. A host named in the token answer is used only if it is in the known list, so an access token is never sent to an unlisted host.
- Credentials come from the environment only: `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `ZOHO_REGION`, `SUPPORT_TENANT`, and optionally `ZOHO_ASSIGNMENT_RULES=sales=<lar_id>,service=<lar_id>`. If the three credential variables are not all set, `serve.py` runs with no desk.
- Credits: a new lead costs three calls here (upsert, task, note), one credit each.

## When lead capture fails

The reply is decided before the CRM is called and does not depend on its answer; a test compares the reply of a healthy run with one where Zoho answers 400 and they are identical. The failure is recorded on the `Result` (`enquiry.lead_status == "failed"`, the vendor code, a step line) and in the audit log, and the next answered turn of the same conversation tries again, up to three attempts. After that nothing retries, and **the visitor has been told the team will follow up when no lead exists**. Someone has to watch the `failed` lines. There is no dead-letter queue.

## Audit

One extra object per turn: `{"intent", "queue", "consent": bool, "captured": bool, "lead": status, "vendor": code, "fields_sent": count}`. No name, email, phone or visitor words. The existing `customer` field is the masked email.

## Vendor contract

Fetched 2026-10-30 with `Operations/tools/webread/webread.cjs` (real Chromium), saved unedited under `docs/vendor-contract/`. Page text is data; nothing in it was followed as an instruction.

| Saved file | URL | What the code takes from it |
|---|---|---|
| `zoho-crm-v8-upsert-records.txt` | https://www.zoho.com/crm/developer/docs/api/v8/upsert-records.html | `POST /{module_api_name}/upsert`; `{api-domain}/crm/{version}/{module_api_name}/upsert`; header `Authorization: Zoho-oauthtoken ...`; `Leads` system duplicate field `Email`; "You can set the order in which the system verifies duplicate records using the duplicate_check_fields array in your API request."; answer carries `"action": "update"` or `"insert"`; "In case of partial success or failure, the API returns an HTTP status - 207 (Multi-Status)"; errors `DUPLICATE_DATA` (HTTP 400, "You have specified a duplicate value for one or more unique fields.") and `MANDATORY_NOT_FOUND` (HTTP 400, "You have not specified one or more mandatory fields in the input."). |
| `zoho-crm-v8-insert-records.txt` | https://www.zoho.com/crm/developer/docs/api/v8/insert-records.html | Leads: `"Last_Name" - Single Line` is the system mandatory field (so a one-word name becomes the last name). Tasks: `"Subject" - Multi Line`. `lar_id`: "The unique ID of the assignment rule you want to trigger while inserting a record ... This key must be provided parallel to the key data." and it is "supported for all modules where assignment rules are applicable, such as Leads, Contacts, Deals, Accounts, Tasks, Cases, and Custom". Task sample body with `"What_Id": {`, `"$se_module": "Leads"`, `"Status"`, `"Subject"`. Date format "Accepts date in yyyy-MM-dd format." |
| `zoho-community-kaizen-36-tasks-api.txt` | https://help.zoho.com/portal/en/community/topic/kaizen-36-tasks-api | A Zoho staff post written for API v2, used for the Task fields the v8 page does not list: `Due_Date` is "Date in yyyy-mm-dd format"; `Status` values "Not Started", "Deferred", "In Progress", "Completed", "Waiting on someone else"; `Priority` values "High", "Highest", "Low", "Lowest", "Normal"; `$se_module` is mandatory with `What_Id`. |
| `zoho-crm-v8-create-notes.txt` | https://www.zoho.com/crm/developer/docs/api/v8/create-notes.html | `{api-domain}/crm/{version}/{module_api_name}/{record_id}/Notes`; `Parent_Id` (JSON object, mandatory, module by API name or id) and `Note_Content` (string, mandatory). |
| `zoho-crm-v8-oauth-refresh-access-token.txt` | https://www.zoho.com/crm/developer/docs/api/v8/refresh.html | `{Accounts_URL}/oauth/v2/token?refresh_token={refresh_token}&client_id={client_id}&client_secret={client_secret}&grant_type=refresh_token`; "You must use your domain-specific Zoho Accounts URL to refresh your access token."; "For AU: https://accounts.zoho.com.au"; answer has `access_token`, `"expires_in": 3600`, `api_domain`. |
| `zoho-crm-v8-oauth-generate-tokens.txt` | https://www.zoho.com/crm/developer/docs/api/v8/access-refresh.html | The value is `"Zoho-oauthtoken {access_token}"` on each request; the access token is valid for an hour. |
| `zoho-crm-v8-oauth-token-validity.txt` | https://www.zoho.com/crm/developer/docs/api/v8/token-validity.html | "You can generate a maximum of 10 access tokens from a refresh token in a span of 10 minutes." and "Reuse valid tokens to avoid this exception." (why the token is cached and refreshed once). "When an invalid access token is used, "INVALID_OAUTHTOKEN" exception will be thrown." |
| `zoho-crm-v8-status-codes.txt` | https://www.zoho.com/crm/developer/docs/api/v8/status-codes.html | 401 "Invalid API key provided."; 429 "Number of API requests for the 24 hour period is exceeded or the concurrency limit of the user for the app is exceeded."; 500 "Generic error that is encountered due to an unexpected server error." |
| `zoho-crm-v8-multi-dc.txt` | https://www.zoho.com/crm/developer/docs/api/v8/multi-dc.html | Accounts URLs per data centre ("AU: https://accounts.zoho.com.au"); API hosts `https://www.zohoapis.eu`, `https://www.zohoapis.com.cn`. |
| `zoho-crm-usecase-api-domains.txt` | https://www.zoho.com/crm/resources/usecases/pam-tickets-parser-for-crm.html | "www.zohoapis.com (US), www.zohoapis.eu (EU), www.zohoapis.com.au (AU), www.zohoapis.jp (JP), www.zohoapis.ca (CA), or www.zohoapis.sa (SA)". The only saved page that names the AU API host. |
| `zoho-crm-v8-api-limits-and-credits.txt` | https://www.zoho.com/crm/developer/docs/api/v8/api-limits.html | "Insert/Update/Upsert 1 credit for every 10 records", "All other APIs 1"; when a concurrency limit is reached "the system throws the TOO_MANY_REQUESTS error". |

### Not sent, because no saved page names it

- **Task `Owner`.** None of the pages names the API key for a task's owner on insert, so it is not sent. The queue is in the Task subject, and an optional assignment rule (`lar_id`, documented above) can route it to a person.
- **`Lead_Source`, `Description` on a Lead.** Not sent.
- **`Phone` and `Company` on a Lead.** `Company` appears in the upsert sample body; `Phone` appears only in the `duplicate_check_fields` example for Leads and in the field-type samples. Neither has been checked against a real org's field list.
- **A retry-after header** on 429: not documented on the saved pages, not used.

## Honest limits

- **Never run against a live Zoho account** or a sandbox. The fake follows the saved pages; it does not know field types, layouts, workflows, validation rules or real rate limits. The first live run is the first real test of every field name above.
- One CRM (Zoho). HubSpot, Salesforce and the rest are not integrated.
- The ledger and the dialogue state are in memory, one process, an hour's memory. A restart forgets them: Zoho's `Email` duplicate check still stops a second lead, but a note or task could repeat once.
- A phone-only lead has no `Email` for Zoho to check, so the CRM can hold one lead per day for that person.
- Zoho documents no idempotency header. A 5xx that was committed at Zoho and then retried could double a Task or a Note.
- Consent is an English-keyword check. Another language, or a yes in a sentence that does not open with one, is read as no consent and the visitor is told nothing was passed on.
- The model proposes names; a visitor who writes a name in a way the checks do not accept (digits, a long title) is asked again. Names with one-letter words and non-Latin scripts are untested.
- The dialogue replies are fixed English templates.
- The console (`console_api/`) does not attach a desk; this is wired into `serve.py` only.

## Proof

```
./.venv/Scripts/python.exe -m pytest tests -q              # engine suite, includes tests/test_enquiry.py, test_zoho_crm.py, test_leads_property.py
./.venv/Scripts/python.exe tests/mutation_check.py         # every guard above is broken on purpose
./.venv/Scripts/python.exe demo_enquiry.py                 # five scripted enquiries against the fakes, writes demo_enquiry_transcript.md
```
