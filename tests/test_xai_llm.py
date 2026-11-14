"""The xAI (Grok) chat provider against tests/xai_standin.py, a real HTTP server written from the pages saved in
docs/vendor-contract/xai-*.txt. Nothing here is live: no xAI key, no network beyond 127.0.0.1.

Every negative test is preceded by a positive control (the same request shape is answered normally), so a refusal in a test is the
guard under test and not a request the stand-in never accepted. Each guard has a mutant in tests/mutation_check.py."""
import random
import threading
from pathlib import Path

import httpx
import pytest

from agent import config, xai_http
from agent.config import Settings
from agent.llm import LLMError, GroqLLM, build_llm
from agent.xai_http import MAX_RETRIES, XaiAPIError, XaiBadResponseError, XaiConfigError, XaiError, XaiTransportError
from agent.xai_llm import REASONING_EFFORT_MODELS, XaiEmptyError, XaiLLM, XaiRefusalError, XaiTruncatedError

from xai_standin import OTHER_KEY, VALID_KEY, Fault, StandIn, chat_body, http_error

ROOT = Path(__file__).resolve().parent.parent
SAVED = ROOT / "docs" / "vendor-contract"


def saved(name):
    return (SAVED / name).read_text(encoding="utf-8")


@pytest.fixture
def server():
    s = StandIn().start()
    yield s
    s.stop()


class Rig:
    def __init__(self, server, **kw):
        self.server, self.sleeps = server, []
        self.llm = XaiLLM(kw.pop("key", VALID_KEY), kw.pop("model", "grok-4.7"), base_url=server.base, sleep=self.sleeps.append, **kw)

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


def one_chat(server):
    (req,) = server.of_kind("chat")
    return req


# ---- the requests on the wire ----------------------------------------------------------------------------------------

