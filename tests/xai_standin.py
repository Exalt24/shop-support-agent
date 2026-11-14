# LIFESPAN: permanent
"""A stand-in for the xAI API (chat completions, image generation, deferred image results, video generation and video results),
written from the pages saved in docs/vendor-contract/xai-*.txt (retrieved 2026-11-15). It is NOT xAI: it checks what the saved pages
say a caller must send, and answers in the documented shapes, so a test can break a client in either direction (a bad request is
refused here, a bad reply is produced here) without a network, an account or a key. It is a real HTTP server on 127.0.0.1, so the
client's real httpx calls, timeouts and dropped connections are exercised too.

POST /v1/chat/completions  (xai-api-reference-chat-completions.txt, xai-reasoning.txt)
  - "Authorization: Bearer <key>" with a key this server knows, otherwise 401 (xai-debugging-errors.txt)
  - a body key outside the reference's list is refused; model a string naming a model the saved pages list; messages with a role and a
    string content; max_completion_tokens a positive integer; temperature 0 to 2; presence_penalty, frequency_penalty and stop are
    refused for reasoning models; reasoning_effort only for grok-4.7, grok-4.6, grok-4.5 and only low, medium, high or xhigh
POST /v1/images/generations  (xai-image-generation.txt, xai-api-reference-images.txt)
  - prompt required; n 1 to 10; aspect_ratio, resolution, quality and response_format from the documented lists; quality only on
    grok-imagine-image-2.0; deferred only with response_format url. A normal answer costs 400,000,000 ticks an image ($0.04, the
    price on xai-imagine-overview.txt, at 10,000,000,000 ticks a dollar). A deferred request answers {"request_id"} and the job is
    done after `pending_polls` polls
GET /v1/images/{request_id}   202 {"request_id", "status": "pending"}, then 200 done (data, usage) or 200 failed (error)
POST /v1/videos/generations   (xai-video-generation.txt, xai-api-reference-videos.txt)
  - model a video model of the saved pages; prompt required unless an image is given; image {"url"}; duration an integer 1 to 15;
    aspect_ratio and resolution from the documented lists. Answers {"request_id"}
GET /v1/videos/{request_id}   status pending (with progress), then done (video.url, video.duration, video.respect_moderation) or failed

What the saved pages do not give, so this file chooses it: the JSON body of an HTTP error (the stand-in answers {"error": ...}), the
host names of the image and video URLs, whether pending polls are HTTP 200 or 202 for video (a switch, both are served), the answer
for an unknown request_id (404), and the exact wording of a refusal. A request that fails validation is refused first and does not
consume a scripted fault, so a test cannot pass by accident.
"""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Built from fragments so no key-shaped literal sits in the repo; the tests import these.
VALID_KEY = "".join(["xai", "-", "stand", "in", "-key-", "0001", "-abcdef"])
OTHER_KEY = "".join(["xai", "-", "stand", "in", "-key-", "0002", "-abcdef"])
CHAT_MODELS = {"grok-4.7", "grok-4.6", "grok-4.5", "grok-4.3", "grok-4.20-0309-non-reasoning", "grok-4.20-0309-reasoning",
               "grok-4.20-multi-agent-0309", "grok-build-0.1", "latest"}
REASONING_MODELS = {"grok-4.7", "grok-4.6", "grok-4.5"}
EFFORTS = {"low", "medium", "high", "xhigh"}
IMAGE_MODELS = {"grok-imagine-image", "grok-imagine-image-2.0"}
VIDEO_MODELS = {"grok-imagine-video", "grok-imagine-video-1.5", "grok-imagine-video-1.5-lite"}
CHAT_KEYS = {"deferred", "frequency_penalty", "guidelines", "logit_bias", "logprobs", "max_completion_tokens", "max_tokens", "messages",
             "model", "n", "parallel_tool_calls", "presence_penalty", "prompt_cache_key", "reasoning_effort", "response_format",
             "safety_identifier", "search_parameters", "seed", "service_tier", "stop", "stream", "stream_options", "temperature",
             "tool_choice", "tools", "top_logprobs", "top_p", "user", "web_search_options"}
IMAGE_KEYS = {"aspect_ratio", "deferred", "model", "n", "output", "prompt", "quality", "resolution", "response_format",
              "storage_options", "user"}
VIDEO_KEYS = {"aspect_ratio", "duration", "image", "keyframes", "model", "output", "prompt", "reference_audios", "reference_images",
              "resolution", "storage_options", "user", "generate_audio"}
