"""The IBM watsonx.ai provider against tests/watsonx_standin.py, a real HTTP server (IAM and the chat API on one port) written from
the pages saved in docs/vendor-contract/ibm-watsonx/. Nothing here is live: no IBM Cloud account, no key, no network beyond 127.0.0.1.

Every negative test is preceded by a positive control (the same request shape is answered normally), so a refusal in a test is the
guard under test and not a request the stand-in never accepted. Each guard has a mutant in tests/mutation_check.py."""
import logging
import os
import random
import threading
from pathlib import Path

import httpx
import pytest

from agent import config, watsonx_llm
from agent.config import Settings
from agent.llm import GroqLLM, LLMError, build_llm
from agent.watsonx_llm import (
    BACKOFF_BASE_SECONDS, DEFAULT_TOKEN_SECONDS, MAX_RETRIES, MAX_RETRY_AFTER_SECONDS, REFRESH_MARGIN_SECONDS, IamTokens,
    WatsonxAPIError, WatsonxAuthError, WatsonxBadResponseError, WatsonxConfigError, WatsonxEmptyError, WatsonxLLM, WatsonxQuotaError,
    WatsonxTransportError, WatsonxTruncatedError)

from watsonx_standin import (
    GRANT, OTHER_KEY, PROJECT_ID, TRACE, VALID_KEY, Fault, StandIn, chat_body, error_body, http_error)

ROOT = Path(__file__).resolve().parent.parent
SAVED = ROOT / "docs" / "vendor-contract" / "ibm-watsonx"


class Clock:
    """One fake clock shared by the client and the stand-in, so a token really expires on the server when time is moved."""

    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def server(clock):
    s = StandIn(clock=clock).start()
    yield s
    s.stop()


class Rig:
    def __init__(self, server, clock, **kw):
        self.server, self.clock, self.sleeps = server, clock, []
        self.llm = WatsonxLLM(kw.pop("key", VALID_KEY), kw.pop("project", PROJECT_ID), url=server.base, iam_url=server.iam_url,
                              sleep=self.sleeps.append, clock=clock, **kw)

    def ask(self, system="You write replies.", user="Where is my order?", **kw):
        return self.llm.complete(system, user, **kw)

    def control(self):
        """The positive control: this client, this server, a normal request, a normal answer. Leaves the token cached."""
        assert self.ask() == self.server.answer
        self.server.requests.clear()
        self.sleeps.clear()

    @property
    def token(self):
        return self.llm._tokens.current


@pytest.fixture
def rig(server, clock):
    return Rig(server, clock)


# ---- the requests on the wire ----------------------------------------------------------------------------------------