def test_a_normal_answer_is_returned_and_the_request_has_the_documented_shape(rig, server):
    assert rig.ask("SYS", "USER", max_tokens=321) == "Your order has shipped."
    req = one_chat(server)
    assert req["method"] == "POST" and req["path"] == "/v1/chat/completions"
    assert req["headers"]["authorization"] == "Bearer " + VALID_KEY
    assert req["headers"]["content-type"].startswith("application/json")
    assert req["body"] == {"model": "grok-4.7", "max_completion_tokens": 321, "temperature": 0, "reasoning_effort": "low",
                           "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]}


def test_the_stand_in_itself_refuses_what_the_pages_say_not_to_send(server, rig):
    # control for the tests above: the server is strict, so "no 400 happened" means something
    rig.control()
    url = server.base + "/chat/completions"
    auth = {"Authorization": "Bearer " + VALID_KEY}
    good = {"model": "grok-4.7", "messages": [{"role": "user", "content": "x"}], "max_completion_tokens": 50, "reasoning_effort": "low"}
    assert httpx.post(url, headers=auth, json=good).status_code == 200
    assert httpx.post(url, headers={"Authorization": "Bearer " + OTHER_KEY}, json=good).status_code == 401
    assert httpx.post(url, headers={}, json=good).status_code == 401
    assert httpx.post(url, headers=auth, json=dict(good, bogus=1)).status_code == 422
    assert httpx.post(url, headers=auth, json=dict(good, model="gpt-5")).status_code == 400
    assert httpx.post(url, headers=auth, json=dict(good, stop=["x"])).status_code == 400  # reasoning models: no stop
    assert httpx.post(url, headers=auth, json=dict(good, presence_penalty=0.5)).status_code == 400
    assert httpx.post(url, headers=auth, json=dict(good, reasoning_effort="max")).status_code == 400
    assert httpx.post(url, headers=auth, json=dict(good, model="grok-4.3")).status_code == 400  # effort only on the three models
    assert httpx.post(url, headers=auth, json=dict(good, max_completion_tokens=0)).status_code == 422
    assert httpx.post(url, headers=auth, json=dict(good, temperature=3)).status_code == 422
    assert httpx.post(url, headers=auth, json=dict(good, messages=[{"role": "User", "content": "x"}])).status_code == 422
    assert httpx.post(url, headers=auth, content=b"x", ).status_code == 415
    assert httpx.get(url, headers=auth).status_code == 405


def test_nothing_the_pages_say_not_to_send_is_sent(rig, server):
    rig.ask()
    body = one_chat(server)["body"]
    for forbidden in ("tools", "tool_choice", "max_tokens", "stream", "stop", "presence_penalty", "frequency_penalty", "n", "deferred",
                      "search_parameters", "response_format", "user"):
        assert forbidden not in body


def test_the_key_goes_only_in_the_authorization_header(rig, server):
    rig.ask()
    req = one_chat(server)
    assert VALID_KEY not in req["raw"].decode() + req["path"]
    assert [k for k, v in req["headers"].items() if VALID_KEY in v] == ["authorization"]


def test_a_model_without_the_reasoning_parameter_is_not_sent_one(server):
    r = Rig(server, model="grok-4.20-0309-non-reasoning")
    r.ask()
    assert "reasoning_effort" not in one_chat(server)["body"]


@pytest.mark.parametrize("model", REASONING_EFFORT_MODELS)
def test_the_three_models_that_take_reasoning_effort_get_it(server, model):
    Rig(server, model=model, reasoning_effort="medium").ask()
    assert one_chat(server)["body"]["reasoning_effort"] == "medium"


def test_the_defaults_are_the_ones_on_the_saved_pages():
    guide, ref, reasoning = saved("xai-chat-completions-guide.txt"), saved("xai-api-reference-chat-completions.txt"), saved("xai-reasoning.txt")
    assert config.XAI_DEFAULT_URL == "https://api.x.ai/v1" and "curl " + config.XAI_DEFAULT_URL + "/chat/completions" in guide
    assert config.XAI_DEFAULT_MODEL == "grok-4.7" and '"model": "%s"' % config.XAI_DEFAULT_MODEL in guide
    assert "Authorization: Bearer $XAI_API_KEY" in guide
    assert "## POST /v1/chat/completions" in ref and "`max_completion_tokens`" in ref and "[DEPRECATED]" in ref.replace("\\", "")
    assert "`refusal`" in ref and "`finish_reason`" in ref and "`cost_in_usd_ticks`" in ref
    assert set(REASONING_EFFORT_MODELS) == {"grok-4.7", "grok-4.6", "grok-4.5"}
    for m in REASONING_EFFORT_MODELS:
        assert "`%s`" % m in reasoning.split("support the `reasoning_effort` parameter")[0] + reasoning[:1500]
    assert 'defaults to `"high"`' in reasoning and "Reasoning cannot be disabled" in reasoning
    assert "cannot be used with reasoning models" in reasoning


# ---- configuration is checked before any request ----------------------------------------------------------------------

@pytest.mark.parametrize("key", ["", "short", "has space in it 1234", "line\r\nbreak-1234567", "tab\tinside-1234567", "café-key-12345678", "valid-key-12345678\n", None])
def test_a_missing_or_malformed_key_is_refused_before_any_request(server, key):
    Rig(server).control()  # control: a normal key works against this server
    with pytest.raises(XaiConfigError) as e:
        Rig(server, key=key)
    assert "XAI_API_KEY" in str(e.value) and not server.requests


@pytest.mark.parametrize("model", ["", "grok 4", "grok/4.7", "x" * 101, None, "grok-4.7\n"])
def test_a_malformed_model_is_refused(server, model):
    with pytest.raises(XaiConfigError):
        XaiLLM(VALID_KEY, model, base_url=server.base)


@pytest.mark.parametrize("url", ["", "http://api.x.ai/v1", "ftp://api.x.ai/v1", "api.x.ai/v1", "https://", "http://example.com/v1",
                                 "http://127.0.0.1.evil.example/v1", "file:///etc/hosts"])
def test_only_https_or_this_machine_is_accepted_as_the_base_url(url):
    with pytest.raises(XaiConfigError):
        XaiLLM(VALID_KEY, base_url=url)


@pytest.mark.parametrize("url", ["https://api.x.ai/v1", "https://us.api.x.ai/v1", "http://127.0.0.1:9/v1", "http://localhost:9/v1", "http://[::1]:9/v1"])
def test_https_and_loopback_http_are_accepted(url):
    XaiLLM(VALID_KEY, base_url=url)


def test_a_trailing_slash_on_the_base_url_does_not_double_the_path(server):
    for base in (server.base + "/", server.base):
        XaiLLM(VALID_KEY, base_url=base, sleep=lambda s: None).complete("s", "u")
    assert [r["path"] for r in server.of_kind("chat")] == ["/v1/chat/completions"] * 2
    assert xai_http.checked_url("https://api.x.ai/v1/", "u") == "https://api.x.ai/v1"
    assert xai_http.checked_url("http://127.0.0.1:9/v1/", "u") == "http://127.0.0.1:9/v1"


@pytest.mark.parametrize("bad", [0, -1, True, "5", 1.5, None])
def test_a_bad_max_tokens_is_refused_before_a_request(rig, server, bad):
    rig.control()
    with pytest.raises(XaiConfigError):
        rig.ask(max_tokens=bad)
    assert not server.requests
    assert rig.ask(max_tokens=1) == server.answer  # one is the smallest the guard lets through


@pytest.mark.parametrize("effort", ["", "LOW", "max", None, "none"])
def test_a_reasoning_effort_the_page_does_not_list_is_refused(server, effort):
    with pytest.raises(XaiConfigError):
        XaiLLM(VALID_KEY, base_url=server.base, reasoning_effort=effort)


# ---- the answer ------------------------------------------------------------------------------------------------------

def test_the_reply_is_trimmed(rig, server):
    server.answer = "  Your order has shipped.\n"
    assert rig.ask() == "Your order has shipped."


@pytest.mark.parametrize("body,exc,has_text", [
    (chat_body("half a sentence and", "length"), XaiTruncatedError, True),
    (chat_body(None, "length"), XaiTruncatedError, False),
    (chat_body("", "length"), XaiTruncatedError, False),
    (chat_body("  ", "length"), XaiTruncatedError, False),
], ids=["cut-off", "no-text-null", "no-text-empty", "no-text-blank"])
def test_a_cut_off_reply_is_a_named_error_and_says_whether_there_was_text(rig, server, body, exc, has_text):
    rig.control()
    server.script("chat", Fault(200, body))
    with pytest.raises(exc) as e:
        rig.ask()
    assert e.value.has_text is has_text and "length" in str(e.value)


@pytest.mark.parametrize("content", [None, "", "   \n"])
def test_a_finished_reply_with_no_text_is_an_empty_error(rig, server, content):
    rig.control()
    server.script("chat", Fault(200, chat_body(content, "stop")))
    with pytest.raises(XaiEmptyError):
        rig.ask()


@pytest.mark.parametrize("finish", ["tool_calls", "end_turn", "content_filter", None, 5, "x y\nz", "x" * 80, "tool\n"])
def test_any_other_finish_reason_is_not_a_reply(rig, server, finish):
    rig.control()
    server.script("chat", Fault(200, chat_body("looks fine", finish)))
    with pytest.raises(XaiBadResponseError) as e:
        rig.ask()
    assert "finish_reason" in str(e.value) and "\n" not in str(e.value) and "x" * 80 not in str(e.value)


def test_a_refusal_is_a_named_error_even_when_text_came_with_it(rig, server):
    rig.control()
    server.script("chat", Fault(200, chat_body("I cannot help with that.", "stop", refusal="policy")))
    with pytest.raises(XaiRefusalError):
        rig.ask()
    server.script("chat", Fault(200, chat_body("fine", "stop", refusal="")))  # an empty refusal is not one
    assert rig.ask() == "fine"


def test_a_tool_call_is_refused_because_no_tools_were_sent(rig, server):
    rig.control()
    body = chat_body("text", "stop")
    body["choices"][0]["message"]["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    server.script("chat", Fault(200, body))
    with pytest.raises(XaiBadResponseError):
        rig.ask()


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"[1, 2]", b'"text"', b"null",
                                 b'{"choices": []}', b'{"choices": {}}', b'{"choices": "x"}', b'{"choices": [1]}', b'{"choices": [null]}',
                                 b'{"choices": [{"finish_reason": "stop"}]}', b'{"choices": [{"message": "x", "finish_reason": "stop"}]}',
                                 b'{"id": "x"}'])