ROLES = ("system", "user", "assistant", "tool", "developer")
IMAGE_RATIOS = {"1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2", "9:19.5", "19.5:9", "9:20", "20:9", "1:2", "2:1", "21:9", "5:2", "auto"}
IMAGE_RESOLUTIONS = {"1k", "2k", "1.5k"}
VIDEO_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "5:2"}
VIDEO_RESOLUTIONS = {"480p", "720p", "1080p"}
TICKS_PER_IMAGE = 400_000_000  # $0.04
TICKS_PER_VIDEO_SECOND = 800_000_000  # "From $0.08 / sec"


def chat_body(content: Any, finish_reason: Optional[str] = "stop", model: str = "grok-4.7", refusal: Optional[str] = None, **extra) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": content, "refusal": refusal}
    body = {"id": "a3d1008e-4544-40d4-d075-11527e794e4a", "object": "chat.completion", "created": 1752854522, "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 32, "completion_tokens": 9, "total_tokens": 135, "cost_in_usd_ticks": 123456,
                      "prompt_tokens_details": {"text_tokens": 32, "audio_tokens": 0, "image_tokens": 0, "cached_tokens": 6},
                      "completion_tokens_details": {"reasoning_tokens": 94, "audio_tokens": 0, "accepted_prediction_tokens": 0,
                                                    "rejected_prediction_tokens": 0}, "num_sources_used": 0},
            "system_fingerprint": "fp_3a7881249c"}
    body.update(extra)
    return body


class Fault:
    """What the next valid request of a kind gets instead of the normal answer."""

    def __init__(self, status: int = 200, body: Any = None, headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None,
                 drop: bool = False, delay: float = 0.0):
        self.status, self.body, self.headers, self.raw, self.drop, self.delay = status, body, headers or {}, raw, drop, delay


def http_error(status: int, message: str = "scripted", headers: Optional[Dict[str, str]] = None) -> Fault:
    return Fault(status, {"error": message}, headers)


def refuse(message: str) -> Fault:
    return Fault(400, {"error": message})


