"""Break each guard on purpose and prove the suite goes red. A guard whose removal changes nothing is decoration.

Every mutant is applied to the real source, the suite is run, and the file is restored byte-exact
(verified, not assumed). A mutant that leaves the suite green is a missing test, and fails this run.
A mutant that HANGS the suite counts as caught too, but is reported separately because it is a
different failure mode from red.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MUTANTS = [
    # (file, original, replacement, what removing it would let through)
    ("agent/guard.py", "for fig in sorted(figures(reply) - allowed):", "for fig in sorted(set()):",
     "an invented figure reaches the shopper"),
    ("agent/guard.py", 'm = PROMISE_RE.search(reply.replace("’", "\'").replace("‘", "\'").replace("ʼ", "\'"))', "m = None",
     "a refund or discount promise reaches the shopper"),
    ("agent/guard.py", "    for m in WORD_FIGURE_RE.finditer(text):", "    for m in ():",
     "'five business days' passes when the facts say 3 to 7 (a number written as a word)"),
    ("agent/shopify.py", "!= email.strip().lower():", "== '\\x00':",
     "anyone can read any order by number (identity check removed)"),
    ("agent/shopify.py", "if body.get(\"errors\"):", "if False:",
     "a throttle delivered as HTTP 200 reads as 'no such order'"),
    ("agent/escalation.py", "lawyer|attorney|sue|lawsuit|legal action|solicitor|court", "zzzzqqqq",
     "a legal threat gets an automated reply"),
    ("agent/core.py", "        if check.ok:", "        if True:",
     "an ungrounded reply is sent instead of retried or escalated"),
    ("agent/core.py", "return self._escalate(res, \"store lookup failed\")", "return res",
     "a store outage produces a blank reply instead of a person"),
    ("agent/shopify.py", "\"status:active %s\" % text", "\"%s\" % text",
     "draft and archived products are offered to shoppers"),
    ("agent/core.py", "if order.fulfillment_status == \"unfulfilled\":", "if False:",
     "an unshipped order's customer is never told it can still be cancelled"),
    ("agent/policy.py", "for suffix in (\"ing\", \"ed\", \"es\", \"s\"):", "for suffix in ():",
     "'return' no longer meets 'Returns' and the wrong policy is retrieved"),
    # ---- customer memory -------------------------------------------------------------------------------------
    ("agent/core.py", "notes = format_hints(memories)", "notes = \"\"",
     "what the agent remembers never reaches the answer prompt"),
    ("agent/core.py", "check = verify_grounded(reply, facts_text, allowed_extra=allowed_extra)",
     "check = verify_grounded(reply, facts_text + \"\\n\" + notes, allowed_extra=allowed_extra)",
     "a figure that exists only in a remembered note is allowed into a reply (memory becomes a source of facts)"),
    ("agent/core.py", "number = last_order_number(memories) or \"\"", "number = \"\"",
     "the remembered order pointer is never used, so a returning customer is asked for the number again"),
    ("agent/core.py", "if order is None and from_memory:", "if False:",
     "a remembered order that no longer exists is reported as 'not found' instead of asking for the number"),
    ("agent/core.py", "                self._remember(email, message, res)\n", "                pass\n",
     "nothing is ever remembered after a reply"),
    ("agent/core.py", "        except Exception as e:  # memory is a convenience: its outage must never block an answer",
     "        except KeyError as e:  # memory is a convenience: its outage must never block an answer",
     "a memory outage on recall costs the customer their answer"),
    ("agent/core.py", "        except Exception as e:\n            res.steps.append(\"memory not saved: %s\" % type(e).__name__)",
     "        except KeyError as e:\n            res.steps.append(\"memory not saved: %s\" % type(e).__name__)",
     "a memory outage on write costs the customer their answer"),
    ("agent/memory.py", "    if not grounded_in(body, message):", "    if False:",
     "the model can store words the customer never wrote"),
    ("agent/memory.py", "if CARD_RE.search(body) or SECRET_RE.search(body) or SECRET_RE.search(key):", "if False:",
     "a card number or a password is stored as a customer note"),
    ("agent/memory.py", "if kind not in EXTRACTABLE_KINDS or", "if kind not in KINDS or",
     "the model can write episodes and live pointers, which only code may write"),
    ("agent/memory.py", "\"fact\": 60,", "\"fact\": 6000,",
     "a customer fact never goes stale"),
    ("agent/memory.py", "return days, (limit is not None and days > limit)", "return days, False",
     "no note is ever marked stale"),
    ("agent/memory.py", "(m.body or \"\").replace('\"', \"'\")", "(m.body or \"\")",
     "a note can close its own quotation in the prompt"),
    ("agent/memory.py", "MAX_PER_TURN = 3", "MAX_PER_TURN = 30",
     "one message can write an unbounded number of notes"),
    # ---- enquiry routing and lead capture -----------------------------------------------------------------------
    ("agent/core.py", "        hard = decide(message, {})", "        hard = None",
     "floor first: the model sees a legal threat before the code does"),
    ("agent/core.py", "        reason = decide(message, triage)\n        if reason:", "        reason = None\n        if reason:",
     "the model's angry flag no longer escalates a sales enquiry (the model's vote is lost)"),
    ("agent/core.py", "            if routing.intent == \"human_needed\":", "            if False:",
     "the model's human_needed proposal no longer adds an escalation"),
    ("agent/enquiry.py", "        intent, step = \"human_needed\", \"routing: the model", "        intent, step = \"general\", \"routing: the model",
     "a value the model made up is routed as a normal enquiry instead of going to a person"),
    ("agent/enquiry.py", "        asked_before = state.consent_asked", "        asked_before = True",
     "a yes volunteered before the question counts as consent"),
    ("agent/enquiry.py", "    return bool(YES_RE.match(message)) and not NEG_RE.search(message)", "    return bool(YES_RE.match(message))",
     "'yes but don't share my phone' counts as consent"),
    ("agent/enquiry.py", "state.intent in LEAD_INTENTS and state.consent is True and not state.captured", "state.intent in LEAD_INTENTS and not state.captured",
     "a lead is captured with no consent at all"),
    ("agent/enquiry.py", "            state.consent_asked = True\n            reply = ask_consent(state, team)", "            state.consent_asked = False\n            reply = ask_consent(state, team)",
     "the consent question is asked on every turn instead of once, and a yes can never follow it"),
    ("agent/enquiry.py", "and state.attempts < self.max_attempts", "",
     "a CRM that keeps refusing is retried on every later turn without limit"),
    ("agent/core.py", "        if not self._enquiry or res.action != \"replied\":", "        if not self._enquiry:",
     "a failed capture is retried on an escalated turn (a person's case triggers a CRM write)"),
    ("agent/core.py", "        except Exception as e:  # the lead is a convenience for the team: whatever goes wrong, the reply is already final",
     "        except KeyError as e:  # the lead is a convenience for the team: whatever goes wrong, the reply is already final",
     "a bug in the capture step itself breaks the visitor's reply"),
    ("agent/core.py", "        state.captured = outcome.status != \"failed\"", "        state.captured = True",
     "a lead that failed to reach the CRM is marked done and never retried"),
    ("agent/enquiry.py", "    return bool(EMAIL_OK.match(value)) and len(value) <= 254 and \"..\" not in value", "    return \"@\" in value",
     "an invalid email address is accepted as contact"),
    ("agent/enquiry.py", "    if not _words(name) <= _words(said):", "    if False:",
     "the model can hand us a name the visitor never wrote"),
    ("agent/enquiry.py", "    if not {w.lower() for w in re.findall(r\"[^\\W_]+\", company)} <= {w.lower() for w in re.findall(r\"[^\\W_]+\", said)}:", "    if False:",
     "a company name is invented"),
    ("agent/enquiry.py", "    if len(emails) == 1:", "    if emails:",
     "another person's email in the message is taken as the visitor's"),
    ("agent/enquiry.py", "MIN_PHONE_DIGITS, MAX_PHONE_DIGITS = 9, 12", "MIN_PHONE_DIGITS, MAX_PHONE_DIGITS = 9, 19",
     "a 16 digit card number passes as a phone number"),
    ("agent/enquiry.py", "    text = CARD_RE.sub(\"[number removed]\", text)", "    text = text",
     "a card-like number is copied into the CRM note"),
    ("agent/enquiry.py", "if s and not SECRET_RE.search(s)]", "if s]",
     "a sentence with a password or PIN is copied into the CRM note"),
    ("agent/enquiry.py", "        if d.weekday() < 5:", "        if True:",
     "a follow-up task falls due on a Saturday"),
    ("agent/leads.py", "                if not entry.lead_id:", "                if True:",
     "a retried request upserts again"),
    ("agent/leads.py", "                if route.task_subject and not entry.task_id:", "                if route.task_subject:",
     "a repeat enquiry creates a second follow-up task"),
    ("agent/leads.py", "                if note_key not in entry.notes:", "                if True:",
     "a retried request adds the same note twice"),
    ("agent/leads.py", "contact.strip().lower(), day.isoformat()", "contact.strip().lower(), \"\"",
     "the idempotency key ignores the day, so the same person is never followed up again"),
    ("agent/leads.py", "(tenant, contact.strip().lower()", "(\"\", contact.strip().lower()",
     "two stores share one idempotency key, so one store's lead hides another's"),
    ("agent/leads.py", "(\"%s|%s|%s\" % (tenant, contact.strip().lower()", "(\"%s|%s|%s\" % (tenant, contact.strip()",
     "Jane@x.com and jane@x.com are two leads"),
    ("agent/leads.py", "            except ZohoError as e:", "            except KeyError as e:",
     "a CRM refusal escapes capture and breaks the visitor's reply"),
    ("agent/leads.py", "            except Exception as e:  # lead capture is a convenience", "            except KeyError as e:  # lead capture is a convenience",
     "an unexpected CRM bug escapes capture and breaks the visitor's reply"),
    ("agent/leads.py", "dup = [\"Email\"] if \"Email\" in record else []", "dup = []",
     "the upsert no longer names Email as the duplicate check"),
    # ---- the Zoho client -------------------------------------------------------------------------------------
    ("agent/zoho_crm.py", "            if status == 401 and not refreshed:", "            if False:",
     "an expired access token fails the call instead of refreshing once"),
    ("agent/zoho_crm.py", "            if status == 401 and not refreshed:", "            if status == 401:",
     "a token that stays rejected is refreshed over and over (Zoho allows 10 per 10 minutes)"),
    ("agent/zoho_crm.py", "            if status in RETRY_STATUSES or 500 <= status < 600:", "            if 500 <= status < 600:",
     "a 429 or 408 is a failure instead of a retry"),
    ("agent/zoho_crm.py", "RETRY_STATUSES = frozenset({408, 429})", "RETRY_STATUSES = frozenset({400, 408, 429})",
     "a 4xx that will never succeed is retried"),
    ("agent/zoho_crm.py", "            if self._token and self._clock() < self._expires_at:", "            if False:",
     "the access token is not cached, so every call refreshes it"),
    ("agent/zoho_crm.py", "TOKEN_SKEW_SECONDS = 60", "TOKEN_SKEW_SECONDS = 0",
     "the token is used up to the second it expires"),
    ("agent/zoho_crm.py", "        if host in KNOWN_API_DOMAINS:", "        if host:",
     "a host named in a token answer is trusted with the access token"),
    ("agent/zoho_crm.py", "\"Authorization\": \"Zoho-oauthtoken \" + token", "\"Authorization\": \"Bearer \" + token",
     "the wrong Authorization scheme is sent"),
    ("agent/zoho_crm.py", "        if first.get(\"status\") != \"success\" or first.get(\"code\") != \"SUCCESS\":", "        if False:",
     "a record-level error inside an HTTP 207 is read as success"),
    ("agent/zoho_crm.py", "    s = _EMAIL_IN_TEXT.sub(\"[email]\", str(text))", "    s = str(text)",
     "a vendor message that echoes an email goes into the audit in clear"),
    ("agent/zoho_crm.py", "    \"au\": (\"https://accounts.zoho.com.au\", \"https://www.zohoapis.com.au\"),", "    \"au\": (\"https://accounts.zoho.com\", \"https://www.zohoapis.com\"),",
     "an Australian account is sent to the US data centre"),
    # ---- the chat widget server: origins, tenants, signed ids, limits, size caps, text only ------------------------------
    ("agent/chat_api.py", "            if not self.registry.origin_known(origin):\n                return self._answer(403, {\"error\": \"origin_not_allowed\"})\n            ok, retry = self.ip_limit.hit(",
     "            if False:\n                return self._answer(403, {\"error\": \"origin_not_allowed\"})\n            ok, retry = self.ip_limit.hit(",
     "a page on any website can post to the chat (the origin check on the message route removed)"),
    ("agent/chat_api.py", "            if origin not in tenant.allowed_origins:\n                return self._answer(403, {\"error\": \"origin_not_allowed\"}, origin)\n            if req.conversation_id is None:",
     "            if False:\n                return self._answer(403, {\"error\": \"origin_not_allowed\"}, origin)\n            if req.conversation_id is None:",
     "site A's page can talk to site B's chat by quoting B's public key"),
    ("agent/chat_api.py", "            if origin not in tenant.allowed_origins:\n                return self._answer(403, {\"error\": \"origin_not_allowed\"}, origin)\n            return self._answer(200, tenant.public(), origin)",
     "            if False:\n                return self._answer(403, {\"error\": \"origin_not_allowed\"}, origin)\n            return self._answer(200, tenant.public(), origin)",
     "site A's page can read site B's theme and greeting by quoting B's public key"),
    ("agent/chat_api.py", "            tenant = self.registry.by_site_key(req.site)", "            tenant = self.registry.tenants[0]",
     "every visitor lands on the first tenant whatever site key the page sent"),
    ("agent/chat_api.py", "            conversation = self.signer.verify(token, tenant.id)", "            conversation = token",
     "a visitor can attach to another conversation by sending any id"),
    ("agent/chat_guard.py", "        if not hmac.compare_digest(sig.encode(\"utf-8\"), self._sign(\".\".join(parts[:3])).encode(\"utf-8\")):", "        if False:",
     "a conversation id the visitor made up or edited is accepted (signature unchecked)"),
    ("agent/chat_guard.py", "        if tenant != tenant_id or not issued.isdigit()", "        if not issued.isdigit()",
     "an id issued for one site is accepted on another"),
    ("agent/chat_guard.py", "or self._now() - int(issued) > self._max_age:", "or False:",
     "a conversation id never expires"),
    ("agent/chat_guard.py", "        if len(q) >= self.limit:", "        if False:",
     "a visitor can send messages without limit"),
    ("agent/chat_api.py", "            ok, retry = self.ip_limit.hit(\"ip:\" + self._ip(request))", "            ok, retry = True, 0",
     "one address can flood the chat endpoint (per-IP limit removed)"),
    ("agent/chat_api.py", "            ok, retry = self.conversation_limit.hit(\"%s:%s\" % (tenant.id, conversation))", "            ok, retry = True, 0",
     "one conversation can flood the model (per-conversation limit removed)"),
    ("agent/chat_guard.py", "    if trust_proxy and forwarded_for:", "    if forwarded_for:",
     "a visitor dodges the per-IP limit by sending a forged X-Forwarded-For"),
    ("agent/chat_guard.py", "        last = forwarded_for.split(\",\")[-1].strip()", "        last = forwarded_for.split(\",\")[0].strip()",
     "behind a proxy the visitor's own forged entry is taken instead of the one the proxy wrote"),
    ("agent/chat_guard.py", "        if len(out) > limit:", "        if False:",
     "a chunked body of any size is read into memory"),
    ("agent/chat_api.py", "            if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:", "            if False:",
     "a body that declares itself huge is read before it is refused"),
    ("agent/chat_api.py", "MAX_MESSAGE_CHARS = 1000", "MAX_MESSAGE_CHARS = 10 ** 7",
     "a message of any length reaches the model"),
    ("agent/chat_api.py", "    model_config = ConfigDict(extra=\"forbid\")", "    model_config = ConfigDict(extra=\"ignore\")",
     "a request with fields the API does not have is accepted"),
    ("agent/chat_guard.py", "    return _CONTROL.sub(\"\", str(value))[:limit]", "    return str(value)[:limit]",
     "control characters in a reply reach the visitor's screen"),
    ("agent/chat_guard.py", "    return _CONTROL.sub(\"\", str(value))[:limit]", "    return _CONTROL.sub(\"\", str(value))",
     "a reply of any length reaches the visitor's screen"),
    ("agent/chat_api.py", "headers.update({\"Access-Control-Allow-Origin\": origin, \"Vary\": \"Origin\"})", "headers.update({\"Access-Control-Allow-Origin\": \"*\", \"Vary\": \"Origin\"})",
     "any website can read the chat's answers (CORS opened to every origin)"),
    ("agent/chat_api.py", "            awaiting = bool(result.enquiry and result.enquiry.awaiting_consent)", "            awaiting = False",
     "the yes and no buttons never appear, so the consent question has no button answer"),
    ("agent/leads.py", "        route = self._routes[intent]", "        route = ROUTES[intent]",
     "a lead for site B lands in the default owner queue instead of site B's own"),
    ("agent/enquiry.py", "        team = team_word(self.routes[intent].queue)", "        team = team_word(ROUTES[intent].queue)",
     "the visitor of site B is told the default team will call, not site B's"),
    ("agent/tenants.py", "    return Tenant(tid, key, _text(theme, \"name\", 2, 40, where), initials, theme[\"primary\"], theme[\"on_primary\"],", "    return Tenant(tid, key, _text(theme, \"name\", 2, 40, where), initials, \"#000000\", theme[\"on_primary\"],",
     "a tenant's own colour is ignored"),
    ("agent/tenants.py", "        n = normalise_origin(o)\n        if not n:", "        n = str(o).strip().lower()\n        if not n:",
     "a wildcard or a path is accepted as an allowed origin"),
    ("widget/widget.js", "      m.appendChild(document.createTextNode(String(text))); // text node: markup in a message is shown, never run",
     "      m.insertAdjacentHTML(\"beforeend\", String(text)); // BROKEN ON PURPOSE",
     "markup in a reply or in the visitor's own words runs inside the page"),
    ("widget/widget.js", "yes.addEventListener(\"click\", function () { submit(YES_TEXT, false, true); });",
     "yes.addEventListener(\"click\", function () { submit(NO_TEXT, false, true); });",
     "the yes button sends a no"),
    # ---- the Zoho MCP tools: strict input, confirm, idempotency, small answers, text from the CRM is data -----------------------
    ("agent/mcp_tools.py", "        if not allowed:\n            return ToolResult(True, {\"error\": \"slow_down\"", "        if False:\n            return ToolResult(True, {\"error\": \"slow_down\"",
     "a client can call the tools as fast as it likes (the spec says a server MUST rate limit tool invocations)"),
    ("agent/mcp_tools.py", "        if size > MAX_ARGUMENT_CHARS:", "        if False:",
     "an argument object of any size is parsed and validated"),
    ("agent/mcp_tools.py", "        if args[\"confirm\"] is not True:", "        if False:",
     "a write with confirm false is executed instead of previewed"),
    ("agent/mcp_tools.py", "[\"lead_id\", \"content\", \"confirm\", \"idempotency_key\"]", "[\"lead_id\", \"content\", \"idempotency_key\"]",
     "a note is written with no confirm flag at all"),
    ("agent/mcp_tools.py", "                if seen[0] != fingerprint:", "                if False:",
     "an idempotency key reused for a different call returns the first call's answer"),
    ("agent/mcp_tools.py", "            answer = self._finish(tool.handler(args))\n", "            self._store.put(key, fingerprint, {})\n            answer = self._finish(tool.handler(args))\n",
     "a key is remembered before the write has succeeded, so the retry of a failed write never runs"),
    ("agent/mcp_tools.py", "\"required\": required, \"additionalProperties\": False, **extra}", "\"required\": required, \"additionalProperties\": True, **extra}",
     "an extra argument such as a token is accepted and ignored"),
    ("agent/mcp_tools.py", "\"lead_status\": _clip(rec.get(\"Lead_Status\"))}", "\"lead_status\": _clip(rec.get(\"Lead_Status\")), \"owner\": rec.get(\"Owner\")}",
     "a lead comes back with more fields than the tool needs"),
    ("agent/mcp_tools.py", "    return plain_text(value, limit).strip() or None", "    return str(value)[:limit]",
     "control characters from the CRM reach the caller"),
    ("agent/mcp_tools.py", "[-MAX_NOTES_RETURNED:]", "[:]",
     "every note of a lead comes back, however many"),
    ("agent/mcp_tools.py", "_clip(r.get(\"Note_Content\"), NOTE_CHARS)", "_clip(r.get(\"Note_Content\"), 5000)",
     "a note of any length comes back whole"),
    ("agent/mcp_tools.py", "        if len(result.text) <= self._max_output:\n            return result\n        trimmed", "        if True:\n            return result\n        trimmed",
     "the answer is not capped"),
    ("agent/mcp_tools.py", "        if CARD_RE.search(a[\"content\"]) or SECRET_RE.search(a[\"content\"]):", "        if False:",
     "a card number or a password is written into a CRM note"),
    ("agent/mcp_tools.py", "        due = add_business_days(self._today(), a[\"due_in_business_days\"]).isoformat()", "        due = self._today().isoformat()",
     "a follow-up task is due today whatever was asked"),
    ("agent/mcp_tools.py", "consent=\"given by %s\" % a[\"consent\"][\"method\"].replace(\"_\", \" \"))", "consent=None)",
     "a lead is created and the consent is not recorded anywhere"),
    ("agent/mcp_tools.py", "_object({\"given\": {\"const\": True},", "_object({\"given\": {\"type\": \"boolean\"},",
     "a lead is created with consent given false"),
    ("agent/mcp_tools.py", "            if not valid_email(a[\"email\"].strip()):", "            if False:",
     "an invalid email address is sent to the CRM"),
    ("agent/mcp_tools.py", "            if not phone:", "            if False:",
     "an invalid phone number is sent to the CRM"),
    ("agent/mcp_tools.py", "\"data_notice\": DATA_NOTICE}", "\"x\": 0}",
     "text from the CRM comes back with no notice that it is data"),
    ("agent/mcp_tools.py", "        except Exception:  # whatever it was, the caller gets no detail\n            return _error(\"internal_error\", \"The tool failed.\")",
     "        except Exception as e:\n            return _error(\"internal_error\", str(e))",
     "an internal error's text reaches the caller"),
    ("agent/mcp_tools.py", "if t - at > self._ttl]:", "if False]:",
     "an idempotency key is remembered forever"),
    ("agent/mcp_tools.py", "        if len(self._items) >= self._max:", "        if False:",
     "the idempotency memory grows without bound"),
    ("agent/mcp_tools.py", "        if outcome.status == \"failed\":", "        if False:",
     "a lead the CRM refused is reported as created"),
    ("agent/mcp_tools.py", "\"queue\": {\"type\": \"string\", \"enum\": self._queues}", "\"queue\": {\"type\": \"string\", \"maxLength\": 32}",
     "a task can be sent to a queue the tenant does not have"),
    ("agent/zoho_crm.py", "        if r.status_code == 204:\n            return []", "        if False:\n            return []",
     "a search with no match is an error instead of an empty answer"),
    ("agent/zoho_crm.py", "\"/Leads/search\", None, {\"email\": email}", "\"/Leads/search\", None, {\"word\": email}",
     "the lookup searches by a word instead of the email address"),
    ("agent/zoho_crm.py", "{\"fields\": \"Note_Content,Created_Time\", \"per_page\": \"200\"}", "{\"per_page\": \"200\"}",
     "the notes read leaves out the mandatory fields parameter"),
    ("agent/zoho_crm.py", "                return (reader or self._first_record)(r)", "                return self._first_record(r)",
     "a read is judged by the write rules (a record with no status is an error)"),
]


# The two slow files (a real Chromium, a real child process over stdio) run only for a mutant the fast tests did not catch.
SLOW = ["tests/test_widget_browser.py", "tests/test_mcp_stdio.py"]


def run_pytest(args: list[str], retries: int = 2) -> tuple[str, str]:
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", *args, "-q", "-x", "-rf", "-p", "no:cacheprovider"],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=180)
    except subprocess.TimeoutExpired:
        return "hang", ""
    if p.returncode != 0 and "WinError 10055" in p.stdout and retries:
        return run_pytest(args, retries - 1)  # Windows ran out of socket buffers (many test runs in a row): not the mutant's doing, so not a catch
    return ("green" if p.returncode == 0 else "red"), p.stdout[-600:]


def run_suite() -> tuple[str, str]:
    outcome, out = run_pytest(["tests", *["--ignore=" + f for f in SLOW]])
    if outcome != "green":
        return outcome, out
    return run_pytest(SLOW)


def main() -> int:
    baseline, out = run_suite()
    if baseline != "green":
        print("BASELINE IS NOT GREEN, mutation results would mean nothing\n" + out)
        return 2
    survivors, ran = [], 0
    only = sys.argv[1] if len(sys.argv) > 1 else ""  # an optional filter: only mutants whose file or consequence contains this text
    for rel, old, new, consequence in MUTANTS:
        if only and only not in rel and only not in consequence:
            continue
        ran += 1
        path = ROOT / rel
        original = path.read_bytes()
        text = original.decode("utf-8")
        if text.count(old) != 1:
            print("FAIL  %s: anchor %r found %d times, expected exactly 1" % (rel, old, text.count(old)))
            return 2
        try:
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            outcome, tail = run_suite()
        finally:
            path.write_bytes(original)
        if path.read_bytes() != original:
            print("FAIL  %s was not restored byte-exact" % rel)
            return 2
        caught = outcome in ("red", "hang")
        failed = re.search(r"^FAILED (\S+)", tail, re.M)  # which test caught it, so a catch by something unrelated is visible
        print("%-6s %-22s %s" % ("caught" if caught else "ALIVE", rel, consequence) + ("  [suite hung]" if outcome == "hang" else "")
              + ("  <- " + failed.group(1).split("::")[-1][:60] if caught and failed else ""))
        if not caught:
            survivors.append(rel + ": " + consequence)
    if survivors:
        print("\n%d mutant(s) survived: a guard with no test behind it." % len(survivors))
        return 1
    print("\nAll %d mutants caught, every file restored byte-exact." % ran)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