def test_a_200_that_is_not_a_chat_answer_is_a_named_error(rig, server, raw):
    rig.control()
    server.script("chat", Fault(200, raw=raw))
    with pytest.raises(XaiBadResponseError):
        rig.ask()


def test_usage_is_reported_and_junk_in_it_is_ignored(server):
    seen = []
    r = Rig(server, on_usage=seen.append)
    r.ask()
    assert seen == [{"prompt_tokens": 32, "completion_tokens": 9, "total_tokens": 135, "cost_in_usd_ticks": 123456}]
    body = chat_body("ok")
    body["usage"] = {"prompt_tokens": float("nan"), "completion_tokens": True, "total_tokens": float("inf"), "cost_in_usd_ticks": 7,
                     "extra": 1}
    server.script("chat", Fault(200, body))
    seen.clear()
    assert r.ask() == "ok" and seen == [{"cost_in_usd_ticks": 7}]
    server.script("chat", Fault(200, dict(chat_body("ok"), usage=None)))
    seen.clear()
    assert r.ask() == "ok" and seen == []


# ---- failures, retries and what a failure shows -------------------------------------------------------------------------

@pytest.mark.parametrize("status,meaning", [(400, "Bad Request"), (401, "Unauthorized"), (403, "Forbidden"), (404, "Not Found"),
                                            (405, "Method Not Allowed"), (415, "Unsupported Media Type"), (422, "Unprocessable Entity")])
