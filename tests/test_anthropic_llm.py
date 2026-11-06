"""The Claude API provider against tests/anthropic_standin.py, a real HTTP server written from the pages saved in
docs/vendor-contract/anthropic/. Nothing here is live: no key, no credit, no network beyond 127.0.0.1.

Every negative test is preceded by a positive control (the same request shape is answered normally), so a refusal in a test is
the guard under test and not a request the stand-in never accepted. Each guard has a mutant in tests/mutation_check.py."""
import logging
import random
from pathlib import Path

import httpx
import pytest

from agent import anthropic_llm, config
from agent.anthropic_llm import (
    BACKOFF_BASE_SECONDS, MAX_RETRIES, MAX_RETRY_AFTER_SECONDS, AnthropicLLM, LLMAPIError, LLMBadResponseError, LLMConfigError,
    LLMEmptyError, LLMRefusalError, LLMSpendLimitError, LLMTransportError, LLMTruncatedError, parse_retry_after)
from agent.config import Settings
from agent.llm import GroqLLM, LLMError, build_llm

from anthropic_standin import (
    SONNET, VALID_KEYS, VERSION, Fault, StandIn, error_body, http_error, message_body, text, thinking)

KEY = "sk-ant-valid-key-0001"
ROOT = Path(__file__).resolve().parent.parent
assert KEY in VALID_KEYS


@pytest.fixture
def server():
    s = StandIn().start()
    yield s
    s.stop()


class Rig:
    def __init__(self, server, **kw):
        self.server, self.sleeps = server, []
        self.llm = AnthropicLLM(kw.pop("key", KEY), url=server.url, sleep=self.sleeps.append, **kw)

    def ask(self, system="You write replies.", user="Where is my order?", **kw):
        return self.llm.complete(system, user, **kw)

    def control(self):
        """The positive control: this client, this server, a normal request, a normal answer."""
        assert self.ask() == self.server.answer
        self.server.requests.clear()
        self.sleeps.clear()


@pytest.fixture
def rig(server):
    return Rig(server)


# ---- the request on the wire ----------------------------------------------------------------------------------------

def test_a_normal_answer_is_returned_and_the_request_has_the_documented_shape(rig, server):
    assert rig.ask("SYS", "USER", max_tokens=321) == "Your order has shipped."
    (req,) = server.valid_requests()
    assert req["path"] == "/v1/messages"
    assert req["headers"]["authorization"] == "Bearer " + KEY
    assert req["headers"]["anthropic-version"] == VERSION == config.ANTHROPIC_VERSION
    assert req["headers"]["content-type"].startswith("application/json")
    assert "x-api-key" not in req["headers"] and "anthropic-workspace-id" not in req["headers"]
    assert req["body"] == {"model": "claude-sonnet-5-5", "max_tokens": 321, "system": "SYS",
                           "messages": [{"role": "user", "content": "USER"}], "thinking": {"type": "between_tools"}}


def test_no_sampling_parameter_is_ever_sent(rig, server):
    # api-versioning.txt: temperature, top_p and top_k "return a 400 error on every model" since 2026-10-21
    rig.ask()
    body = server.valid_requests()[0]["body"]
    assert not {"temperature", "top_p", "top_k"} & set(body)


def test_the_stand_in_itself_refuses_a_sampling_parameter(server):
    # control for the test above: the server is strict, so "no 400 happened" means something
    r = httpx.post(server.url, headers={"Authorization": "Bearer " + KEY, "anthropic-version": VERSION},
                   json={"model": SONNET, "max_tokens": 5, "messages": [{"role": "user", "content": "x"}], "temperature": 0})
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"


def test_the_default_model_is_the_one_on_the_models_page():
    assert config.ANTHROPIC_MODEL == "claude-sonnet-5-5"
    saved = (ROOT / "docs" / "vendor-contract" / "anthropic" / "models-overview.txt").read_text(encoding="utf-8")
    assert "claude-sonnet-5-5" in saved


def test_between_tools_goes_to_sonnet_5_5_only(server):
    ok = Rig(server, model="claude-opus-5-5")
    ok.control()
    ok.ask()
    assert "thinking" not in server.valid_requests()[-1]["body"]
    # control passed above; now the same field on another model is what the server refuses
    wrong = Rig(server, model="claude-opus-5-5", thinking={"type": "between_tools"})
    with pytest.raises(LLMAPIError) as e:
        wrong.ask()
    assert e.value.status == 400 and "between_tools" in str(e.value)


