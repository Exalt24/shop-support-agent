"""The Zoho client against the in-process FAKE (agent/zoho_fake.py). No network, no account. Each rule is one the saved vendor
pages state, and each has a mutant in mutation_check.py."""
import pytest

from agent.zoho_crm import KNOWN_API_DOMAINS, REGIONS, ZohoCrmClient, ZohoError, ZohoSettings, scrub
from agent.zoho_fake import FakeZoho

LEAD = {"Last_Name": "Doe", "First_Name": "Jane", "Email": "jane@example.com"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def client(region="us", fake_region=None, http=None, clock=None, max_attempts=4):
    http = http or FakeZoho(fake_region or region)
    sleeps = []
    c = ZohoCrmClient(ZohoSettings("cid", "csecret", "rtoken", region), http, clock=clock or Clock(), sleep=sleeps.append,
                      max_attempts=max_attempts)
    return c, http, sleeps


# ---- the contract on the wire ----------------------------------------------------------------------------------------

def test_upsert_sends_the_documented_url_header_and_duplicate_check_on_email():
    c, http, _ = client()
    r = c.upsert_lead(LEAD, ["Email"])
    method, url, body = http.api_calls()[0]
    assert (method, url) == ("POST", "https://www.zohoapis.com/crm/v8/Leads/upsert")
    assert body == {"data": [LEAD], "duplicate_check_fields": ["Email"]}
    assert r.action == "insert" and http.leads[r.id]["Last_Name"] == "Doe"


def test_the_refresh_call_uses_the_documented_parameters_and_the_header_is_zoho_oauthtoken():
    c, http, _ = client()
    c.upsert_lead(LEAD, ["Email"])
    method, url, params = http.token_calls()[0]
    assert (method, url) == ("POST", "https://accounts.zoho.com/oauth/v2/token")
    assert params == {"refresh_token": "rtoken", "client_id": "cid", "client_secret": "csecret", "grant_type": "refresh_token"}


def test_a_second_upsert_for_the_same_email_is_an_update_not_a_second_lead():
    c, http, _ = client()
    first = c.upsert_lead(LEAD, ["Email"])
    second = c.upsert_lead(dict(LEAD, Company="Acme"), ["Email"])
    assert second.id == first.id and second.action == "update" and len(http.leads) == 1


def test_a_task_carries_the_documented_fields_and_the_assignment_rule_beside_data():
    c, http, _ = client()
    lead = c.upsert_lead(LEAD, ["Email"])
    task = {"Subject": "Follow up", "Status": "Not Started", "Priority": "High", "Due_Date": "2026-11-02",
            "What_Id": {"id": lead.id}, "$se_module": "Leads"}
    c.create_task(task, assignment_rule_id="rule-9")
    _, url, body = http.api_calls()[-1]
    assert url.endswith("/crm/v8/Tasks") and body == {"data": [task], "lar_id": "rule-9"}
    assert http.tasks[0]["Subject"] == "Follow up"


def test_a_note_goes_to_the_records_notes_url_with_parent_id_and_content():
    c, http, _ = client()
    lead = c.upsert_lead(LEAD, ["Email"])
    c.add_note("Leads", lead.id, "hello")
    _, url, body = http.api_calls()[-1]
    assert url.endswith("/crm/v8/Leads/%s/Notes" % lead.id)
    assert body["data"][0] == {"Parent_Id": {"module": {"api_name": "Leads"}, "id": lead.id}, "Note_Content": "hello"}


def test_a_record_level_error_is_raised_with_the_vendors_code():
    c, _, _ = client()
    with pytest.raises(ZohoError) as e:
        c.upsert_lead({"Email": "jane@example.com"}, ["Email"])  # no Last_Name
    assert e.value.code == "MANDATORY_NOT_FOUND" and e.value.status == 400


# ---- the token -------------------------------------------------------------------------------------------------------

def test_the_access_token_is_cached_and_refreshed_a_minute_before_it_expires():
    clock = Clock()
    c, http, _ = client(clock=clock)
    c.upsert_lead(LEAD, ["Email"])
    c.upsert_lead(LEAD, ["Email"])
    assert http.tokens_issued == 1  # reused: Zoho allows 10 access tokens per refresh token per 10 minutes
    clock.t += 3600 - 61
    c.upsert_lead(LEAD, ["Email"])
    assert http.tokens_issued == 1
    clock.t += 2
    c.upsert_lead(LEAD, ["Email"])
    assert http.tokens_issued == 2


def test_a_401_refreshes_the_token_once_and_retries():
    c, http, sleeps = client()
    c.upsert_lead(LEAD, ["Email"])
    http.expire_token()
    r = c.upsert_lead(dict(LEAD, Company="Acme"), ["Email"])
    assert r.action == "update"
    assert http.tokens_issued == 2 and sleeps == []
    statuses = [call[0] for call in http.api_calls()]
    assert len(statuses) == 3  # ok, rejected, retried ok


def test_a_second_401_after_the_refresh_is_a_failure_not_a_loop():
    c, http, _ = client()
    http.script(401, {"code": "INVALID_OAUTHTOKEN", "message": "invalid oauth token", "status": "error"})
    http.script(401, {"code": "INVALID_OAUTHTOKEN", "message": "invalid oauth token", "status": "error"})
    with pytest.raises(ZohoError) as e:
        c.upsert_lead(LEAD, ["Email"])
    assert e.value.status == 401 and e.value.code == "INVALID_OAUTHTOKEN"
    assert http.tokens_issued == 2 and len(http.api_calls()) == 2


def test_a_refused_refresh_is_a_failure_and_is_not_retried():
    c, http, _ = client()
    c._s = ZohoSettings("", "csecret", "rtoken")  # the fake refuses a refresh without a client id, as an error body on HTTP 200
    with pytest.raises(ZohoError) as e:
        c.upsert_lead(LEAD, ["Email"])
    assert e.value.code == "TOKEN_REFRESH_FAILED" and len(http.token_calls()) == 1 and http.api_calls() == []


# ---- retries ---------------------------------------------------------------------------------------------------------

def test_a_429_then_success_backs_off_and_succeeds():
    c, http, sleeps = client()
    http.script(429, {"code": "TOO_MANY_REQUESTS", "message": "too many", "status": "error"})
    assert c.upsert_lead(LEAD, ["Email"]).action == "insert"
    assert sleeps == [0.5] and len(http.leads) == 1


def test_backoff_doubles_and_a_run_of_5xx_ends_in_a_retryable_failure():
    c, http, sleeps = client()
    for _ in range(4):
        http.script(503)
    with pytest.raises(ZohoError) as e:
        c.upsert_lead(LEAD, ["Email"])
    assert e.value.retryable and sleeps == [0.5, 1.0, 2.0] and len(http.api_calls()) == 4 and http.leads == {}


def test_a_408_is_retried():
    c, http, sleeps = client()
    http.script(408)
    assert c.upsert_lead(LEAD, ["Email"]).action == "insert" and sleeps == [0.5]


def test_a_transport_error_is_retried_then_reported():
    c, http, sleeps = client(max_attempts=3)
    http.drop(3)
    with pytest.raises(ZohoError) as e:
        c.upsert_lead(LEAD, ["Email"])
    assert e.value.code == "TRANSPORT" and len(sleeps) == 2


def test_a_4xx_other_than_408_and_429_fails_at_once_with_the_vendors_code_and_message():
    c, http, sleeps = client()
    http.script(400, {"code": "DUPLICATE_DATA", "message": "duplicate data", "details": {"api_name": "Email"}, "status": "error"})
    with pytest.raises(ZohoError) as e:
        c.upsert_lead(LEAD, ["Email"])
    assert e.value.code == "DUPLICATE_DATA" and "duplicate data" in str(e.value)
    assert sleeps == [] and len(http.api_calls()) == 1 and not e.value.retryable


def test_a_207_with_a_record_level_error_is_a_failure_even_though_the_http_status_is_2xx():
    c, http, _ = client()
    http.script(207, {"data": [{"code": "DUPLICATE_DATA", "message": "duplicate data", "status": "error", "details": {"api_name": "Email"}}]})
    with pytest.raises(ZohoError) as e:
        c.upsert_lead(LEAD, ["Email"])
    assert e.value.code == "DUPLICATE_DATA" and http.leads == {}


def test_a_vendor_message_that_echoes_an_email_is_masked():
    assert scrub("invalid data for jane@example.com") == "invalid data for [email]"


# ---- regions ---------------------------------------------------------------------------------------------------------

def test_an_australian_account_uses_the_com_au_accounts_and_api_domains():
    c, http, _ = client(region="au")
    c.upsert_lead(LEAD, ["Email"])
    assert http.token_calls()[0][1].startswith("https://accounts.zoho.com.au/")
    assert http.api_calls()[0][1].startswith("https://www.zohoapis.com.au/crm/v8/")


def test_the_wrong_region_reaches_nothing():
    c, http, _ = client(region="us", fake_region="au")  # an AU account, a client left on the US domains
    with pytest.raises(ZohoError):
        c.upsert_lead(LEAD, ["Email"])
    assert http.leads == {}


def test_a_data_centre_named_in_the_token_answer_is_used_only_if_it_is_a_known_zoho_host():
    c, http, _ = client()
    c._access_token()
    assert c._api_domain == "https://www.zohoapis.com"
    http2 = FakeZoho()
    real = http2._token
    def token(params):
        r = real(params)
        r._body["api_domain"] = "https://evil.example.net"
        return r
    http2._token = token
    c2, _, _ = client(http=http2)
    c2._access_token()
    assert c2._api_domain == "https://www.zohoapis.com"  # never sent a token to an unlisted host


def test_every_region_pairs_a_zoho_accounts_url_with_an_api_domain():
    for name, (accounts, api) in REGIONS.items():
        assert accounts.startswith("https://accounts.zoho") and api.startswith("https://www.zohoapis.") and api in KNOWN_API_DOMAINS, name


def test_an_unknown_region_is_refused_and_settings_come_only_from_named_variables():
    with pytest.raises(ValueError):
        ZohoSettings("a", "b", "c", "mars")
    with pytest.raises(RuntimeError, match="ZOHO_CLIENT_ID.*ZOHO_REFRESH_TOKEN"):
        ZohoSettings.from_env({"ZOHO_CLIENT_SECRET": "x"})
    s = ZohoSettings.from_env({"ZOHO_CLIENT_ID": "a", "ZOHO_CLIENT_SECRET": "b", "ZOHO_REFRESH_TOKEN": "c", "ZOHO_REGION": "AU"})
    assert s.region == "au" and s.accounts_url == "https://accounts.zoho.com.au"
    assert "b" not in repr(s) and "c" not in repr(s).replace("au", "")  # secrets stay out of a repr
