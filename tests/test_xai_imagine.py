"""The Grok Imagine client (images now or deferred, image to video) against tests/xai_standin.py, a real HTTP server written from the
pages saved in docs/vendor-contract/xai-*.txt. Nothing here is live: no xAI key, no spend, no network beyond 127.0.0.1.

Every negative test is preceded by a positive control (the same call is answered normally), so a refusal in a test is the guard under
test and not a request the stand-in never accepted. Each guard has a mutant in tests/mutation_check.py."""
import base64
import random
from pathlib import Path

import httpx
import pytest

from agent import config
from agent.xai_http import MAX_RETRIES, XaiAPIError, XaiBadResponseError, XaiConfigError, XaiTransportError
from agent.xai_imagine import (POLL_SECONDS, POLL_TIMEOUT_SECONDS, GeneratedImage, ImageBatch, Poll, VideoResult, XaiImagine,
                               XaiJobFailedError, XaiModerationError, XaiTimeoutError)

from xai_standin import OTHER_KEY, VALID_KEY, Fault, StandIn, http_error

ROOT = Path(__file__).resolve().parent.parent
SAVED = ROOT / "docs" / "vendor-contract"
PIC = "https://example.com/still.png"


def saved(name):
    return (SAVED / name).read_text(encoding="utf-8")