def test_an_explicit_thinking_setting_wins(server):
    r = Rig(server, thinking={"type": "adaptive"})
    r.ask()
    assert server.valid_requests()[-1]["body"]["thinking"] == {"type": "adaptive"}


def test_the_workspace_header_is_sent_only_when_set_and_only_when_well_formed(server):
    Rig(server, workspace="wrkspc_011CZkZaBF1tNoB5wlCeusgy").ask()
    assert server.valid_requests()[-1]["headers"]["anthropic-workspace-id"] == "wrkspc_011CZkZaBF1tNoB5wlCeusgy"
    for bad in ("wrk space", "w\r\nX-Injected: 1", "x" * 65, "wrkspc_é"):
        with pytest.raises(LLMConfigError) as e:
            AnthropicLLM(KEY, workspace=bad)
        assert KEY not in str(e.value)


@pytest.mark.parametrize("key", ["", "short", "has space in it 1234", "line\r\nbreak-1234567", "tab\tinside-1234567", "café-key-12345678"])
def test_a_malformed_key_is_refused_before_any_request_and_never_echoed(key, server):
    with pytest.raises(LLMConfigError) as e:
        AnthropicLLM(key, url=server.url)
    assert key.strip() not in str(e.value) or not key.strip()
    assert server.requests == []


@pytest.mark.parametrize("bad", [0, -5, True, 1.5, "10", None])
def test_max_tokens_must_be_a_positive_integer(rig, server, bad):
    rig.control()
    with pytest.raises(LLMConfigError):
        rig.ask(max_tokens=bad)
    assert server.requests == []


# ---- the answer ------------------------------------------------------------------------------------------------------

def test_text_blocks_are_joined_thinking_blocks_are_not_part_of_the_answer(rig, server):
    rig.control()
    server.script(Fault(200, message_body([thinking("hmm"), text("Hello, "), {"type": "redacted_thinking", "data": "x"}, text("there.  ")])))
    assert rig.ask() == "Hello, there."