class StandIn:
    def __init__(self, pending_polls: int = 2, video_pending_status: int = 200):
        self.answer = "Your order has shipped."
        self.responder = None  # (system, user) -> reply text, for tests that drive the whole agent
        self.pending_polls = pending_polls  # how many polls of a job answer "pending" before it is done
        self.video_pending_status = video_pending_status
        self.job_failure: Optional[Dict[str, str]] = None  # {"code", "message"}: the next job ends failed
        self.video_moderation_ok = True
        self.requests: List[Dict[str, Any]] = []  # every request in arrival order, each with a "kind"
        self.faults: Dict[str, List[Fault]] = {}
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._issued = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True
            wbufsize = 65536

            def log_message(self, *args):  # silence
                pass

            def do_POST(self):
                outer._handle(self, "POST")

            def do_GET(self):
                outer._handle(self, "GET")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.origin = "http://127.0.0.1:%d" % self._server.server_address[1]
        self.base = self.origin + "/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def start(self) -> "StandIn":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script(self, kind: str, *faults: Fault) -> None:
        """kind is chat, image, image_poll, video or video_poll."""
        with self._lock:
            self.faults.setdefault(kind, []).extend(faults)

    def of_kind(self, kind: str, valid: bool = True) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["kind"] == kind and (r["valid"] or not valid)]

    def all_text(self) -> str:
        """Everything that crossed the wire as text, for a test that looks for a secret where it must not be."""
        return "\n".join(r["raw"].decode("utf-8", "replace") + json.dumps(r["headers"]) + r["path"] for r in self.requests)

    # ---- one request -----------------------------------------------------------------------------------------------------

    def _send(self, h: BaseHTTPRequestHandler, status: int, body: Any, headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None) -> None:
        payload = raw if raw is not None else json.dumps(body).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(payload)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        try:
            h.wfile.write(payload)
            h.wfile.flush()
        except OSError:  # the client gave up (a timeout test): nothing to answer
            pass

    @staticmethod
    def _route(method: str, path: str) -> str:
        parts = [p for p in path.split("/") if p]
        if parts[:1] != ["v1"]:
            return "other"
        rest = parts[1:]
        if rest == ["chat", "completions"]:
            return "chat" if method == "POST" else "wrong_method"
        if rest == ["images", "generations"]:
            return "image" if method == "POST" else "wrong_method"
        if rest == ["videos", "generations"]:
            return "video" if method == "POST" else "wrong_method"
        if len(rest) == 2 and rest[0] == "images":
            return "image_poll" if method == "GET" else "wrong_method"
        if len(rest) == 2 and rest[0] == "videos":
            return "video_poll" if method == "GET" else "wrong_method"
        return "other"

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in h.headers.items()}
        path = urlparse(h.path).path
        kind = self._route(method, path)
        record: Dict[str, Any] = {"kind": kind, "method": method, "path": h.path, "request_line": h.requestline, "headers": headers,
                                  "raw": raw, "body": None, "valid": False}
        with self._lock:
            self.requests.append(record)
        if kind == "other":
            self._send(h, 404, {"error": "not found"})
            return
        if kind == "wrong_method":
            self._send(h, 405, {"error": "method not allowed"})
            return
        problem = self._validate(kind, method, path, headers, raw, record)
        if problem is not None:
            self._send(h, problem.status, problem.body, problem.headers)
            return
        record["valid"] = True
        with self._lock:
            queue = self.faults.get(kind, [])
            fault = queue.pop(0) if queue else None
        if fault is not None:
            if fault.delay:
                time.sleep(fault.delay)
            if fault.drop:
                h.close_connection = True
                h.connection.close()
                return
            if not (fault.status == 200 and fault.body is None and fault.raw is None):  # a bare Fault() is the normal answer
                self._send(h, fault.status, fault.body, fault.headers, fault.raw)
                return
        status, body = self._answer(kind, path, record["body"])
        self._send(h, status, body)

    # ---- validation ------------------------------------------------------------------------------------------------------

    def _validate(self, kind: str, method: str, path: str, headers: Dict[str, str], raw: bytes, record: Dict[str, Any]) -> Optional[Fault]:
        auth = headers.get("authorization", "")
        key = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if key != VALID_KEY:
            return Fault(401, {"error": "Incorrect API key provided"})
        if method == "GET":
            return None
        if "application/json" not in headers.get("content-type", ""):
            return Fault(415, {"error": "content-type must be application/json"})
        try:
            body = json.loads(raw)
        except ValueError:
            return refuse("The request body is not valid JSON")
        record["body"] = body
        if not isinstance(body, dict):
            return refuse("The request body must be a JSON object")
        return {"chat": self._validate_chat, "image": self._validate_image, "video": self._validate_video}[kind](body)

    @staticmethod
    def _is_int(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)

    def _validate_chat(self, body: Dict[str, Any]) -> Optional[Fault]:
        for k in body:
            if k not in CHAT_KEYS:
                return Fault(422, {"error": "%s: unknown field" % k})
        model = body.get("model")
        if not isinstance(model, str) or model not in CHAT_MODELS:
            return refuse("model: a model from the documentation is required")
        if "reasoning_effort" in body and (model not in REASONING_MODELS or body["reasoning_effort"] not in EFFORTS):
            return refuse("reasoning_effort: not supported for this model, or not low, medium, high or xhigh")
        if model.endswith("-reasoning") or model in REASONING_MODELS:
            for k in ("presence_penalty", "frequency_penalty", "stop"):
                if k in body:
                    return refuse("%s cannot be used with reasoning models" % k)
        cap = body.get("max_completion_tokens")
        if "max_completion_tokens" in body and (not self._is_int(cap) or cap < 1):
            return Fault(422, {"error": "max_completion_tokens: must be a positive integer"})
        if "temperature" in body and (not isinstance(body["temperature"], (int, float)) or isinstance(body["temperature"], bool)
                                      or not 0 <= body["temperature"] <= 2):
            return Fault(422, {"error": "temperature: must be a number from 0 to 2"})
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return refuse("messages: Field required")
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in ROLES or not isinstance(m.get("content"), (str, list)):
                return Fault(422, {"error": "messages: each needs a role in lower case and a content string"})
        return None

    def _validate_image(self, body: Dict[str, Any]) -> Optional[Fault]:
        for k in body:
            if k not in IMAGE_KEYS:
                return Fault(422, {"error": "%s: unknown field" % k})
        if not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
            return refuse("prompt: Field required")
        model = body.get("model")
        if model is not None and model not in IMAGE_MODELS:
            return refuse("model: not an image model")
        if "n" in body and (not self._is_int(body["n"]) or not 1 <= body["n"] <= 10):
            return Fault(422, {"error": "n: 1 to 10"})
        if "aspect_ratio" in body and body["aspect_ratio"] not in IMAGE_RATIOS:
            return Fault(422, {"error": "aspect_ratio: not one of the documented ratios"})
        if "resolution" in body and body["resolution"] not in IMAGE_RESOLUTIONS:
            return Fault(422, {"error": "resolution: 1k or 2k"})
        if "quality" in body and (body["quality"] not in ("low", "medium", "auto") or model != "grok-imagine-image-2.0"):
            return Fault(422, {"error": "quality: low, medium or auto, and only for grok-imagine-image-2.0"})
        if "response_format" in body and body["response_format"] not in ("url", "b64_json"):
            return Fault(422, {"error": "response_format: url or b64_json"})
        if body.get("deferred") is not None and not isinstance(body["deferred"], bool):
            return Fault(422, {"error": "deferred: boolean"})
        if body.get("deferred") and body.get("response_format", "url") != "url":
            return refuse("Only response_format url is supported when deferred")
        return None

    def _validate_video(self, body: Dict[str, Any]) -> Optional[Fault]:
        for k in body:
            if k not in VIDEO_KEYS:
                return Fault(422, {"error": "%s: unknown field" % k})
        model = body.get("model")
        if model is not None and model not in VIDEO_MODELS:
            return refuse("model: not a video model")
        image = body.get("image")
        if image is not None and (not isinstance(image, dict) or not isinstance(image.get("url"), str) or not image["url"]):
            return Fault(422, {"error": "image: needs a url"})
        if image is None and (not isinstance(body.get("prompt"), str) or not body["prompt"].strip()):
            return refuse("prompt: required for text to video")
        if "prompt" in body and not isinstance(body["prompt"], str):
            return Fault(422, {"error": "prompt: string"})
        if "duration" in body and (not self._is_int(body["duration"]) or not 1 <= body["duration"] <= 15):
            return Fault(422, {"error": "duration: 1 to 15"})
        if "aspect_ratio" in body and body["aspect_ratio"] not in VIDEO_RATIOS:
            return Fault(422, {"error": "aspect_ratio: not one of the documented ratios"})
        if "resolution" in body and body["resolution"] not in VIDEO_RESOLUTIONS:
            return Fault(422, {"error": "resolution: 480p, 720p or 1080p"})
        return None

    # ---- the normal answers ----------------------------------------------------------------------------------------------

    def _new_id(self) -> str:
        with self._lock:
            self._issued += 1
            return "00000000-0000-4000-8000-%012d" % self._issued

    def _images(self, body: Dict[str, Any], count: int) -> List[Dict[str, Any]]:
        out = []
        for i in range(count):
            if body.get("response_format") == "b64_json":
                out.append({"b64_json": base64.b64encode(b"STANDIN-IMAGE-%d" % i).decode(), "mime_type": "image/jpeg"})
            else:
                out.append({"url": "https://stand-in.invalid/xai-tmp-imgen-%d.jpeg" % i, "mime_type": "image/jpeg"})
        return out

    def _answer(self, kind: str, path: str, body: Optional[Dict[str, Any]]):
        if kind == "chat":
            msgs = body["messages"]
            reply = self.answer
            if self.responder:
                system = next((m["content"] for m in msgs if m["role"] == "system"), "")
                reply = self.responder(system, msgs[-1]["content"])
            return 200, chat_body(reply, model=body["model"])
        if kind == "image":
            n = body.get("n", 1)
            if body.get("deferred"):
                rid = self._new_id()
                with self._lock:
                    self._jobs[rid] = {"kind": "image", "n": n, "polls": 0, "failure": self.job_failure, "body": body}
                    self.job_failure = None
                return 200, {"request_id": rid}
            return 200, {"data": self._images(body, n), "usage": {"cost_in_usd_ticks": n * TICKS_PER_IMAGE}}
        if kind == "video":
            rid = self._new_id()
            with self._lock:
                self._jobs[rid] = {"kind": "video", "polls": 0, "failure": self.job_failure, "body": body,
                                   "moderation_ok": self.video_moderation_ok}
                self.job_failure = None
            return 200, {"request_id": rid}
        rid = path.rsplit("/", 1)[1]
        with self._lock:
            job = self._jobs.get(rid)
            if job is None or job["kind"] != kind.split("_")[0]:
                return 404, {"error": "unknown request id"}
            job["polls"] += 1
            polls = job["polls"]
        if polls <= self.pending_polls:
            if kind == "image_poll":
                return 202, {"request_id": rid, "status": "pending"}
            progress = min(99, polls * 30)
            return self.video_pending_status, {"status": "pending", "progress": progress, "model": None}
        if job["failure"]:
            return 200, {"request_id": rid, "status": "failed", "error": job["failure"]}
        if kind == "image_poll":
            n = job["n"]
            return 200, {"request_id": rid, "status": "done", "data": self._images(job["body"], n),
                         "usage": {"cost_in_usd_ticks": n * TICKS_PER_IMAGE}}
        seconds = job["body"].get("duration", 8)
        video = {"duration": seconds, "respect_moderation": job["moderation_ok"]}
        if job["moderation_ok"]:
            video["url"] = "https://stand-in.invalid/xai-video-%s.mp4" % rid
        return 200, {"status": "done", "video": video, "model": job["body"].get("model", "grok-imagine-video-1.5"), "progress": 100,
                     "usage": {"cost_in_usd_ticks": seconds * TICKS_PER_VIDEO_SECOND}}