class Clock:
    """A fake clock that only moves when the client sleeps, so a wait is exact and instant."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def server():
    s = StandIn().start()
    yield s
    s.stop()


class Rig:
    def __init__(self, server, **kw):
        self.server, self.clock = server, Clock()
        self.api = XaiImagine(kw.pop("key", VALID_KEY), base_url=server.base, sleep=self.clock.sleep, clock=self.clock, **kw)

    @property
    def sleeps(self):
        return self.clock.sleeps

    def control(self):
        """The positive control: this client, this server, a normal image call, a normal answer."""
        assert len(self.api.generate("a red kite").images) == 1
        self.server.requests.clear()
        self.sleeps.clear()


@pytest.fixture
def rig(server):
    return Rig(server)


def last(server, kind):
    return server.of_kind(kind)[-1]


# ---- images, answered in the request ---------------------------------------------------------------------------------------

def test_a_plain_image_request_has_the_documented_shape_and_costs_four_cents(rig, server):
    batch = rig.api.generate("A collage of London landmarks")
    req = last(server, "image")
    assert req["method"] == "POST" and req["path"] == "/v1/images/generations"
    assert req["headers"]["authorization"] == "Bearer " + VALID_KEY and req["headers"]["content-type"].startswith("application/json")
    assert req["body"] == {"model": "grok-imagine-image-2.0", "prompt": "A collage of London landmarks"}
    assert isinstance(batch, ImageBatch) and len(batch.images) == 1
    assert batch.images[0] == GeneratedImage("https://stand-in.invalid/xai-tmp-imgen-0.jpeg", None, "image/jpeg")
    assert batch.cost_usd_ticks == 400_000_000 and batch.cost_usd == pytest.approx(0.04)


def test_every_option_is_sent_under_its_documented_name_and_n_images_cost_n_times_as_much(rig, server):
    batch = rig.api.generate("a lighthouse", n=4, aspect_ratio="16:9", resolution="2k", quality="low", response_format="url")
    assert last(server, "image")["body"] == {"model": "grok-imagine-image-2.0", "prompt": "a lighthouse", "n": 4, "aspect_ratio": "16:9",
                                             "resolution": "2k", "quality": "low", "response_format": "url"}
    assert len(batch.images) == 4 and batch.cost_usd == pytest.approx(0.16)


def test_base64_output_is_decoded_on_request(rig, server):
    batch = rig.api.generate("a garden", n=2, response_format="b64_json")
    assert last(server, "image")["body"]["response_format"] == "b64_json"
    assert [i.image_bytes() for i in batch.images] == [b"STANDIN-IMAGE-0", b"STANDIN-IMAGE-1"] and all(i.url is None for i in batch.images)
    with pytest.raises(XaiConfigError):
        rig.api.generate("a garden").images[0].image_bytes()  # a URL image has no bytes to decode


def test_the_stand_in_itself_refuses_what_the_pages_say_not_to_send(server, rig):
    # control for the tests below: the server is strict, so "no 4xx happened" means something
    rig.control()
    url = server.base + "/images/generations"
    auth = {"Authorization": "Bearer " + VALID_KEY}
    good = {"model": "grok-imagine-image-2.0", "prompt": "x"}
    assert httpx.post(url, headers=auth, json=good).status_code == 200
    assert httpx.post(url, headers={"Authorization": "Bearer " + OTHER_KEY}, json=good).status_code == 401
    assert httpx.post(url, headers=auth, json=dict(good, n=11)).status_code == 422
    assert httpx.post(url, headers=auth, json=dict(good, n=0)).status_code == 422
    assert httpx.post(url, headers=auth, json=dict(good, aspect_ratio="7:3")).status_code == 422
    assert httpx.post(url, headers=auth, json=dict(good, resolution="4k")).status_code == 422
    assert httpx.post(url, headers=auth, json=dict(good, quality="high")).status_code == 422
    assert httpx.post(url, headers=auth, json={"model": "grok-imagine-image", "prompt": "x", "quality": "low"}).status_code == 422
    assert httpx.post(url, headers=auth, json=dict(good, deferred=True, response_format="b64_json")).status_code == 400
    assert httpx.post(url, headers=auth, json={"model": "grok-imagine-image-2.0"}).status_code == 400
    assert httpx.post(url, headers=auth, json=dict(good, foo=1)).status_code == 422
    assert httpx.get(url, headers=auth).status_code == 405
    video = server.base + "/videos/generations"
    assert httpx.post(video, headers=auth, json={"model": "grok-imagine-video-1.5", "prompt": "x"}).status_code == 200
    assert httpx.post(video, headers=auth, json={"model": "grok-imagine-video-1.5"}).status_code == 400
    assert httpx.post(video, headers=auth, json={"model": "grok-imagine-video-1.5", "prompt": "x", "duration": 16}).status_code == 422
    assert httpx.post(video, headers=auth, json={"model": "grok-imagine-video-1.5", "prompt": "x", "aspect_ratio": "2:1"}).status_code == 422


@pytest.mark.parametrize("kw", [
    {"prompt": ""}, {"prompt": "   "}, {"prompt": None}, {"prompt": 5},
    {"n": 0}, {"n": 11}, {"n": True}, {"n": "2"}, {"n": 1.5}, {"n": -1},
    {"aspect_ratio": "7:3"}, {"aspect_ratio": "16x9"}, {"aspect_ratio": 16},
    {"resolution": "4k"}, {"resolution": "1K"}, {"resolution": "720p"},
    {"quality": "high"}, {"quality": "LOW"},
    {"response_format": "png"}, {"response_format": "base64"},
], ids=lambda kw: "-".join("%s=%r" % kv for kv in kw.items()))
def test_a_bad_image_argument_is_refused_before_any_request(rig, server, kw):
    rig.control()
    args = dict({"prompt": "a kite"}, **kw)
    with pytest.raises(XaiConfigError):
        rig.api.generate(args.pop("prompt"), **args)
    assert not server.requests


@pytest.mark.parametrize("kw", [{"n": 1}, {"n": 10}, {"aspect_ratio": "auto"}, {"aspect_ratio": "19.5:9"}, {"aspect_ratio": "5:2"},
                                {"resolution": "1k"}, {"resolution": "1.5k"}, {"quality": "auto"}, {"quality": "medium"}])
def test_the_edges_of_the_documented_ranges_are_accepted(rig, server, kw):
    assert rig.api.generate("a kite", **kw).images
    assert not [r for r in server.requests if not r["valid"]]


def test_quality_is_refused_for_a_model_that_does_not_take_it(server):
    api = XaiImagine(VALID_KEY, base_url=server.base, image_model="grok-imagine-image", sleep=lambda s: None)
    api.generate("a kite")  # control
    server.requests.clear()
    with pytest.raises(XaiConfigError):
        api.generate("a kite", quality="low")
    assert not server.requests


@pytest.mark.parametrize("url", ["", "http://api.x.ai/v1", "ftp://x/v1", "http://example.com/v1"])
def test_only_https_or_this_machine_is_accepted_as_the_base_url(url):
    with pytest.raises(XaiConfigError):
        XaiImagine(VALID_KEY, base_url=url)


@pytest.mark.parametrize("key", ["", "short", "has space in it 1234", None, "tab\tinside-1234567"])
def test_a_malformed_key_is_refused(server, key):
    with pytest.raises(XaiConfigError):
        XaiImagine(key, base_url=server.base)


@pytest.mark.parametrize("name", ["image_model", "video_model"])
@pytest.mark.parametrize("model", ["", "a b", "x" * 101, None, "grok-imagine\n"])
def test_a_malformed_model_name_is_refused(server, name, model):
    with pytest.raises(XaiConfigError):
        XaiImagine(VALID_KEY, base_url=server.base, **{name: model})


def test_the_key_goes_only_in_the_authorization_header_and_never_into_a_message(rig, server):
    rig.api.generate("a kite")
    req = last(server, "image")
    assert VALID_KEY not in req["raw"].decode() + req["path"] and [k for k, v in req["headers"].items() if VALID_KEY in v] == ["authorization"]
    rig.control()
    server.script("image", http_error(403, "rejected key %s" % VALID_KEY))
    with pytest.raises(XaiAPIError) as e:
        rig.api.generate("a kite")
    assert VALID_KEY not in str(e.value) and "rejected" not in str(e.value) and str(e.value).startswith("HTTP 403 Forbidden")


# ---- failures of the request ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_client_error_is_named_and_not_retried(rig, server, status):
    rig.control()
    server.script("image", http_error(status))
    with pytest.raises(XaiAPIError) as e:
        rig.api.generate("a kite")
    assert e.value.status == status and len(server.of_kind("image")) == 1 and rig.sleeps == []


def test_a_wrong_key_is_a_401(server):
    r = Rig(server, key=OTHER_KEY)
    with pytest.raises(XaiAPIError) as e:
        r.api.generate("a kite")
    assert e.value.status == 401 and OTHER_KEY not in str(e.value)


def test_a_rate_limit_on_the_post_is_retried_because_a_refused_request_was_not_billed(rig, server):
    rig.control()
    server.script("image", http_error(429))
    assert rig.api.generate("a kite").images and rig.sleeps == [1.0] and len(server.of_kind("image")) == 2
    server.requests.clear()
    rig.sleeps.clear()
    server.script("image", *[http_error(429)] * (MAX_RETRIES + 1))
    with pytest.raises(XaiAPIError):
        rig.api.generate("a kite")
    assert len(server.of_kind("image")) == MAX_RETRIES + 1 and rig.sleeps == [1.0, 2.0]


@pytest.mark.parametrize("kind,call", [
    ("image", lambda a: a.generate("a kite")),
    ("image", lambda a: a.start_deferred("a kite")),
    ("video", lambda a: a.start_video("a kite")),
], ids=["generate", "start_deferred", "start_video"])
def test_a_paid_post_is_never_repeated_after_a_dropped_connection(rig, server, kind, call):
    rig.control()
    server.script(kind, Fault(drop=True))
    with pytest.raises(XaiTransportError):
        call(rig.api)
    assert len(server.of_kind(kind)) == 1 and rig.sleeps == []


def test_a_read_timeout_on_a_paid_post_is_not_repeated(server):
    r = Rig(server, timeout=0.2)
    r.control()
    server.script("image", Fault(delay=1.0))
    with pytest.raises(XaiTransportError) as e:
        r.api.generate("a kite")
    assert "ReadTimeout" in str(e.value) and len(server.of_kind("image")) == 1


@pytest.mark.parametrize("raw", [b"", b"nope", b"[]", b"null", b'{"data": []}', b'{"data": {}}', b'{"data": "x"}', b'{"x": 1}',
                                 b'{"data": [1]}', b'{"data": [null]}', b'{"data": [{}]}', b'{"data": [{"url": ""}]}',
                                 b'{"data": [{"url": 5}]}', b'{"data": [{"url": "http://insecure.example/a.jpg"}]}',
                                 b'{"data": [{"url": "ftp://x/a.jpg"}]}',
                                 b'{"data": [{"url": "https://a.example/1.jpg"}, {"url": "https://a.example/2.jpg"}]}'])
def test_a_200_that_is_not_an_image_answer_is_a_named_error(rig, server, raw):
    rig.control()
    server.script("image", Fault(200, raw=raw))
    with pytest.raises(XaiBadResponseError):  # the last case has two images for n=1
        rig.api.generate("a kite")


@pytest.mark.parametrize("entry", [{}, {"b64_json": ""}, {"b64_json": 5}, {"b64_json": "not base64!!"}, {"b64_json": "QUJD!!"},{"url": "https://a.example/x.jpg"}])
def test_a_b64_request_with_a_bad_entry_is_a_named_error(rig, server, entry):
    rig.control()
    server.script("image", Fault(200, {"data": [entry]}))
    with pytest.raises(XaiBadResponseError):
        rig.api.generate("a kite", response_format="b64_json")


def test_fewer_images_than_asked_for_are_returned_as_they_are_and_cost_may_be_absent(rig, server):
    server.script("image", Fault(200, {"data": [{"url": "https://a.example/1.jpg", "mime_type": "image/png"}]}))
    batch = rig.api.generate("a kite", n=3)
    assert len(batch.images) == 1 and batch.images[0].mime_type == "image/png" and batch.cost_usd_ticks is None and batch.cost_usd is None


@pytest.mark.parametrize("mime", [None, 5, "text/html", "image/jp eg", "image/" + "x" * 30, "image/jpeg\n"])
def test_an_odd_mime_type_is_dropped_not_trusted(rig, server, mime):
    server.script("image", Fault(200, {"data": [{"url": "https://a.example/1.jpg", "mime_type": mime}]}))
    assert rig.api.generate("a kite").images[0].mime_type is None


@pytest.mark.parametrize("ticks", ["many", True, -5, 1.5, None])
def test_a_junk_cost_is_ignored(rig, server, ticks):
    server.script("image", Fault(200, {"data": [{"url": "https://a.example/1.jpg"}], "usage": {"cost_in_usd_ticks": ticks}}))
    assert rig.api.generate("a kite").cost_usd_ticks is None


# ---- deferred images ------------------------------------------------------------------------------------------------------

def test_a_deferred_request_returns_an_id_and_the_poll_goes_pending_then_done(rig, server):
    rid = rig.api.start_deferred("a serene garden", n=2, aspect_ratio="1:1")
    req = last(server, "image")
    assert req["body"] == {"model": "grok-imagine-image-2.0", "prompt": "a serene garden", "n": 2, "aspect_ratio": "1:1", "deferred": True}
    assert rid == "00000000-0000-4000-8000-000000000001"
    assert rig.api.poll_images(rid, expected=2) == Poll(False)  # HTTP 202
    assert last(server, "image_poll")["path"] == "/v1/images/" + rid and last(server, "image_poll")["method"] == "GET"
    assert rig.api.poll_images(rid, expected=2) == Poll(False)
    done = rig.api.poll_images(rid, expected=2)
    assert done.done and isinstance(done.result, ImageBatch) and len(done.result.images) == 2
    assert done.result.request_id == rid and done.result.cost_usd == pytest.approx(0.08)


def test_wait_images_polls_every_five_seconds_until_done(rig, server):
    rid = rig.api.start_deferred("a serene garden")
    batch = rig.api.wait_images(rid, expected=1)
    assert len(batch.images) == 1 and rig.sleeps == [POLL_SECONDS, POLL_SECONDS] and POLL_SECONDS == 5.0
    assert len(server.of_kind("image_poll")) == 3


def test_a_failed_deferred_job_is_a_body_not_an_http_error_and_is_not_polled_again(rig, server):
    server.job_failure = {"code": "invalid_argument", "message": "prompt   rejected\nby policy"}
    rid = rig.api.start_deferred("a kite")
    server.pending_polls = 0
    with pytest.raises(XaiJobFailedError) as e:
        rig.api.wait_images(rid)
    assert e.value.code == "invalid_argument" and "invalid_argument: prompt rejected by policy" in str(e.value)
    assert len(server.of_kind("image_poll")) == 1 and rig.sleeps == []


@pytest.mark.parametrize("err,code,text", [
    ({"code": "internal_error", "message": "x" * 500}, "internal_error", "x" * 200),
    ({"code": "something_new", "message": "m"}, "unknown", "m"),
    ({"message": "no code"}, "unknown", "no code"),
    ({"code": "permission_denied"}, "permission_denied", "no message"),
    ("a string", "unknown", "no message"),
    (None, "unknown", "no message"),
])
def test_a_failed_job_error_is_shown_cut_and_with_an_unknown_code_marked_unknown(rig, server, err, code, text):
    server.job_failure = err
    rid = rig.api.start_deferred("a kite")
    server.pending_polls = 0
    server._jobs[rid]["failure"] = err or {}
    server.script("image_poll", Fault(200, {"request_id": rid, "status": "failed", **({"error": err} if err is not None else {})}))
    with pytest.raises(XaiJobFailedError) as e:
        rig.api.poll_images(rid)
    assert e.value.code == code and str(e.value).endswith(text) and "x" * 201 not in str(e.value)


def test_a_poll_that_times_out_keeps_the_request_id(server):
    r = Rig(server, poll_timeout=20.0)
    server.pending_polls = 10_000
    rid = r.api.start_deferred("a kite")
    with pytest.raises(XaiTimeoutError) as e:
        r.api.wait_images(rid)
    assert e.value.request_id == rid and rid in str(e.value)
    assert len(server.of_kind("image_poll")) == 5 and r.sleeps == [5.0] * 4  # polls at 0, 5, 10, 15 and 20 s, the last one gives up


def test_the_default_wait_is_ten_minutes():
    assert POLL_TIMEOUT_SECONDS == 600.0
    assert "10 minutes" in saved("xai-video-generation.txt") and "time.sleep(5)" in saved("xai-video-generation.txt")


@pytest.mark.parametrize("rid", ["", "../etc/passwd", "a b", "a/b", "a?x=1", "x" * 129, None, 5, "id\n", "é"])
def test_a_request_id_that_could_change_the_url_is_refused(rig, server, rid):
    rig.control()
    for call in (rig.api.poll_images, rig.api.poll_video, rig.api.wait_images, rig.api.wait_video):
        with pytest.raises(XaiConfigError):
            call(rid)
    assert not server.requests


def test_a_request_id_in_its_documented_shape_is_accepted(rig, server):
    rid = rig.api.start_deferred("a kite")
    assert rig.api.poll_images(rid) == Poll(False)
    with pytest.raises(XaiAPIError) as e:  # the shape of the xAI sample id is accepted too, and the stand-in does not know it
        rig.api.poll_images("e5b1b4d4-7b6a-4a0e-9c0d-7f3c7d8a1b2c")
    assert e.value.status == 404


def test_an_unknown_id_is_a_404_and_a_poll_error_is_retried_when_transient(rig, server):
    with pytest.raises(XaiAPIError) as e:
        rig.api.poll_images("no-such-job")
    assert e.value.status == 404
    rid = rig.api.start_deferred("a kite")
    server.requests.clear()
    server.script("image_poll", http_error(429), Fault(drop=True))
    assert rig.api.poll_images(rid) == Poll(False)  # a GET is safe to repeat: the 429 and the dropped connection are both absorbed
    assert len(server.of_kind("image_poll")) == 3 and rig.sleeps == [1.0, 2.0]


@pytest.mark.parametrize("body,status", [
    ({"request_id": "x", "status": "weird"}, 200), ({"request_id": "x"}, 200), ({"status": 5}, 200), ([], 200),
    ({"request_id": "x", "status": "done", "data": []}, 200), ({"request_id": "x", "status": "done"}, 200),
    ({"request_id": "x", "status": "done", "data": [{"url": "https://a.example/1.jpg"}]}, 202),
])
def test_a_poll_answer_that_is_not_documented_is_a_named_error(rig, server, body, status):
    rid = rig.api.start_deferred("a kite")
    server.script("image_poll", Fault(status, body))
    with pytest.raises(XaiBadResponseError):
        rig.api.poll_images(rid)


def test_an_unknown_status_is_not_read_as_done_even_when_it_carries_a_result(rig, server):
    rid = rig.api.start_deferred("a kite")
    server.script("image_poll", Fault(200, {"request_id": rid, "status": "weird", "data": [{"url": "https://a.example/1.jpg"}]}))
    with pytest.raises(XaiBadResponseError) as e:
        rig.api.poll_images(rid)
    assert "status" in str(e.value)
    vid = rig.api.start_video("a lake")
    server.script("video_poll", Fault(200, {"status": "weird", "video": {"url": "https://a.example/v.mp4", "respect_moderation": True}}))
    with pytest.raises(XaiBadResponseError) as e:
        rig.api.poll_video(vid)
    assert "status" in str(e.value)


@pytest.mark.parametrize("raw", [b"", b"nope", b"{"])
def test_a_poll_body_that_is_not_json_is_a_named_error(rig, server, raw):
    rid = rig.api.start_deferred("a kite")
    server.script("image_poll", Fault(200, raw=raw))
    with pytest.raises(XaiBadResponseError):
        rig.api.poll_images(rid)


@pytest.mark.parametrize("body", [{}, {"request_id": ""}, {"request_id": "a b"}, {"request_id": 5}, {"request_id": "../x"}, []])
def test_a_start_answer_without_a_usable_request_id_is_a_named_error(rig, server, body):
    rig.control()
    server.script("image", Fault(200, body))
    with pytest.raises(XaiBadResponseError):
        rig.api.start_deferred("a kite")
    server.script("video", Fault(200, body))
    with pytest.raises(XaiBadResponseError):
        rig.api.start_video("a kite")


# ---- video ----------------------------------------------------------------------------------------------------------------

def test_a_text_to_video_request_has_the_documented_shape(rig, server):
    rid = rig.api.start_video("A serene lake at sunrise")
    req = last(server, "video")
    assert req["method"] == "POST" and req["path"] == "/v1/videos/generations"
    assert req["headers"]["authorization"] == "Bearer " + VALID_KEY
    assert req["body"] == {"model": "grok-imagine-video-1.5", "prompt": "A serene lake at sunrise"} and rid.startswith("00000000-")


def test_an_image_to_video_request_sends_the_image_object_and_every_option(rig, server):
    rig.api.start_video("Make the water crash down", image_url=PIC, duration=12, aspect_ratio="9:16", resolution="720p")
    assert last(server, "video")["body"] == {"model": "grok-imagine-video-1.5", "prompt": "Make the water crash down", "image": {"url": PIC},
                                             "duration": 12, "aspect_ratio": "9:16", "resolution": "720p"}


def test_an_image_alone_is_enough_because_the_prompt_is_optional_for_image_to_video(rig, server):
    rig.api.start_video(image_url=PIC)
    assert last(server, "video")["body"] == {"model": "grok-imagine-video-1.5", "image": {"url": PIC}}


def test_a_data_uri_is_accepted_as_the_image(rig, server):
    uri = "data:image/jpeg;base64," + base64.b64encode(b"jpeg bytes").decode()
    rig.api.start_video(image_url=uri)
    assert last(server, "video")["body"]["image"] == {"url": uri}


@pytest.mark.parametrize("kw", [
    {}, {"prompt": ""}, {"prompt": "  "}, {"prompt": 5},
    {"prompt": "x", "image_url": ""}, {"prompt": "x", "image_url": "ftp://x/a.png"}, {"prompt": "x", "image_url": "javascript:alert(1)"},
    {"prompt": "x", "image_url": "data:image/gif;base64,AAAA"}, {"prompt": "x", "image_url": "data:text/html;base64,AAAA"},
    {"prompt": "x", "image_url": "https://a.example/a b.png"}, {"prompt": "x", "image_url": "https://a.example/a.png\n"},
    {"prompt": "x", "image_url": 5}, {"image_url": "file:///etc/hosts"},
    {"prompt": "x", "duration": 0}, {"prompt": "x", "duration": 16}, {"prompt": "x", "duration": -1}, {"prompt": "x", "duration": True},
    {"prompt": "x", "duration": "8"}, {"prompt": "x", "duration": 8.5},
    {"prompt": "x", "aspect_ratio": "2:1"}, {"prompt": "x", "aspect_ratio": "auto"}, {"prompt": "x", "aspect_ratio": "19.5:9"},
    {"prompt": "x", "resolution": "2160p"}, {"prompt": "x", "resolution": "1k"}, {"prompt": "x", "resolution": "720P"},
], ids=lambda kw: "-".join("%s=%r" % kv for kv in kw.items()) or "empty")
def test_a_bad_video_argument_is_refused_before_any_request(rig, server, kw):
    rig.control()
    with pytest.raises(XaiConfigError):
        rig.api.start_video(**kw)
    assert not server.requests


@pytest.mark.parametrize("kw", [{"duration": 1}, {"duration": 15}, {"aspect_ratio": "21:9"}, {"aspect_ratio": "5:2"}, {"resolution": "480p"},
                                {"resolution": "1080p"}])
def test_the_edges_of_the_documented_video_ranges_are_accepted(rig, server, kw):
    assert rig.api.start_video("a lake", **kw)
    assert not [r for r in server.requests if not r["valid"]]


def test_a_video_poll_reports_progress_while_pending_whatever_the_http_status(server):
    for pending_status in (200, 202):
        server.video_pending_status = pending_status
        r = Rig(server)
        rid = r.api.start_video("a lake")
        assert r.api.poll_video(rid) == Poll(False, 30)
        assert r.api.poll_video(rid) == Poll(False, 60)
        done = r.api.poll_video(rid)
        assert done.done and done.progress == 100


def test_wait_video_returns_the_url_duration_and_cost(rig, server):
    rid = rig.api.start_video("a lake", duration=6)
    result = rig.api.wait_video(rid)
    assert isinstance(result, VideoResult) and result.url == "https://stand-in.invalid/xai-video-%s.mp4" % rid
    assert result.duration == 6 and result.request_id == rid and result.cost_usd_ticks == 6 * 800_000_000
    assert rig.sleeps == [5.0, 5.0]
    assert last(server, "video_poll")["path"] == "/v1/videos/" + rid


def test_a_video_that_did_not_pass_moderation_is_a_named_error_not_a_blank_url(rig, server):
    server.video_moderation_ok = False
    rid = rig.api.start_video("a lake")
    server.pending_polls = 0
    with pytest.raises(XaiModerationError):
        rig.api.wait_video(rid)


def test_a_failed_video_job_names_the_documented_code(rig, server):
    server.job_failure = {"code": "service_unavailable", "message": "engine overloaded"}
    rid = rig.api.start_video("a lake")
    server.pending_polls = 0
    with pytest.raises(XaiJobFailedError) as e:
        rig.api.wait_video(rid)
    assert e.value.code == "service_unavailable" and "engine overloaded" in str(e.value) and len(server.of_kind("video_poll")) == 1


def test_a_video_poll_that_times_out_keeps_the_request_id(server):
    r = Rig(server, poll_timeout=12.0)
    server.pending_polls = 10_000
    rid = r.api.start_video("a lake")
    with pytest.raises(XaiTimeoutError) as e:
        r.api.wait_video(rid)
    assert e.value.request_id == rid and len(server.of_kind("video_poll")) == 3 and r.sleeps == [5.0, 5.0]


@pytest.mark.parametrize("body", [
    {"status": "done"}, {"status": "done", "video": "x"}, {"status": "done", "video": {}},
    {"status": "done", "video": {"url": "https://a.example/v.mp4"}},  # respect_moderation missing
    {"status": "done", "video": {"url": "https://a.example/v.mp4", "respect_moderation": "yes"}},
    {"status": "done", "video": {"respect_moderation": True}}, {"status": "done", "video": {"url": "", "respect_moderation": True}},
    {"status": "done", "video": {"url": "http://a.example/v.mp4", "respect_moderation": True}},
    {"status": "done", "video": {"url": 5, "respect_moderation": True}}, {"status": "odd"}, {}, {"status": None},
])
def test_a_video_poll_answer_that_is_not_documented_is_a_named_error(rig, server, body):
    rid = rig.api.start_video("a lake")
    server.script("video_poll", Fault(200, body))
    with pytest.raises(XaiBadResponseError):
        rig.api.poll_video(rid)


@pytest.mark.parametrize("duration,want", [(8, 8), (8.0, 8), (0, None), (-3, None), ("8", None), (None, None), (True, None), (2.5, None)])
def test_a_junk_video_duration_is_dropped(rig, server, duration, want):
    rid = rig.api.start_video("a lake")
    server.script("video_poll", Fault(200, {"status": "done", "video": {"url": "https://a.example/v.mp4", "respect_moderation": True,
                                                                          "duration": duration}}))
    assert rig.api.poll_video(rid).result.duration == want


@pytest.mark.parametrize("progress,want", [(0, 0), (50, 50), (99, 99), (100, None), (-1, None), ("5", None), (True, None), (None, None)])
def test_a_pending_progress_outside_zero_to_ninety_nine_is_dropped(rig, server, progress, want):
    rid = rig.api.start_video("a lake")
    server.script("video_poll", Fault(200, {"status": "pending", "progress": progress}))
    assert rig.api.poll_video(rid) == Poll(False, want)


# ---- the saved pages say what the code says ---------------------------------------------------------------------------------

def test_the_names_urls_and_limits_are_the_ones_on_the_saved_pages():
    imagine, gen, ref = saved("xai-imagine-overview.txt"), saved("xai-image-generation.txt"), saved("xai-api-reference-images.txt")
    video, vref, i2v = saved("xai-video-generation.txt"), saved("xai-api-reference-videos.txt"), saved("xai-image-to-video.txt")
    assert config.XAI_IMAGE_MODEL == "grok-imagine-image-2.0" and config.XAI_IMAGE_MODEL in imagine and "$0.04 / image" in imagine
    assert config.XAI_VIDEO_MODEL == "grok-imagine-video-1.5" and config.XAI_VIDEO_MODEL in imagine
    assert config.XAI_DEFAULT_URL + "/images/generations" in gen and config.XAI_DEFAULT_URL + "/videos/generations" in video
    assert "(`1`\u2013`10`)" in gen and "`low`, `medium`, and `auto`" in gen and "only supported for `grok-imagine-image-2.0`" in gen
    assert "`1k` (default when omitted)" in gen and "`2k`" in gen and "`url`, which returns temporary hosted URLs" in gen
    assert "GET /v1/images/\\{request\\_id}" in ref and "`pending` (HTTP `202`)" in gen and "(HTTP `200`)" in gen
    assert "Deferred requests support `response_format: \"url\"` only" in gen and "retained for 24 hours" in gen
    assert "10,000,000,000 ticks" in ref and "GET /v1/videos/\\{request\\_id}" in vref and "`respect_moderation`" in vref
    assert "Range: \\[1, 15]" in vref and "Default: 8" in vref and "Set `duration` from 1 to 15 seconds" in video
    assert "`pending`" in video and "`done`" in video and "`failed`" in video
    assert "A **public URL**" in i2v and "A **base64-encoded data URI**" in i2v and "Sending `image` alone is image-to-video" in i2v
    assert "Required for text-to-video" in vref.replace("\\", "") or "Required for text-to-video (T2V)" in vref.replace("\\", "")


# ---- random polling worlds follow the rules ------------------------------------------------------------------------------

def _oracle(pending, timeout, step):
    """(polls made, sleeps, outcome) for a job that answers pending `pending` times, written plainly and independently."""
    elapsed, polls, sleeps = 0.0, 0, []
    while True:
        polls += 1
        if polls > pending:
            return polls, sleeps, "done"
        if elapsed + step > timeout:
            return polls, sleeps, "timeout"
        sleeps.append(step)
        elapsed += step


@pytest.mark.parametrize("seed", range(40))
def test_random_polling_worlds_follow_the_wait_rules(server, seed):
    rnd = random.Random(seed)
    pending, timeout, step = rnd.randint(0, 12), rnd.choice([0.0, 4.0, 5.0, 9.0, 10.0, 25.0, 61.0]), rnd.choice([1.0, 2.5, 5.0])
    kind = rnd.choice(["image", "video"])
    r = Rig(server, poll_timeout=timeout, poll_seconds=step)
    server.pending_polls = pending
    if kind == "image":
        rid = r.api.start_deferred("a kite")
        run = lambda: r.api.wait_images(rid, expected=1)
    else:
        rid = r.api.start_video("a lake")
        run = lambda: r.api.wait_video(rid)
    want_polls, want_sleeps, want_outcome = _oracle(pending, timeout, step)
    try:
        run()
        outcome = "done"
    except XaiTimeoutError:
        outcome = "timeout"
    assert (len(server.of_kind(kind + "_poll")), r.sleeps, outcome) == (want_polls, want_sleeps, want_outcome), (seed, pending, timeout, step)