def test_a_normal_answer_is_returned_and_the_chat_request_has_the_documented_shape(rig, server):
    assert rig.ask("SYS", "USER", max_tokens=321) == "Your order has shipped."
    (req,) = server.chat_requests()
    assert req["path"] == "/ml/v1/text/chat?version=" + config.WATSONX_API_VERSION
    assert req["headers"]["authorization"] == "Bearer " + rig.token and rig.token
    assert req["headers"]["content-type"].startswith("application/json")
    assert req["body"] == {"model_id": "ibm/granite-4-h-small", "project_id": PROJECT_ID, "max_completion_tokens": 321, "temperature": 0,
                           "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]}


def test_the_token_request_has_the_documented_shape_and_the_key_goes_nowhere_else(rig, server):
    rig.ask()
    (iam,) = server.iam_requests()
    assert iam["headers"]["content-type"].startswith("application/x-www-form-urlencoded")
    assert iam["body"] == {"grant_type": GRANT, "apikey": VALID_KEY}
    for chat in server.chat_requests():
        assert VALID_KEY not in chat["raw"].decode() + chat["path"] + str(chat["headers"])


def test_the_stand_in_itself_refuses_what_the_pages_say_not_to_send(server, rig):
    # control for the tests above: the server is strict, so "no 400 happened" means something
    rig.control()
    good = {"model_id": "ibm/granite-4-h-small", "project_id": PROJECT_ID, "messages": [{"role": "user", "content": "x"}]}
    url = server.base + "/ml/v1/text/chat"
    auth = {"Authorization": "Bearer " + rig.token}
    ok = httpx.post(url, params={"version": config.WATSONX_API_VERSION}, headers=auth, json=good)
    assert ok.status_code == 200
    bad_role = dict(good, messages=[{"role": "User", "content": "x"}])
    assert httpx.post(url, params={"version": config.WATSONX_API_VERSION}, headers=auth, json=bad_role).status_code == 400
    assert httpx.post(url, params={"version": "2020-01-01"}, headers=auth, json=good).status_code == 400
    assert httpx.post(url, params={"version": config.WATSONX_API_VERSION}, headers=auth, json=dict(good, space_id="s")).status_code == 400
    assert httpx.post(url, params={"version": config.WATSONX_API_VERSION}, headers=auth, json=dict(good, model_id="x/y")).status_code == 400
    assert httpx.post(url, params={"version": config.WATSONX_API_VERSION}, headers={"Authorization": "Bearer nope"}, json=good).status_code == 401
    assert httpx.post(server.iam_url, json={"apikey": VALID_KEY}).status_code == 400  # IAM wants a form, not JSON
    assert httpx.post(server.iam_url, data={"grant_type": "password", "apikey": VALID_KEY}).status_code == 400


def test_no_tools_and_no_deprecated_token_field_are_sent(rig, server):
    rig.ask()
    body = server.chat_requests()[0]["body"]
    assert "tools" not in body and "tool_choice" not in body and "max_tokens" not in body and "space_id" not in body


def test_the_default_version_model_and_urls_are_the_ones_on_the_saved_pages():
    chat = (SAVED / "chat-api.txt").read_text(encoding="utf-8")
    creds = (SAVED / "credentials-api-key-iam-token.txt").read_text(encoding="utf-8")
    assert "/ml/v1/text/chat?version=" + config.WATSONX_API_VERSION in chat
    assert config.WATSONX_DEFAULT_MODEL_ID == "ibm/granite-4-h-small" and config.WATSONX_DEFAULT_MODEL_ID in (SAVED / "chat-api.txt").read_text(encoding="utf-8")
    assert config.WATSONX_IAM_URL in creds and GRANT in creds
    assert config.WATSONX_DEFAULT_URL == "https://us-south.ml.cloud.ibm.com"


def test_the_model_id_and_project_id_from_the_settings_are_sent(server, clock):
    r = Rig(server, clock, model_id="openai/gpt-oss-120b", project="proj-77")
    r.ask()
    assert server.chat_requests()[0]["body"]["model_id"] == "openai/gpt-oss-120b" and server.chat_requests()[0]["body"]["project_id"] == "proj-77"


@pytest.mark.parametrize("key", ["", "short", "has space in it 1234", "line\r\nbreak-1234567", "tab\tinside-1234567", "café-key-12345678"])
def test_a_malformed_key_is_refused_before_any_request_and_never_echoed(key, server):
    with pytest.raises(WatsonxConfigError) as e:
        WatsonxLLM(key, PROJECT_ID, url=server.base, iam_url=server.iam_url)
    assert key.strip() not in str(e.value) or not key.strip()
    assert server.requests == []


@pytest.mark.parametrize("project", ["", "has space", "x" * 65, "proj\r\nid", "é"])
def test_a_malformed_project_id_is_refused_before_any_request(project, server):
    with pytest.raises(WatsonxConfigError, match="WATSONX_PROJECT_ID"):
        WatsonxLLM(VALID_KEY, project, url=server.base, iam_url=server.iam_url)
    assert server.requests == []


@pytest.mark.parametrize("model", ["", "has space", "x" * 101, "m\r\nodel", "ibm/é"])
def test_a_malformed_model_id_is_refused_before_any_request(model, server):
    with pytest.raises(WatsonxConfigError, match="WATSONX_MODEL_ID"):
        WatsonxLLM(VALID_KEY, PROJECT_ID, model, url=server.base, iam_url=server.iam_url)
    assert server.requests == []


def test_a_token_or_key_is_never_sent_over_plain_http_to_another_machine(server):
    WatsonxLLM(VALID_KEY, PROJECT_ID, url="https://us-south.ml.cloud.ibm.com/", iam_url="https://iam.cloud.ibm.com/identity/token")  # control
    WatsonxLLM(VALID_KEY, PROJECT_ID, url=server.base, iam_url=server.iam_url)  # control: loopback http is the stand-in
    for bad in ("http://us-south.ml.cloud.ibm.com", "ftp://x.example", "us-south.ml.cloud.ibm.com", "", "https://", "http://127.0.0.1.evil.example"):
        with pytest.raises(WatsonxConfigError, match="WATSONX_URL"):
            WatsonxLLM(VALID_KEY, PROJECT_ID, url=bad)
        with pytest.raises(WatsonxConfigError, match="IAM URL"):
            WatsonxLLM(VALID_KEY, PROJECT_ID, iam_url=bad)


def test_a_trailing_slash_is_dropped_from_an_https_url_too():
    assert WatsonxLLM(VALID_KEY, PROJECT_ID, url="https://us-south.ml.cloud.ibm.com/")._url == "https://us-south.ml.cloud.ibm.com"
    assert WatsonxLLM(VALID_KEY, PROJECT_ID, iam_url="https://iam.cloud.ibm.com/identity/token/")._tokens._url == "https://iam.cloud.ibm.com/identity/token"


def test_a_trailing_slash_on_the_url_does_not_double_the_path(server, clock):
    llm = WatsonxLLM(VALID_KEY, PROJECT_ID, url=server.base + "/", iam_url=server.iam_url, sleep=lambda s: None, clock=clock)
    assert llm.complete("s", "u") == server.answer
    assert server.chat_requests()[0]["path"].startswith("/ml/v1/text/chat?")
    assert "//" not in server.chat_requests()[0]["request_line"].split(" ")[1]  # the server rewrites a leading // to /, so look at the raw line


@pytest.mark.parametrize("bad", [0, -5, True, 1.5, "10", None])
def test_max_tokens_must_be_a_positive_integer(rig, server, bad):
    rig.control()
    with pytest.raises(WatsonxConfigError):
        rig.ask(max_tokens=bad)
    assert server.requests == []


# ---- the token: fetch, reuse, refresh --------------------------------------------------------------------------------

def test_the_token_is_fetched_once_and_reused_for_every_call(rig, server):
    for _ in range(5):
        assert rig.ask() == server.answer
    assert len(server.iam_requests()) == 1 and server.tokens_issued == 1 and rig.llm._tokens.fetches == 1
    assert len({r["headers"]["authorization"] for r in server.chat_requests()}) == 1


def test_the_token_is_not_fetched_until_it_is_needed(rig, server):
    assert server.requests == []  # building the client made no request


def test_a_token_is_renewed_inside_the_refresh_margin_even_though_the_server_would_still_accept_it(rig, server, clock):
    rig.control()
    first = rig.token
    clock.advance(3600 - REFRESH_MARGIN_SECONDS - 1)  # one second before the renewal point
    rig.ask()
    assert rig.token == first and server.tokens_issued == 1
    clock.advance(1)  # now exactly at it; the server's own expiry is still 120 s away
    rig.ask()
    assert rig.token != first and server.tokens_issued == 2
    assert server.chat_requests(valid=False) == server.chat_requests(valid=True)  # no request was ever refused: renewal came first
    assert server.chat_requests()[-1]["headers"]["authorization"] == "Bearer " + rig.token


def test_a_renewed_token_is_then_reused_again(rig, server, clock):
    rig.control()
    clock.advance(3600)
    for _ in range(3):
        rig.ask()
    assert server.tokens_issued == 2


def test_the_expiry_comes_from_expires_in_when_it_is_there(server, clock):
    server.expires_in = 600  # a short token: the renewal point is at 480 s
    r = Rig(server, clock)
    r.control()
    clock.advance(479)
    r.ask()
    assert server.tokens_issued == 1
    clock.advance(1)
    r.ask()
    assert server.tokens_issued == 2


def test_the_expiry_comes_from_the_expiration_epoch_when_expires_in_is_missing(server, clock):
    server.expires_in = None  # the stand-in then issues a 3600 s token and reports only "expiration"
    r = Rig(server, clock)
    r.control()
    clock.advance(3600 - REFRESH_MARGIN_SECONDS - 1)
    r.ask()
    assert server.tokens_issued == 1
    clock.advance(1)
    r.ask()
    assert server.tokens_issued == 2


def _tokens(server, clock, *bodies):
    server.script_iam(*[Fault(200, b) for b in bodies])
    return IamTokens(VALID_KEY, url=server.iam_url, clock=clock, sleep=lambda s: None)


def test_expires_in_wins_over_the_expiration_epoch_when_they_disagree(server, clock):
    # a clock that disagrees with IBM's must not make a fresh token look expired: the relative figure is trusted
    far_past = int(clock.now) - 99999
    t = _tokens(server, clock, {"access_token": "tok-a", "expires_in": 3600, "expiration": far_past}, {"access_token": "tok-b", "expires_in": 3600})
    assert t.get() == "tok-a" and t.get() == "tok-a" and t.fetches == 1


def test_a_token_with_only_an_expiration_epoch_is_renewed_by_that_epoch_and_not_by_the_default_hour(server, clock):
    t = _tokens(server, clock, {"access_token": "tok-a", "expiration": int(clock.now) + 600}, {"access_token": "tok-b"})
    assert t.get() == "tok-a"
    clock.advance(600 - REFRESH_MARGIN_SECONDS - 1)
    assert t.get() == "tok-a"
    clock.advance(1)
    assert t.get() == "tok-b"


def test_whitespace_around_the_access_token_is_not_sent_in_the_header(server, clock):
    t = _tokens(server, clock, {"access_token": "  tok-a\n", "expires_in": 3600})
    assert t.get() == "tok-a"


def test_an_iam_answer_with_no_expiry_at_all_is_trusted_for_the_documented_hour(server, clock):
    t = _tokens(server, clock, {"access_token": "tok-a"}, {"access_token": "tok-b"})
    assert t.get() == "tok-a"
    clock.advance(DEFAULT_TOKEN_SECONDS - REFRESH_MARGIN_SECONDS - 1)
    assert t.get() == "tok-a"
    clock.advance(1)
    assert t.get() == "tok-b"


@pytest.mark.parametrize("fields", [{"expires_in": "soon"}, {"expires_in": -5}, {"expires_in": 0}, {"expires_in": True}, {"expires_in": float("nan")}, {"expires_in": float("inf")},
                                    {"expires_in": None, "expiration": "tomorrow"}, {"expiration": -1}, {"expiration": True}],
                         ids=["str", "negative", "zero", "bool", "nan", "inf", "str-expiration", "negative-expiration", "bool-expiration"])
def test_an_unusable_expiry_falls_back_to_the_documented_hour(server, clock, fields):
    t = _tokens(server, clock, {"access_token": "tok-a", **fields}, {"access_token": "tok-b"})
    t.get()
    clock.advance(DEFAULT_TOKEN_SECONDS - REFRESH_MARGIN_SECONDS - 1)
    assert t.get() == "tok-a"
    clock.advance(1)
    assert t.get() == "tok-b"


@pytest.mark.parametrize("body", [{"token_type": "Bearer"}, {"access_token": ""}, {"access_token": "   "}, {"access_token": 7}, ["list"], "string"],
                         ids=["missing", "empty", "blank", "number", "list", "string"])
def test_an_iam_answer_without_a_usable_token_is_a_named_error(server, clock, body):
    good = _tokens(server, clock, {"access_token": "tok-a", "expires_in": 3600})
    assert good.get() == "tok-a"  # control: the same path with a good body works
    bad = _tokens(server, clock, body)
    with pytest.raises(WatsonxBadResponseError):
        bad.get()


def test_an_iam_200_that_is_not_json_is_a_named_error(server, clock):
    server.script_iam(Fault(raw=b"<html>gateway</html>"))
    with pytest.raises(WatsonxBadResponseError):
        IamTokens(VALID_KEY, url=server.iam_url, clock=clock, sleep=lambda s: None).get()


def test_a_401_from_the_chat_api_refreshes_the_token_once_and_retries_once(rig, server):
    rig.control()
    first = rig.token
    server.expire_tokens()  # the server forgets the token mid-session: the client's own clock says it is fine
    assert rig.ask() == server.answer
    assert rig.token != first and server.tokens_issued == 2
    refused, answered = server.chat_requests(valid=False)
    assert refused["headers"]["authorization"] == "Bearer " + first and answered["headers"]["authorization"] == "Bearer " + rig.token
    assert rig.sleeps == []  # a refresh is not a backoff


def test_a_second_401_is_raised_and_not_retried_again(rig, server):
    rig.control()
    server.script_chat(http_error(401, "authentication_token_expired", "scripted"), http_error(401, "authentication_token_expired", "scripted again"),
                       Fault())
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert e.value.status == 401 and e.value.code == "authentication_token_expired" and "scripted again" in str(e.value)
    assert len(server.chat_requests()) == 2 and server.tokens_issued == 2  # the third, good answer was never asked for


def test_a_401_refresh_does_not_spend_the_retry_budget(rig, server):
    rig.control()
    server.script_chat(http_error(401, "authentication_token_expired"), *[http_error(503, "service_unavailable")] * MAX_RETRIES)
    assert rig.ask() == server.answer
    assert len(server.chat_requests()) == 2 + MAX_RETRIES and rig.sleeps == [1.0, 2.0]


def test_a_refresh_after_a_401_is_allowed_again_on_the_next_call(rig, server):
    rig.control()
    for _ in range(2):
        server.expire_tokens()
        assert rig.ask() == server.answer
    assert server.tokens_issued == 3


def test_a_stale_401_does_not_throw_away_a_token_another_thread_already_renewed(server, clock):
    t = IamTokens(VALID_KEY, url=server.iam_url, clock=clock, sleep=lambda s: None)
    old = t.get()
    t.invalidate(old)
    fresh = t.get()
    t.invalidate(old)  # a second caller that also got a 401 for the old token
    assert t.get() == fresh and t.fetches == 2


def test_a_wrong_key_stops_at_the_token_request_and_names_the_vendors_code(server, clock):
    r = Rig(server, clock, key=OTHER_KEY)
    with pytest.raises(WatsonxAuthError) as e:
        r.ask()
    assert e.value.status == 400 and e.value.code == "BXNIM0415E" and "Provided API key could not be found" in str(e.value)
    assert "IAM token request refused: HTTP 400 BXNIM0415E:" in str(e.value)
    assert OTHER_KEY not in str(e.value) and server.chat_requests(valid=False) == [] and r.sleeps == []


def test_a_transient_iam_failure_is_retried_and_a_persistent_one_is_reported(server, clock):
    r = Rig(server, clock)
    server.script_iam(http_error(503, "service_unavailable"), Fault(drop=True))
    assert r.ask() == server.answer and r.sleeps == [1.0, 2.0]
    r2 = Rig(server, clock)
    server.script_iam(*[http_error(503, "service_unavailable", "iam down")] * (MAX_RETRIES + 1))
    with pytest.raises(WatsonxAuthError) as e:
        r2.ask()
    assert e.value.status == 503 and "IAM token request refused" in str(e.value) and "iam down" in str(e.value)
    assert r2.sleeps == [1.0, 2.0]  # the same doubling backoff as the chat call
    r3 = Rig(server, clock)
    server.script_iam(*[Fault(drop=True)] * (MAX_RETRIES + 1))
    with pytest.raises(WatsonxTransportError) as e3:
        r3.ask()
    assert "IAM token request failed" in str(e3.value) and r3.sleeps == [1.0, 2.0]


def test_eight_threads_starting_together_fetch_one_token(rig, server):
    server.script_iam(Fault(delay=0.4))  # the first fetch is slow, so the other seven arrive while it is in flight
    answers, errors = [], []

    def run():
        try:
            answers.append(rig.ask())
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert errors == [] and answers == [server.answer] * 8
    assert server.tokens_issued == 1


# ---- the answer ------------------------------------------------------------------------------------------------------

# (finish_reason, content, exception): stop and length are on the saved pages; time_limit, cancelled and error are the other values
# java-sdk-error-handling.txt names; tool_calls is what a tool-calling answer carries, which this client never asked for
FINISH_TABLE = [
    ("stop", "fine", None),
    ("STOP", "fine", None),
    ("stop", "", WatsonxEmptyError),
    ("stop", "   \n", WatsonxEmptyError),
    ("stop", None, WatsonxEmptyError),
    ("stop", [{"type": "text", "text": "a list is not the documented answer"}], WatsonxEmptyError),
    ("length", "", WatsonxTruncatedError),
    ("length", None, WatsonxTruncatedError),
    ("length", "The order is on its way and should arr", WatsonxTruncatedError),
    ("LENGTH", "cut", WatsonxTruncatedError),
    ("time_limit", "partial", WatsonxTruncatedError),
    ("time_limit", None, WatsonxTruncatedError),
    ("tool_calls", None, WatsonxBadResponseError),
    ("tool_calls", "half", WatsonxBadResponseError),
    ("cancelled", "half", WatsonxBadResponseError),
    ("error", "half", WatsonxBadResponseError),
    (None, "half", WatsonxBadResponseError),
    ("a_value_added_later", "half", WatsonxBadResponseError),
]


@pytest.mark.parametrize("finish,content,exc", FINISH_TABLE, ids=["%s-%d-%s" % (f, i, e.__name__ if e else "ok") for i, (f, _, e) in enumerate(FINISH_TABLE)])
def test_every_finish_reason_is_a_reply_or_a_named_error_never_an_empty_string(rig, server, finish, content, exc):
    rig.control()
    server.script_chat(Fault(200, chat_body(content, finish)))
    if exc is None:
        assert rig.ask() == "fine"
        return
    with pytest.raises(exc) as e:
        rig.ask()
    assert isinstance(e.value, LLMError) and str(e.value)  # the agent catches LLMError and hands the case to a person


def test_a_truncated_error_says_whether_any_text_arrived(rig, server):
    rig.control()
    server.script_chat(Fault(200, chat_body(None, "length")), Fault(200, chat_body("cut o", "length")))
    with pytest.raises(WatsonxTruncatedError) as none:
        rig.ask()
    with pytest.raises(WatsonxTruncatedError) as some:
        rig.ask()
    assert none.value.has_text is False and some.value.has_text is True


def test_reasoning_content_is_not_part_of_the_answer(rig, server):
    # chat-api.txt: a reasoning model answers with message.content and message.reasoning_content
    rig.control()
    body = chat_body("The kilogram is heavier.")
    body["choices"][0]["message"]["reasoning_content"] = "We have a user asking a simple question..."
    server.script_chat(Fault(200, body))
    assert rig.ask() == "The kilogram is heavier."


def test_the_reply_is_trimmed(rig, server):
    rig.control()
    server.script_chat(Fault(200, chat_body("  hello there \n")))
    assert rig.ask() == "hello there"


@pytest.mark.parametrize("fault", [
    Fault(raw=b"<html>Gateway time-out</html>"),
    Fault(raw=b"not json at all"),
    Fault(body=["a", "list"]),
    Fault(body="a string"),
    Fault(body=error_body("internal_error", "mid flight")),
    Fault(body={"id": "x", "choices": []}),
    Fault(body={"id": "x", "choices": "none"}),
    Fault(body={"id": "x", "choices": ["not a dict"]}),
    Fault(body={"id": "x", "choices": [{"index": 0, "finish_reason": "stop"}]}),
    Fault(body={"id": "x", "choices": [{"index": 0, "message": "text", "finish_reason": "stop"}]}),
], ids=["html", "text", "list", "string", "error-on-200", "empty-choices", "choices-not-list", "choice-not-dict", "no-message", "message-not-dict"])
def test_a_200_that_is_not_a_chat_answer_is_a_named_error(rig, server, fault):
    rig.control()
    server.script_chat(fault)
    with pytest.raises((WatsonxBadResponseError, WatsonxEmptyError)):
        rig.ask()


def test_usage_is_reported_to_the_callback_as_numbers_only(server, clock):
    seen = []
    r = Rig(server, clock, on_usage=seen.append)
    r.ask()
    assert seen == [{"prompt_tokens": 87, "completion_tokens": 12, "total_tokens": 99}]
    server.script_chat(Fault(200, chat_body("ok", usage={"prompt_tokens": float("nan"), "completion_tokens": float("inf"), "total_tokens": 5, "extra": 1})),
                       Fault(200, chat_body("ok", usage="n/a")), Fault(200, chat_body("ok", usage=None)))
    for _ in range(3):
        r.ask()
    assert seen[1:] == [{"total_tokens": 5}]  # the others carried nothing usable, so the callback was not called for them
    WatsonxLLM(VALID_KEY, PROJECT_ID, url=server.base, iam_url=server.iam_url, clock=clock).complete("s", "u")  # no callback: no error


# ---- non-200 answers: the status matrix ------------------------------------------------------------------------------

# (status, error code, retried?). The codes for 400, 403 and 429-quota are java-sdk-error-handling.txt's; 404, 409, the generic 429
# and the 5xx names are this file's own labels (the saved pages give no body for them)
ERROR_TABLE = [
    (400, "invalid_input_argument", False), (400, "model_not_supported", False), (400, "json_validation_error", False),
    (403, "authorization_rejected", False), (403, "user_authorization_failed", False), (404, "not_found", False),
    (409, "conflict", False), (418, "teapot", False),
    (429, "rate_limit_exceeded", True), (500, "internal_error", True), (502, "bad_gateway", True), (503, "service_unavailable", True),
    (504, "gateway_timeout", True), (520, "unknown_error", True),
]


@pytest.mark.parametrize("status,code,retried", ERROR_TABLE, ids=["%d-%s" % (s, c) for s, c, _ in ERROR_TABLE])
def test_each_status_is_named_and_only_the_transient_ones_are_retried(rig, server, status, code, retried):
    rig.control()
    server.script_chat(*[http_error(status, code, "scripted %s" % code)] * (MAX_RETRIES + 1))
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    err = e.value
    assert (err.status, err.code, err.trace) == (status, code, TRACE)
    assert "HTTP %d %s: scripted %s" % (status, code, code) in str(err) and TRACE in str(err)
    assert len(server.chat_requests()) == (MAX_RETRIES + 1 if retried else 1)
    assert len(rig.sleeps) == (MAX_RETRIES if retried else 0)


def test_the_token_quota_429_is_never_retried(rig, server):
    rig.control()
    server.script_chat(*[http_error(429, "token_quota_reached", "The token quota has been reached")] * 3)
    with pytest.raises(WatsonxQuotaError) as e:
        rig.ask()
    assert e.value.status == 429 and e.value.code == "token_quota_reached" and len(server.chat_requests()) == 1 and rig.sleeps == []


def test_a_429_with_another_code_is_an_ordinary_rate_limit(rig, server):
    rig.control()
    server.script_chat(http_error(429, "something_else"), Fault())
    assert rig.ask() == server.answer and len(server.chat_requests()) == 2


def test_the_quota_code_on_another_status_is_not_a_quota_error(rig, server):
    rig.control()
    server.script_chat(http_error(503, "token_quota_reached"), Fault())
    assert rig.ask() == server.answer  # still a plain transient 5xx: only a 429 with the code is the quota


def test_an_error_with_no_error_object_still_names_the_status(rig, server):
    rig.control()
    server.script_chat(Fault(400, raw=b"<html>Bad Request</html>"))
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert "HTTP 400: no error object in the response" in str(e.value) and e.value.code == "" and e.value.trace == ""


# ---- retries ---------------------------------------------------------------------------------------------------------

def test_a_429_with_retry_after_waits_that_long_and_then_succeeds(rig, server):
    rig.control()
    server.script_chat(http_error(429, "rate_limit_exceeded", headers={"retry-after": "7"}))
    assert rig.ask() == server.answer
    assert rig.sleeps == [7.0] and len(server.chat_requests()) == 2


def test_a_retry_after_shorter_than_the_backoff_does_not_shorten_it(rig, server):
    rig.control()
    server.script_chat(http_error(429, "rate_limit_exceeded", headers={"retry-after": "0"}))
    assert rig.ask() == server.answer
    assert rig.sleeps == [BACKOFF_BASE_SECONDS]


def test_a_503_is_retried_with_a_doubling_backoff(rig, server):
    rig.control()
    server.script_chat(http_error(503, "service_unavailable"), http_error(503, "service_unavailable"))
    assert rig.ask() == server.answer
    assert rig.sleeps == [1.0, 2.0] and len(server.chat_requests()) == 3


def test_retries_stop_at_the_limit_and_the_last_error_is_raised(rig, server):
    rig.control()
    server.script_chat(*[http_error(503, "service_unavailable", "still busy")] * 5)
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert e.value.status == 503 and len(server.chat_requests()) == 3 and rig.sleeps == [1.0, 2.0]


def test_max_retries_zero_means_one_attempt(server, clock):
    r = Rig(server, clock, max_retries=0)
    r.control()
    server.script_chat(http_error(503, "service_unavailable"))
    with pytest.raises(WatsonxAPIError):
        r.ask()
    assert len(server.chat_requests()) == 1 and r.sleeps == []


def test_a_retry_after_longer_than_a_shopper_will_wait_fails_fast(rig, server):
    rig.control()
    server.script_chat(http_error(429, "rate_limit_exceeded", headers={"retry-after": str(int(MAX_RETRY_AFTER_SECONDS) + 1)}))
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert e.value.retry_after == MAX_RETRY_AFTER_SECONDS + 1 and rig.sleeps == [] and len(server.chat_requests()) == 1


def test_a_retry_after_at_the_limit_is_still_waited_for(rig, server):
    rig.control()
    server.script_chat(http_error(429, "rate_limit_exceeded", headers={"retry-after": str(int(MAX_RETRY_AFTER_SECONDS))}))
    assert rig.ask() == server.answer and rig.sleeps == [MAX_RETRY_AFTER_SECONDS]


@pytest.mark.parametrize("header", ["soon", "-5", "nan", "inf", "", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_an_unusable_retry_after_falls_back_to_the_backoff(rig, server, header):
    rig.control()
    server.script_chat(http_error(429, "rate_limit_exceeded", headers={"retry-after": header}))
    assert rig.ask() == server.answer and rig.sleeps == [BACKOFF_BASE_SECONDS]


def test_a_dropped_connection_is_retried_then_reported(rig, server):
    rig.control()
    server.script_chat(Fault(drop=True), Fault())
    assert rig.ask() == server.answer and rig.sleeps == [1.0]
    server.requests.clear()
    server.script_chat(Fault(drop=True), Fault(drop=True), Fault(drop=True), Fault())
    with pytest.raises(WatsonxTransportError) as e:
        rig.ask()
    assert "RemoteProtocolError" in str(e.value) and len(server.chat_requests()) == 3


def test_a_refused_connection_is_a_named_error_and_is_retried(monkeypatch, clock):
    # a real refused connect takes about 2 s on Windows, so the client is swapped for one whose transport refuses at once
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    monkeypatch.setattr(watsonx_llm, "_client", lambda: httpx.Client(transport=httpx.MockTransport(refuse)))
    sleeps = []
    with pytest.raises(WatsonxTransportError) as e:
        WatsonxLLM(VALID_KEY, PROJECT_ID, url="http://127.0.0.1:9", iam_url="http://127.0.0.1:9/identity/token", sleep=sleeps.append, clock=clock).complete("s", "u")
    assert "ConnectError" in str(e.value) and sleeps == [1.0, 2.0]


def test_a_read_timeout_is_a_named_error_and_is_not_repeated(server, clock):
    r = Rig(server, clock, timeout=0.2)
    r.control()
    server.script_chat(Fault(delay=0.6))
    with pytest.raises(WatsonxTransportError) as e:
        r.ask()
    assert "ReadTimeout" in str(e.value) and r.sleeps == [] and len(server.chat_requests()) == 1


def test_a_read_timeout_while_fetching_the_token_is_not_repeated_either(server, clock):
    server.script_iam(Fault(delay=0.6))
    llm = WatsonxLLM(VALID_KEY, PROJECT_ID, url=server.base, iam_url=server.iam_url, sleep=lambda s: pytest.fail("slept"), clock=clock)
    llm._tokens._timeout = 0.2
    with pytest.raises(WatsonxTransportError, match="IAM token request failed"):
        llm.complete("s", "u")


# ---- the key, the token and the vendor's words -----------------------------------------------------------------------

def test_neither_the_key_nor_the_token_appears_in_any_error_or_log(rig, server, caplog, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.DEBUG)
    rig.control()
    token, echo = rig.token, "bad credential %s and token %s were refused" % (VALID_KEY, rig.token)
    faults = [http_error(403, "authorization_rejected", echo), http_error(400, "invalid_input_argument", echo),
              http_error(500, "internal_error", echo), Fault(raw=("<html>%s %s</html>" % (VALID_KEY, token)).encode()),
              Fault(400, error_body("invalid_input_argument", "x" * 5000 + VALID_KEY + token)),
              Fault(200, chat_body("hi", VALID_KEY + token)), Fault(200, chat_body("hi", token)),
              http_error(418, VALID_KEY + "_code", echo), http_error(418, token, echo),
              Fault(400, error_body("invalid_input_argument", "echoed trace", trace=VALID_KEY)),
              Fault(400, error_body("invalid_input_argument", "echoed trace", trace=token))]
    for f in faults:
        server.script_chat(*([f] * (MAX_RETRIES + 1)))
        with pytest.raises(LLMError) as e:
            rig.ask()
        for secret in (VALID_KEY, token):
            assert secret not in str(e.value) and secret not in repr(e.value) and secret not in repr(e.value.__dict__)
        server.chat_faults.clear()
    assert VALID_KEY not in caplog.text and token not in caplog.text
    out = capsys.readouterr()
    assert VALID_KEY not in out.out + out.err and token not in out.out + out.err
    assert list(tmp_path.iterdir()) == []  # nothing was written next to the process


def test_the_key_never_appears_in_an_iam_error(server, clock, caplog):
    caplog.set_level(logging.DEBUG)
    echo = "api key %s is not right" % VALID_KEY
    faults = [Fault(400, {"errorCode": "BXNIM0415E", "errorMessage": echo}), Fault(500, {"errorCode": VALID_KEY, "errorMessage": echo}),
              Fault(403, raw=("<html>%s</html>" % VALID_KEY).encode())]
    for f in faults:
        server.script_iam(*([f] * (MAX_RETRIES + 1)))
        r = Rig(server, clock)
        with pytest.raises(WatsonxAuthError) as e:
            r.ask()
        assert VALID_KEY not in str(e.value) and VALID_KEY not in repr(e.value) and VALID_KEY not in repr(e.value.__dict__)
        server.iam_faults.clear()
    assert VALID_KEY not in caplog.text


def test_an_echoed_key_is_masked_not_dropped(rig, server):
    rig.control()
    server.script_chat(http_error(403, "authorization_rejected", "Invalid key %s." % VALID_KEY))
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert "Invalid key [secret]." in str(e.value)


def test_an_echoed_token_is_masked_too(rig, server):
    rig.control()
    server.script_chat(http_error(403, "authorization_rejected", "Rejected %s." % rig.token))
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert "Rejected [secret]." in str(e.value)


def test_a_long_vendor_message_is_truncated(rig, server):
    rig.control()
    server.script_chat(http_error(400, "invalid_input_argument", "word " * 400))
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert len(str(e.value)) < 330 and "word word" in str(e.value)


def test_a_long_vendor_code_is_truncated(rig, server):
    rig.control()
    server.script_chat(http_error(400, "c" * 500, "m"))
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert len(e.value.code) == 60


def test_a_body_that_is_not_the_error_object_is_not_echoed(rig, server):
    rig.control()
    server.script_chat(*[Fault(502, raw=b"<html><body>SECRET-INTERNAL-PAGE upstream down</body></html>")] * 3)
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert "SECRET-INTERNAL-PAGE" not in str(e.value) and "HTTP 502" in str(e.value)


def test_a_trace_that_is_not_an_id_is_left_out(rig, server):
    rig.control()
    server.script_chat(http_error(400, "invalid_input_argument", trace="x y\tz <b>"))
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert e.value.trace == "" and "trace" not in str(e.value)


@pytest.mark.parametrize("body", [["a", "list"], "a string", 7, None], ids=["list", "string", "number", "null"])
def test_an_error_status_whose_body_is_not_an_object_still_names_the_status(rig, server, body):
    rig.control()
    server.script_chat(Fault(400, body))
    with pytest.raises(WatsonxAPIError) as e:
        rig.ask()
    assert "HTTP 400: no error object in the response" in str(e.value)


def test_the_errors_list_may_be_empty_or_odd_without_breaking_the_message(rig, server):
    rig.control()
    for body in ({"errors": [], "trace": TRACE}, {"errors": "none"}, {"errors": [7]}, {"trace": TRACE}):
        server.script_chat(Fault(400, body))
        with pytest.raises(WatsonxAPIError) as e:
            rig.ask()
        assert "HTTP 400: no error object in the response" in str(e.value)


def test_settings_never_print_the_watsonx_key():
    s = Settings(groq_key="g", shop="s", shopify_token="t", llm_provider="watsonx", watsonx_key=VALID_KEY, watsonx_project_id=PROJECT_ID)
    assert VALID_KEY not in repr(s) and VALID_KEY not in str(s)


# ---- every request in a seeded random world obeys the rules ----------------------------------------------------------

def _oracle(faults):
    """The rules, written out plainly and independently: (chat requests made, sleeps, tokens issued, outcome)."""
    sleeps, attempt, refreshed, issued, requests = [], 0, False, 1, 0
    while True:
        kind, arg = faults[requests] if requests < len(faults) else ("ok", None)
        requests += 1
        if kind == "ok":
            return requests, sleeps, issued, "ok"
        if kind == "401":
            if refreshed:
                return requests, sleeps, issued, "api"
            refreshed, attempt, issued = True, 0, issued + 1
            continue
        if kind in ("400", "quota"):
            return requests, sleeps, issued, "api"
        if attempt == MAX_RETRIES:
            return requests, sleeps, issued, "transport" if kind == "drop" else "api"
        wait = 1.0 * 2 ** attempt
        if kind in ("429", "503", "500") and arg is not None:
            if arg > 30:
                return requests, sleeps, issued, "api"
            wait = max(wait, arg)
        sleeps.append(wait)
        attempt += 1


def _fault(kind, arg):
    if kind == "drop":
        return Fault(drop=True)
    headers = {"retry-after": str(arg)} if arg is not None else {}
    if kind == "quota":
        return http_error(429, "token_quota_reached")
    status, code = {"400": (400, "invalid_input_argument"), "401": (401, "authentication_token_expired"), "429": (429, "rate_limit_exceeded"),
                    "503": (503, "service_unavailable"), "500": (500, "internal_error")}[kind]
    return http_error(status, code, headers=headers)


@pytest.mark.parametrize("seed", range(40))
def test_retry_and_refresh_rules_hold_over_random_fault_sequences(server, clock, seed):
    rng = random.Random(seed)
    rig = Rig(server, clock)
    rig.control()
    faults = []
    for _ in range(rng.randint(0, 5)):
        kind = rng.choice(["429", "503", "500", "drop", "400", "401", "quota", "429", "503", "401"])
        arg = rng.choice([None, 0, 0.5, 2, 5, 29, 30, 31, 90]) if kind in ("429", "503", "500") else None
        faults.append((kind, arg))
    server.script_chat(*[_fault(k, a) for k, a in faults])
    requests, sleeps, issued, outcome = _oracle(faults)
    try:
        assert rig.ask() == server.answer
        result = "ok"
    except WatsonxTransportError:
        result = "transport"
    except WatsonxAPIError:
        result = "api"
    assert (len(server.chat_requests()), rig.sleeps, server.tokens_issued, result) == (requests, sleeps, issued, outcome), faults


# ---- choosing the provider -------------------------------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    """No saved secret file and none of the variables: the real Operations/.secrets folder is never read by a test."""
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    for name in ("LLM_PROVIDER", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "WATSONX_API_KEY", "WATSONX_PROJECT_ID", "WATSONX_URL", "WATSONX_MODEL_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHOPIFY_SHOP", "demo.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_x")
    return monkeypatch


def test_the_watsonx_provider_needs_its_key_and_project_and_not_the_groq_key(env):
    env.setenv("LLM_PROVIDER", "watsonx")
    with pytest.raises(RuntimeError) as e:
        config.load_settings()
    assert "WATSONX_API_KEY" in str(e.value) and "WATSONX_PROJECT_ID" in str(e.value) and "GROQ_API_KEY" not in str(e.value)
    env.setenv("WATSONX_API_KEY", VALID_KEY)
    with pytest.raises(RuntimeError) as e2:
        config.load_settings()
    assert "WATSONX_PROJECT_ID" in str(e2.value) and "WATSONX_API_KEY" not in str(e2.value)
    env.setenv("WATSONX_PROJECT_ID", PROJECT_ID)
    s = config.load_settings()
    assert s.llm_provider == "watsonx" and isinstance(build_llm(s), WatsonxLLM)


def test_the_provider_switch_picks_watsonx_and_not_groq_or_claude(env, server, clock):
    env.setenv("LLM_PROVIDER", "watsonx")
    env.setenv("WATSONX_API_KEY", VALID_KEY)
    env.setenv("WATSONX_PROJECT_ID", PROJECT_ID)
    env.setenv("WATSONX_URL", server.base)
    env.setattr(config, "WATSONX_IAM_URL", server.iam_url)
    llm = build_llm(config.load_settings())
    assert type(llm) is WatsonxLLM and not isinstance(llm, GroqLLM)
    assert llm.complete("s", "u") == server.answer
    (req,) = server.chat_requests()
    assert req["body"]["model_id"] == "ibm/granite-4-h-small" and req["body"]["project_id"] == PROJECT_ID


def test_url_and_model_come_from_the_environment_and_default_to_dallas_and_granite(env):
    env.setenv("LLM_PROVIDER", "watsonx")
    env.setenv("WATSONX_API_KEY", VALID_KEY)
    env.setenv("WATSONX_PROJECT_ID", PROJECT_ID)
    s = config.load_settings()
    assert (s.watsonx_url, s.watsonx_model_id) == ("https://us-south.ml.cloud.ibm.com", "ibm/granite-4-h-small")
    env.setenv("WATSONX_URL", " https://eu-de.ml.cloud.ibm.com ")
    env.setenv("WATSONX_MODEL_ID", " openai/gpt-oss-120b\n")
    s = config.load_settings()
    assert (s.watsonx_url, s.watsonx_model_id) == ("https://eu-de.ml.cloud.ibm.com", "openai/gpt-oss-120b")
    env.setenv("WATSONX_URL", "")
    env.setenv("WATSONX_MODEL_ID", "")
    s = config.load_settings()
    assert (s.watsonx_url, s.watsonx_model_id) == ("https://us-south.ml.cloud.ibm.com", "ibm/granite-4-h-small")


def test_the_url_and_model_from_the_settings_reach_the_client(env):
    env.setenv("LLM_PROVIDER", "watsonx")
    env.setenv("WATSONX_API_KEY", VALID_KEY)
    env.setenv("WATSONX_PROJECT_ID", PROJECT_ID)
    env.setenv("WATSONX_URL", "https://eu-de.ml.cloud.ibm.com")
    env.setenv("WATSONX_MODEL_ID", "openai/gpt-oss-120b")
    llm = build_llm(config.load_settings())
    assert llm._url == "https://eu-de.ml.cloud.ibm.com" and llm._model == "openai/gpt-oss-120b" and llm._project == PROJECT_ID


def test_a_mistyped_provider_is_still_refused_and_watsonx_is_case_insensitive(env):
    env.setenv("WATSONX_API_KEY", VALID_KEY)
    env.setenv("WATSONX_PROJECT_ID", PROJECT_ID)
    env.setenv("LLM_PROVIDER", " WatsonX ")
    assert config.load_settings().llm_provider == "watsonx"
    env.setenv("LLM_PROVIDER", "watson")
    with pytest.raises(RuntimeError, match="LLM_PROVIDER"):
        config.load_settings()


def test_groq_stays_the_default_and_needs_no_watsonx_setting(env):
    env.setenv("GROQ_API_KEY", "gsk_x")
    s = config.load_settings()
    assert s.llm_provider == "groq" and isinstance(build_llm(s), GroqLLM) and s.watsonx_key == "" and s.watsonx_project_id == ""


def test_the_watsonx_key_is_read_from_the_saved_secret_file_when_the_variable_is_unset(env, tmp_path):
    (tmp_path / "watsonx_key.txt").write_text("WATSONX_API_KEY=%s\n" % VALID_KEY, encoding="utf-8")
    env.setenv("LLM_PROVIDER", "watsonx")
    env.setenv("WATSONX_PROJECT_ID", PROJECT_ID)
    assert config.load_settings().watsonx_key == VALID_KEY
    env.setenv("WATSONX_API_KEY", OTHER_KEY)
    assert config.load_settings().watsonx_key == OTHER_KEY  # the variable wins over the file


def test_the_project_id_is_trimmed(env):
    env.setenv("WATSONX_PROJECT_ID", "  " + PROJECT_ID + " \n")
    assert config.llm_settings_from_env()["watsonx_project_id"] == PROJECT_ID


def test_a_settings_object_built_the_old_way_still_works():
    s = Settings(groq_key="x", shop="demo.myshopify.com", shopify_token="t")
    assert s.llm_provider == "groq" and s.watsonx_key == "" and s.watsonx_url == config.WATSONX_DEFAULT_URL


def test_the_watsonx_provider_without_a_key_stops_at_start_with_a_clear_message():
    with pytest.raises(WatsonxConfigError, match="WATSONX_API_KEY"):
        build_llm(Settings(groq_key="", shop="s", shopify_token="t", llm_provider="watsonx", watsonx_project_id=PROJECT_ID))
    with pytest.raises(WatsonxConfigError, match="WATSONX_PROJECT_ID"):
        build_llm(Settings(groq_key="", shop="s", shopify_token="t", llm_provider="watsonx", watsonx_key=VALID_KEY))


def test_one_shared_client_for_the_process():
    assert watsonx_llm._client() is watsonx_llm._client()  # building a client costs ~140 ms of TLS setup: once per process


def test_the_stand_in_leaves_no_key_shaped_literal_in_the_repo_sources():
    # the sample key is built from fragments; a literal copy of the finished key must not sit in any source or doc
    hits = []
    for folder in ("agent", "tests", "docs", "README.md"):
        base = ROOT / folder
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file() and p.suffix in (".py", ".md", ".txt")]
        for p in files:
            if p.name == "test_watsonx_llm.py" or "test-runs" in p.parts:
                continue
            if VALID_KEY in p.read_text(encoding="utf-8", errors="replace"):
                hits.append(str(p))
    assert hits == []
    assert os.environ.get("WATSONX_API_KEY", "") != VALID_KEY