def test_a_client_error_is_named_from_the_status_table_and_not_retried(rig, server, status, meaning):
    rig.control()
    server.script("chat", http_error(status, "body text that must not be shown"))
    with pytest.raises(XaiAPIError) as e:
        rig.ask()
    assert e.value.status == status and str(e.value).startswith("HTTP %d %s" % (status, meaning))
    assert "must not be shown" not in str(e.value) and len(server.of_kind("chat")) == 1 and rig.sleeps == []
    assert not e.value.retryable


def test_a_wrong_key_is_a_401_that_names_the_key_and_leaks_it_nowhere(server):
    r = Rig(server, key=OTHER_KEY)
    with pytest.raises(XaiAPIError) as e:
        r.ask()
    assert e.value.status == 401 and "API key" in str(e.value) and OTHER_KEY not in str(e.value) and r.sleeps == []


def test_a_response_body_that_echoes_the_key_never_reaches_the_message(rig, server):
    rig.control()
    server.script("chat", http_error(403, "rejected key %s for user" % VALID_KEY))
    with pytest.raises(XaiAPIError) as e:
        rig.ask()
    assert VALID_KEY not in str(e.value) and VALID_KEY not in repr(e.value) and "rejected" not in str(e.value)


@pytest.mark.parametrize("status", [429, 503])
def test_a_rate_limit_or_overload_is_retried_with_exponential_backoff_then_raised(rig, server, status):
    rig.control()
    server.script("chat", *[http_error(status)] * (MAX_RETRIES + 1))
    with pytest.raises(XaiAPIError) as e:
        rig.ask()
    assert e.value.status == status and e.value.retryable
    assert len(server.of_kind("chat")) == MAX_RETRIES + 1 and rig.sleeps == [1.0, 2.0]


@pytest.mark.parametrize("status", [429, 503])
def test_one_transient_failure_is_absorbed(rig, server, status):
    rig.control()
    server.script("chat", http_error(status))
    assert rig.ask() == server.answer
    assert len(server.of_kind("chat")) == 2 and rig.sleeps == [1.0]


@pytest.mark.parametrize("status", [500, 502, 504, 520])
def test_a_server_error_the_pages_do_not_list_is_not_retried(rig, server, status):
    rig.control()
    server.script("chat", http_error(status))
    with pytest.raises(XaiAPIError) as e:
        rig.ask()
    assert e.value.status == status and "server error" in str(e.value)
    assert len(server.of_kind("chat")) == 1 and rig.sleeps == []


def test_a_retry_after_header_is_not_read_because_no_saved_page_names_one(rig, server):
    rig.control()
    server.script("chat", http_error(429, headers={"Retry-After": "25"}))
    assert rig.ask() == server.answer
    assert rig.sleeps == [1.0]