# (stop_reason, content, exception) : the whole matrix from handling-stop-reasons.txt
STOP_TABLE = [
    ("end_turn", [text("fine")], None),
    ("end_turn", [], LLMEmptyError),
    ("end_turn", [text("   \n")], LLMEmptyError),
    ("end_turn", [thinking("only thinking")], LLMEmptyError),
    ("max_tokens", [thinking("ran out while thinking")], LLMTruncatedError),
    ("max_tokens", [], LLMTruncatedError),
    ("max_tokens", [text("The order is on its way and should arr")], LLMTruncatedError),
    ("model_context_window_exceeded", [text("partial")], LLMTruncatedError),
    ("refusal", [], LLMRefusalError),
    ("refusal", [text("I can't help with that.")], LLMRefusalError),
    ("tool_use", [{"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}}], LLMBadResponseError),
    ("pause_turn", [text("half")], LLMBadResponseError),
    ("stop_sequence", [text("half")], LLMBadResponseError),
    (None, [text("half")], LLMBadResponseError),
    ("a_value_added_later", [text("half")], LLMBadResponseError),
]


@pytest.mark.parametrize("stop,content,exc", STOP_TABLE, ids=["%s-%d-%s" % (s, i, e.__name__ if e else "ok") for i, (s, _, e) in enumerate(STOP_TABLE)])
def test_every_stop_reason_is_a_reply_or_a_named_error_never_an_empty_string(rig, server, stop, content, exc):
    rig.control()
    server.script(Fault(200, message_body(content, stop)))
    if exc is None:
        assert rig.ask() == "fine"
        return
    with pytest.raises(exc) as e:
        rig.ask()
    assert isinstance(e.value, LLMError) and str(e.value)  # the agent catches LLMError and hands the case to a person


def test_a_truncated_error_says_whether_any_text_arrived(rig, server):
    rig.control()
    server.script(Fault(200, message_body([thinking()], "max_tokens")), Fault(200, message_body([text("cut o")], "max_tokens")))
    with pytest.raises(LLMTruncatedError) as none:
        rig.ask()
    with pytest.raises(LLMTruncatedError) as some:
        rig.ask()
    assert none.value.has_text is False and some.value.has_text is True


def test_a_refusal_carries_the_category_from_stop_details(rig, server):
    rig.control()
    body = message_body([], "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"})
    server.script(Fault(200, body), Fault(200, message_body([], "refusal")))
    with pytest.raises(LLMRefusalError) as e:
        rig.ask()
    assert e.value.category == "cyber" and "cyber" in str(e.value)
    with pytest.raises(LLMRefusalError) as e2:
        rig.ask()
    assert e2.value.category == "" and "unknown" in str(e2.value)


@pytest.mark.parametrize("fault", [
    Fault(raw=b"<html>gateway</html>"),
    Fault(raw=b"not json at all"),
    Fault(body=["a", "list"]),
    Fault(body="a string"),
    Fault(body={"type": "error", "error": {"type": "api_error", "message": "mid flight"}}),
    Fault(body=message_body("not a list")),
    Fault(body={**message_body([text("x")]), "type": "something_else"}),
], ids=["html", "text", "list", "string", "error-on-200", "content-not-list", "wrong-type"])
def test_a_200_that_is_not_a_message_is_a_named_error(rig, server, fault):
    rig.control()
    server.script(fault)
    with pytest.raises(LLMBadResponseError):
        rig.ask()


# ---- non-200 answers: the status matrix ------------------------------------------------------------------------------

# (status, error type, retried?) from api-errors.txt
ERROR_TABLE = [
    (400, "invalid_request_error", False), (401, "authentication_error", False), (402, "billing_error", False),
    (403, "permission_error", False), (404, "not_found_error", False), (413, "request_too_large", False),
    (429, "rate_limit_error", True), (500, "api_error", True), (504, "timeout_error", True), (529, "overloaded_error", True),
]


@pytest.mark.parametrize("status,etype,retried", ERROR_TABLE, ids=[str(t[0]) for t in ERROR_TABLE])
def test_each_documented_error_is_named_and_only_the_transient_ones_are_retried(rig, server, status, etype, retried):
    rig.control()
    server.script(*[http_error(status, etype, "scripted %s" % etype)] * (MAX_RETRIES + 1))
    with pytest.raises(LLMAPIError) as e:
        rig.ask()
    err = e.value
    assert (err.status, err.error_type, err.request_id) == (status, etype, "req_018EeWyXxfu5pfWkrYcMdjWG")
    assert "HTTP %d %s: scripted %s" % (status, etype, etype) in str(err) and "req_018EeWyXxfu5pfWkrYcMdjWG" in str(err)
    assert len(server.valid_requests()) == (MAX_RETRIES + 1 if retried else 1)
    assert len(rig.sleeps) == (MAX_RETRIES if retried else 0)


def test_an_unlisted_4xx_is_not_retried_and_an_unlisted_5xx_is(rig, server):
    rig.control()
    server.script(http_error(409, "conflict_error"))
    with pytest.raises(LLMAPIError):
        rig.ask()
    assert len(server.valid_requests()) == 1
    server.requests.clear()
    server.script(http_error(502, "api_error"), Fault())
    assert rig.ask() == server.answer and len(server.valid_requests()) == 2


# ---- retries ---------------------------------------------------------------------------------------------------------

def test_a_429_with_retry_after_waits_that_long_and_then_succeeds(rig, server):
    rig.control()
    server.script(http_error(429, "rate_limit_error", headers={"retry-after": "7"}))
    assert rig.ask() == server.answer
    assert rig.sleeps == [7.0] and len(server.valid_requests()) == 2


def test_a_retry_after_shorter_than_the_backoff_does_not_shorten_it(rig, server):
    rig.control()
    server.script(http_error(429, "rate_limit_error", headers={"retry-after": "0"}))
    assert rig.ask() == server.answer
    assert rig.sleeps == [BACKOFF_BASE_SECONDS]


def test_a_529_is_retried_with_a_doubling_backoff(rig, server):
    rig.control()
    server.script(http_error(529, "overloaded_error"), http_error(529, "overloaded_error"))
    assert rig.ask() == server.answer
    assert rig.sleeps == [1.0, 2.0] and len(server.valid_requests()) == 3


def test_retries_stop_at_the_limit_and_the_last_error_is_raised(rig, server):
    rig.control()
    server.script(*[http_error(529, "overloaded_error", "still busy")] * 5)
    with pytest.raises(LLMAPIError) as e:
        rig.ask()
    assert e.value.status == 529 and len(server.valid_requests()) == 3 and rig.sleeps == [1.0, 2.0]


def test_max_retries_zero_means_one_attempt(server):
    r = Rig(server, max_retries=0)
    r.control()
    server.script(http_error(529, "overloaded_error"))
    with pytest.raises(LLMAPIError):
        r.ask()
    assert len(server.valid_requests()) == 1 and r.sleeps == []


def test_a_retry_after_longer_than_a_shopper_will_wait_fails_fast(rig, server):
    rig.control()
    server.script(http_error(429, "rate_limit_error", headers={"retry-after": str(int(MAX_RETRY_AFTER_SECONDS) + 1)}))
    with pytest.raises(LLMAPIError) as e:
        rig.ask()
    assert e.value.retry_after == MAX_RETRY_AFTER_SECONDS + 1 and rig.sleeps == [] and len(server.valid_requests()) == 1


def test_a_retry_after_at_the_limit_is_still_waited_for(rig, server):
    rig.control()
    server.script(http_error(429, "rate_limit_error", headers={"retry-after": str(int(MAX_RETRY_AFTER_SECONDS))}))
    assert rig.ask() == server.answer and rig.sleeps == [MAX_RETRY_AFTER_SECONDS]


@pytest.mark.parametrize("header", ["soon", "-5", "nan", "inf", "", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_an_unusable_retry_after_falls_back_to_the_backoff(rig, server, header):
    rig.control()
    server.script(http_error(429, "rate_limit_error", headers={"retry-after": header}))
    assert rig.ask() == server.answer and rig.sleeps == [BACKOFF_BASE_SECONDS]


def test_parse_retry_after_reads_seconds_only():
    assert parse_retry_after("12") == 12.0 and parse_retry_after(" 0.5 ") == 0.5 and parse_retry_after(None) is None
    for bad in ("-1", "nan", "inf", "x", ""):
        assert parse_retry_after(bad) is None


def test_the_spend_cap_429_is_never_retried(rig, server):
    # api-rate-limits.txt: it has no retry-after, error.details.error_code is enforced_spend_limit_reached, and retrying fails
    rig.control()
    server.script(*[http_error(429, "rate_limit_error", "You have reached your API usage limits.",
                               details={"error_code": "enforced_spend_limit_reached"})] * 3)
    with pytest.raises(LLMSpendLimitError) as e:
        rig.ask()
    assert e.value.status == 429 and len(server.valid_requests()) == 1 and rig.sleeps == []


def test_a_429_with_another_detail_code_is_an_ordinary_rate_limit(rig, server):
    rig.control()
    server.script(http_error(429, "rate_limit_error", details={"error_code": "something_else"}), Fault())
    assert rig.ask() == server.answer and len(server.valid_requests()) == 2


def test_a_dropped_connection_is_retried_then_reported(rig, server):
    rig.control()
    server.script(Fault(drop=True), Fault())
    assert rig.ask() == server.answer and rig.sleeps == [1.0]
    server.requests.clear()
    server.script(Fault(drop=True), Fault(drop=True), Fault(drop=True), Fault())
    with pytest.raises(LLMTransportError) as e:
        rig.ask()
    assert "RemoteProtocolError" in str(e.value) and len(server.valid_requests()) == 3


def test_a_refused_connection_is_a_named_error_and_is_retried(monkeypatch):
    # a real refused connect takes about 2 s on Windows, so the client is swapped for one whose transport refuses at once
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    monkeypatch.setattr(anthropic_llm, "_client", lambda: httpx.Client(transport=httpx.MockTransport(refuse)))
    sleeps = []
    with pytest.raises(LLMTransportError) as e:
        AnthropicLLM(KEY, url="http://127.0.0.1:9/v1/messages", sleep=sleeps.append).complete("s", "u")
    assert "ConnectError" in str(e.value) and sleeps == [1.0, 2.0]


def test_a_read_timeout_is_a_named_error_and_is_not_repeated(server):
    r = Rig(server, timeout=0.2)
    r.control()
    server.script(Fault(delay=0.6))
    with pytest.raises(LLMTransportError) as e:
        r.ask()
    assert "ReadTimeout" in str(e.value) and r.sleeps == [] and len(server.valid_requests()) == 1


# ---- the key and the vendor's words ----------------------------------------------------------------------------------

def test_the_key_never_appears_in_any_error(rig, server, caplog):
    caplog.set_level(logging.DEBUG)
    rig.control()
    echo = "bad credential %s was refused" % KEY
    faults = [http_error(401, "authentication_error", echo), http_error(400, "invalid_request_error", echo),
              http_error(500, "api_error", echo), Fault(raw=("<html>%s</html>" % KEY).encode()),
              Fault(400, error_body("invalid_request_error", "x" * 5000 + KEY)),
              Fault(200, message_body([text("hi")], KEY)), Fault(200, message_body([], "refusal", stop_details={"category": KEY})),
              http_error(418, KEY + "_type", echo)]
    for f in faults:
        server.script(*([f] * (MAX_RETRIES + 1)))
        with pytest.raises(LLMError) as e:
            rig.ask()
        assert KEY not in str(e.value) and KEY not in repr(e.value)
        server.faults.clear()
    assert KEY not in caplog.text


def test_an_echoed_key_is_masked_not_dropped(rig, server):
    rig.control()
    server.script(http_error(401, "authentication_error", "Invalid key %s." % KEY))
    with pytest.raises(LLMAPIError) as e:
        rig.ask()
    assert "Invalid key [key]." in str(e.value)


def test_a_long_vendor_message_is_truncated(rig, server):
    rig.control()
    server.script(http_error(400, "invalid_request_error", "word " * 400))
    with pytest.raises(LLMAPIError) as e:
        rig.ask()
    assert len(str(e.value)) < 330 and "word word" in str(e.value)


def test_a_body_that_is_not_the_error_object_is_not_echoed(rig, server):
    rig.control()
    server.script(Fault(502, raw=b"<html><body>SECRET-INTERNAL-PAGE upstream down</body></html>"))
    server.script(Fault(502, raw=b"<html><body>SECRET-INTERNAL-PAGE upstream down</body></html>"))
    server.script(Fault(502, raw=b"<html><body>SECRET-INTERNAL-PAGE upstream down</body></html>"))
    with pytest.raises(LLMAPIError) as e:
        rig.ask()
    assert "SECRET-INTERNAL-PAGE" not in str(e.value) and "HTTP 502" in str(e.value)


def test_a_request_id_header_that_is_not_an_id_is_left_out(rig, server):
    rig.control()
    server.script(http_error(400, "invalid_request_error", headers={"request-id": "x y\tz"}))
    with pytest.raises(LLMAPIError) as e:
        rig.ask()
    assert e.value.request_id == "" and "request-id" not in str(e.value)


def test_settings_never_print_the_anthropic_key():
    s = Settings(groq_key="g", shop="s", shopify_token="t", llm_provider="anthropic", anthropic_key=KEY)
    assert KEY not in repr(s) and KEY not in str(s)


# ---- every request in a seeded random world obeys the rules ----------------------------------------------------------

def _oracle(faults):
    """The rules, written out plainly and independently: (requests made, sleeps, outcome)."""
    sleeps = []
    for attempt in range(MAX_RETRIES + 1):
        kind, arg = faults[attempt] if attempt < len(faults) else ("ok", None)
        if kind == "ok":
            return attempt + 1, sleeps, "ok"
        if kind in ("400", "401", "spend"):
            return attempt + 1, sleeps, "api"
        if attempt == MAX_RETRIES:
            return attempt + 1, sleeps, "transport" if kind == "drop" else "api"
        wait = 1.0 * 2 ** attempt
        if kind in ("429", "529", "500") and arg is not None:
            if arg > 30:
                return attempt + 1, sleeps, "api"
            wait = max(wait, arg)
        sleeps.append(wait)
    raise AssertionError("unreachable")


def _fault(kind, arg):
    if kind == "drop":
        return Fault(drop=True)
    headers = {"retry-after": str(arg)} if arg is not None else {}
    if kind == "spend":
        return http_error(429, "rate_limit_error", details={"error_code": "enforced_spend_limit_reached"})
    status, etype = {"400": (400, "invalid_request_error"), "401": (401, "authentication_error"), "429": (429, "rate_limit_error"),
                     "529": (529, "overloaded_error"), "500": (500, "api_error")}[kind]
    return http_error(status, etype, headers=headers)


@pytest.mark.parametrize("seed", range(40))
def test_retry_rules_hold_over_random_fault_sequences(server, seed):
    rng = random.Random(seed)
    rig = Rig(server)
    rig.control()
    faults = []
    for _ in range(rng.randint(0, 4)):
        kind = rng.choice(["429", "529", "500", "drop", "400", "401", "spend", "429", "529"])
        arg = rng.choice([None, 0, 0.5, 2, 5, 29, 30, 31, 90]) if kind in ("429", "529", "500") else None
        faults.append((kind, arg))
    server.script(*[_fault(k, a) for k, a in faults])
    requests, sleeps, outcome = _oracle(faults)
    try:
        got = rig.ask()
        result = "ok"
        assert got == server.answer
    except LLMTransportError:
        result = "transport"
    except LLMAPIError:
        result = "api"
    assert (len(server.valid_requests()), rig.sleeps, result) == (requests, sleeps, outcome), faults


# ---- choosing the provider -------------------------------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    """No saved secret file and none of the variables: the real Operations/.secrets folder is never read by a test."""
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    for name in ("LLM_PROVIDER", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "SHOPIFY_SHOP", "SHOPIFY_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHOPIFY_SHOP", "demo.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_x")
    return monkeypatch


def test_groq_is_the_default_and_still_needs_its_own_key(env):
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        config.load_settings()
    env.setenv("GROQ_API_KEY", "gsk_x")
    s = config.load_settings()
    assert s.llm_provider == "groq" and isinstance(build_llm(s), GroqLLM)


def test_groq_needs_no_anthropic_key(env):
    env.setenv("GROQ_API_KEY", "gsk_x")
    assert config.load_settings().anthropic_key == ""


def test_the_anthropic_provider_needs_its_key_and_not_the_groq_one(env):
    env.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(RuntimeError) as e:
        config.load_settings()
    assert "ANTHROPIC_API_KEY" in str(e.value) and "GROQ_API_KEY" not in str(e.value)
    env.setenv("ANTHROPIC_API_KEY", KEY)
    s = config.load_settings()
    llm = build_llm(s)
    assert s.llm_provider == "anthropic" and isinstance(llm, AnthropicLLM)


def test_the_provider_name_is_case_and_space_insensitive_and_an_unknown_one_is_refused(env):
    env.setenv("ANTHROPIC_API_KEY", KEY)
    env.setenv("LLM_PROVIDER", " Anthropic ")
    assert config.load_settings().llm_provider == "anthropic"
    env.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(RuntimeError, match="LLM_PROVIDER"):
        config.load_settings()
    with pytest.raises(LLMError):
        build_llm(Settings(groq_key="", shop="s", shopify_token="t", llm_provider="openai"))


def test_an_empty_provider_variable_means_groq(env):
    env.setenv("LLM_PROVIDER", "")
    env.setenv("GROQ_API_KEY", "gsk_x")
    assert config.load_settings().llm_provider == "groq"


def test_the_anthropic_key_is_read_from_the_saved_secret_file_when_the_variable_is_unset(env, tmp_path):
    (tmp_path / "anthropic_key.txt").write_text("ANTHROPIC_API_KEY=%s\n" % KEY, encoding="utf-8")
    env.setenv("LLM_PROVIDER", "anthropic")
    assert config.load_settings().anthropic_key == KEY


def test_the_workspace_id_reaches_the_client(env, server):
    env.setenv("LLM_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", KEY)
    env.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_abc123")
    env.setattr(config, "ANTHROPIC_URL", server.url)
    build_llm(config.load_settings()).complete("s", "u")
    assert server.valid_requests()[0]["headers"]["anthropic-workspace-id"] == "wrkspc_abc123"


def test_a_settings_object_built_the_old_way_still_works():
    s = Settings(groq_key="x", shop="demo.myshopify.com", shopify_token="t")
    assert s.llm_provider == "groq" and s.anthropic_key == ""


def test_the_anthropic_provider_without_a_key_stops_at_start_with_a_clear_message():
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        build_llm(Settings(groq_key="", shop="s", shopify_token="t", llm_provider="anthropic"))