def test_the_retry_count_is_a_setting(server):
    r = Rig(server, max_retries=0)
    server.script("chat", http_error(429))
    with pytest.raises(XaiAPIError):
        r.ask()
    assert len(server.of_kind("chat")) == 1 and r.sleeps == []


def test_a_202_on_the_chat_endpoint_is_not_a_reply(rig, server):
    rig.control()
    server.script("chat", Fault(202, {"request_id": "x"}))
    with pytest.raises(XaiAPIError) as e:
        rig.ask()
    assert e.value.status == 202 and len(server.of_kind("chat")) == 1


def test_a_dropped_connection_is_retried_and_then_a_transport_error(rig, server):
    rig.control()
    server.script("chat", Fault(drop=True))
    assert rig.ask() == server.answer and rig.sleeps == [1.0]
    server.requests.clear()
    rig.sleeps.clear()
    server.script("chat", *[Fault(drop=True)] * (MAX_RETRIES + 1))
    with pytest.raises(XaiTransportError) as e:
        rig.ask()
    assert len(server.of_kind("chat")) == MAX_RETRIES + 1 and rig.sleeps == [1.0, 2.0]
    assert "transport error" in str(e.value) and VALID_KEY not in str(e.value)


def test_a_read_timeout_is_not_repeated(server):
    r = Rig(server, timeout=0.2)
    r.control()
    server.script("chat", Fault(delay=1.0))
    with pytest.raises(XaiTransportError) as e:
        r.ask()
    assert "ReadTimeout" in str(e.value) and len(server.of_kind("chat")) == 1 and r.sleeps == []


def test_a_refused_connection_is_a_transport_error():
    s = StandIn().start()
    base = s.base
    s.stop()
    sleeps = []
    with pytest.raises(XaiTransportError):
        XaiLLM(VALID_KEY, base_url=base, sleep=sleeps.append).complete("s", "u")
    assert sleeps == [1.0, 2.0]


def test_every_failure_is_an_llm_error_so_the_agent_hands_the_case_to_a_person():
    for cls in (XaiError, XaiConfigError, XaiTransportError, XaiAPIError, XaiBadResponseError, XaiTruncatedError, XaiEmptyError, XaiRefusalError):
        assert issubclass(cls, LLMError)


def test_a_client_is_safe_to_share_between_threads(rig, server):
    out, errors = [], []

    def go(i):
        try:
            out.append(rig.ask(user="q%d" % i))
        except Exception as e:  # pragma: no cover - the assertion below reports it
            errors.append(e)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and out == [server.answer] * 8 and len(server.of_kind("chat")) == 8


# ---- settings and the factory ----------------------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    for name in ("LLM_PROVIDER", "XAI_API_KEY", "XAI_BASE_URL", "XAI_MODEL", "GROQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHOPIFY_SHOP", "demo.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_x")
    return tmp_path


def test_the_provider_list_names_xai_and_an_unknown_one_is_refused(clean_env, monkeypatch):
    assert "xai" in config.LLM_PROVIDERS
    monkeypatch.setenv("LLM_PROVIDER", " XAI ")
    monkeypatch.setenv("XAI_API_KEY", VALID_KEY)
    assert config.load_settings().llm_provider == "xai"
    monkeypatch.setenv("LLM_PROVIDER", "grok")
    with pytest.raises(RuntimeError) as e:
        config.load_settings()
    assert "xai" in str(e.value)


def test_a_missing_xai_key_stops_start_up_and_names_the_variable(clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "xai")
    with pytest.raises(RuntimeError) as e:
        config.load_settings()
    assert "XAI_API_KEY" in str(e.value) and "GROQ_API_KEY" not in str(e.value)


def test_the_xai_key_does_not_satisfy_groq_and_the_groq_key_not_xai(clean_env, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", VALID_KEY)
    with pytest.raises(RuntimeError) as e:
        config.load_settings()  # provider is groq: the xai key is not what it needs
    assert "GROQ_API_KEY" in str(e.value)


def test_the_key_model_and_url_come_from_the_environment(clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "xai")
    monkeypatch.setenv("XAI_API_KEY", VALID_KEY)
    monkeypatch.setenv("XAI_MODEL", " grok-4.6 ")
    monkeypatch.setenv("XAI_BASE_URL", " https://us.api.x.ai/v1 ")
    s = config.load_settings()
    assert (s.xai_key, s.xai_model, s.xai_base_url) == (VALID_KEY, "grok-4.6", "https://us.api.x.ai/v1")


def test_the_defaults_apply_when_nothing_is_set(clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "xai")
    monkeypatch.setenv("XAI_API_KEY", VALID_KEY)
    monkeypatch.setenv("XAI_MODEL", "")
    monkeypatch.setenv("XAI_BASE_URL", "")
    s = config.load_settings()
    assert (s.xai_model, s.xai_base_url) == ("grok-4.7", "https://api.x.ai/v1")


def test_the_key_can_come_from_the_saved_secret_file(clean_env, monkeypatch):
    (clean_env / "xai_key.txt").write_text("XAI_API_KEY=" + VALID_KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("LLM_PROVIDER", "xai")
    assert config.load_settings().xai_key == VALID_KEY
    monkeypatch.setenv("XAI_API_KEY", OTHER_KEY)  # the environment wins
    assert config.load_settings().xai_key == OTHER_KEY


def test_build_llm_makes_the_xai_client_and_groq_stays_the_default():
    s = Settings(groq_key="g", shop="s", shopify_token="t", llm_provider="xai", xai_key=VALID_KEY, xai_model="grok-4.6",
                 xai_base_url="https://us.api.x.ai/v1")
    llm = build_llm(s)
    assert isinstance(llm, XaiLLM) and llm._model == "grok-4.6" and llm._http._key == VALID_KEY and llm._http._url == "https://us.api.x.ai/v1"
    assert isinstance(build_llm(Settings(groq_key="g", shop="s", shopify_token="t")), GroqLLM)


def test_build_llm_with_no_xai_key_is_a_config_error():
    with pytest.raises(XaiConfigError):
        build_llm(Settings(groq_key="", shop="s", shopify_token="t", llm_provider="xai"))


def test_settings_never_print_the_xai_key():
    s = Settings(groq_key="g", shop="s", shopify_token="t", llm_provider="xai", xai_key=VALID_KEY)
    assert VALID_KEY not in repr(s) and VALID_KEY not in str(s)


# ---- every request in a seeded random world obeys the rules ----------------------------------------------------------

def _oracle(faults):
    """The rules, written out plainly and independently: (chat requests made, sleeps, outcome)."""
    sleeps, attempt, requests = [], 0, 0
    while True:
        kind = faults[requests] if requests < len(faults) else "ok"
        requests += 1
        if kind == "ok":
            return requests, sleeps, "ok"
        if kind in ("400", "401", "500", "202"):
            return requests, sleeps, "api"
        if attempt == MAX_RETRIES:
            return requests, sleeps, "transport" if kind == "drop" else "api"
        sleeps.append(1.0 * 2 ** attempt)
        attempt += 1


FAULTS = {"429": lambda: http_error(429), "503": lambda: http_error(503), "500": lambda: http_error(500), "400": lambda: http_error(400),
          "401": lambda: http_error(401), "202": lambda: Fault(202, {"request_id": "x"}), "drop": lambda: Fault(drop=True)}


@pytest.mark.parametrize("seed", range(60))
def test_random_fault_sequences_follow_the_retry_rules(server, seed):
    rnd = random.Random(seed)
    faults = [rnd.choice(["429", "429", "503", "drop", "500", "400", "401", "202"]) for _ in range(rnd.randint(0, 4))]
    r = Rig(server)
    server.script("chat", *[FAULTS[f]() for f in faults])
    want_requests, want_sleeps, want_outcome = _oracle(faults)
    try:
        outcome = "ok" if r.ask() == server.answer else "wrong"
    except XaiTransportError:
        outcome = "transport"
    except XaiAPIError:
        outcome = "api"
    assert (len(server.of_kind("chat")), r.sleeps, outcome) == (want_requests, want_sleeps, want_outcome), (seed, faults)
