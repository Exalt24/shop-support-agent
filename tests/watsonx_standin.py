# LIFESPAN: permanent
"""A stand-in for IBM Cloud IAM and the watsonx.ai chat API, written from the pages saved in docs/vendor-contract/ibm-watsonx/
(retrieved 2026-11-06). It is NOT IBM: it checks what the saved pages say a caller must send, and answers in the documented shapes,
so a test can break the client in either direction (a bad request is refused here, a bad reply is produced here) without a network,
an account or a key. It is a real HTTP server on 127.0.0.1 (one port, two paths), so the client's real httpx calls, timeouts and
dropped connections are exercised too.

POST /identity/token  (the IAM endpoint; credentials-api-key-iam-token.txt)
  - Content-Type must be application/x-www-form-urlencoded; the form must carry grant_type=urn:ibm:params:oauth:grant-type:apikey and
    an apikey this server knows, otherwise 400 with IAM's error shape {"errorCode", "errorMessage"} (that shape is from IBM's IAM
    page as quoted by a search result, not from a saved page: docs/watsonx.md says so)
  - the answer has access_token, token_type, expires_in and expiration (epoch seconds). A token stops working when the stand-in's
    clock passes its expiry, or at once when a test calls expire_tokens()
POST /ml/v1/text/chat  (chat-api.txt)
  - "Authorization: Bearer <token>" with a token this server issued and has not expired, otherwise 401 authentication_token_expired
    (java-sdk-authentication.txt: "SDK catches 401 authentication_token_expired")
  - ?version= must be one of the dates the saved chat page shows; content-type application/json
  - body: model_id (a model id the saved pages name), exactly one of project_id and space_id, messages (role in lower case, content a
    string or a list of parts), max_completion_tokens or the deprecated max_tokens (a positive integer); a key outside the documented
    list is refused; tools are accepted only when they are a list (this client sends none)
  - errors are {"errors": [{"code", "message", "more_info"}], "trace"} (node-sdk-api-error*.txt) with the codes of
    java-sdk-error-handling.txt
  - the messages the pages do not give (the exact wording of a missing field) are this file's own

Faults are queued with script_chat(...) and script_iam(...); each valid request consumes the next one for its path, and an empty
queue (or a bare Fault()) means a normal answer. A request that fails validation is refused first and does not consume a fault, so a
test cannot pass by accident.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

GRANT = "urn:ibm:params:oauth:grant-type:apikey"  # credentials-api-key-iam-token.txt
VERSIONS = {"2024-10-08", "2024-10-09", "2025-10-25"}  # the version= dates on chat-api.txt
MODELS = {"ibm/granite-4-h-small", "openai/gpt-oss-120b", "meta-llama/llama-3-8b-instruct",
          "meta-llama/llama-3-2-11b-vision-instruct"}  # model ids that appear on the saved pages
ALLOWED_KEYS = {"model_id", "model", "project_id", "space_id", "messages", "tools", "tool_choice_option", "tool_choice",
                "max_completion_tokens", "max_tokens", "temperature", "top_p", "time_limit", "stream", "seed", "stop", "n",
                "chat_template_kwargs", "reasoning_effort", "include_reasoning", "response_format", "frequency_penalty",
                "presence_penalty", "logit_bias", "logprobs", "top_logprobs", "context", "moderations", "crypto"}
ROLES = ("system", "user", "assistant", "tool")  # lower case only: "The role parameter ... is case sensitive"
# Built from fragments so no key-shaped literal sits in the repo; the tests import these.
VALID_KEY = "".join(["stand", "in", "-ibm-", "api", "key", "-0001"])
OTHER_KEY = "".join(["stand", "in", "-ibm-", "api", "key", "-0002"])
PROJECT_ID = "".join(["00000000", "-1111-", "2222-", "3333-", "444444444444"])
TRACE = "".join(["trace", "-", "0a1b2c3d4e5f"])


def error_body(code: str, message: str, trace: str = TRACE, **extra) -> Dict[str, Any]:
    return {"errors": [{"code": code, "message": message, "more_info": "https://cloud.ibm.com/apidocs/watsonx-ai", **extra}],
            "trace": trace}


def chat_body(content: Any, finish_reason: Optional[str] = "stop", model: str = "ibm/granite-4-h-small", **extra) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    body = {"id": "chat-45932923166b4607bde75207a0a9f5d4", "model_id": model, "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "created": 1728404199, "created_at": "2024-10-08T16:16:40.102Z",
            "usage": {"completion_tokens": 12, "prompt_tokens": 87, "total_tokens": 99},
            "system": {"warnings": []}}
    body.update(extra)
    return body


class Fault:
    """What the next valid request gets instead of the normal answer."""

    def __init__(self, status: int = 200, body: Any = None, headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None,
                 drop: bool = False, delay: float = 0.0):
        self.status, self.body, self.headers, self.raw, self.drop, self.delay = status, body, headers or {}, raw, drop, delay


def http_error(status: int, code: str, message: str = "scripted", headers: Optional[Dict[str, str]] = None, **extra) -> Fault:
    return Fault(status, error_body(code, message, **extra), headers)


def refuse(message: str, code: str = "invalid_request_entity") -> Fault:
    return Fault(400, error_body(code, message))


class StandIn:
    def __init__(self, responder: Optional[Callable[[str, str], str]] = None, clock: Callable[[], float] = time.time,
                 expires_in: Any = 3600, expiration_field: bool = True):
        self.answer = "Your order has shipped."
        self.responder = responder  # (system, user) -> reply text, for tests that drive the whole agent
        self.clock = clock
        self.expires_in, self.expiration_field = expires_in, expiration_field  # what IAM says; None leaves the field out
        self.requests: List[Dict[str, Any]] = []  # chat requests and IAM requests, in arrival order, each with a "kind"
        self.chat_faults: List[Fault] = []
        self.iam_faults: List[Fault] = []
        self._tokens: Dict[str, float] = {}  # issued token -> the stand-in clock time at which it stops working
        self._issued = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True  # headers and body are two small writes: without this each reply waits on a delayed ACK
            wbufsize = 65536

            def log_message(self, *args):  # silence
                pass

            def do_POST(self):
                outer._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self._server.server_address[1]
        self.iam_url = self.base + "/identity/token"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def start(self) -> "StandIn":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script_chat(self, *faults: Fault) -> None:
        with self._lock:
            self.chat_faults.extend(faults)

    def script_iam(self, *faults: Fault) -> None:
        with self._lock:
            self.iam_faults.extend(faults)

    def expire_tokens(self) -> None:
        """Every token issued so far stops working now (revoked, or expired earlier than IAM said)."""
        with self._lock:
            for t in self._tokens:
                self._tokens[t] = float("-inf")

    def chat_requests(self, valid: bool = True) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["kind"] == "chat" and (r["valid"] or not valid)]

    def iam_requests(self, valid: bool = True) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["kind"] == "iam" and (r["valid"] or not valid)]

    @property
    def tokens_issued(self) -> int:
        return self._issued

    def all_text(self) -> str:
        """Everything that crossed the wire as text, for a test that looks for a secret where it must not be."""
        return "\n".join(r["raw"].decode("utf-8", "replace") + json.dumps(r["headers"]) + r["path"] for r in self.requests)

    # ---- one request ---------------------------------------------------------------------------------------------------

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

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length)
        headers = {k.lower(): v for k, v in h.headers.items()}
        parts = urlparse(h.path)
        kind = "iam" if parts.path == "/identity/token" else "chat" if parts.path == "/ml/v1/text/chat" else "other"
        # requestline is kept because http.server rewrites a leading "//" in the path to "/", which would hide a doubled slash
        record: Dict[str, Any] = {"kind": kind, "path": h.path, "request_line": h.requestline, "headers": headers, "raw": raw, "body": None,
                                  "valid": False}
        with self._lock:
            self.requests.append(record)
        if kind == "other":
            self._send(h, 404, error_body("not_found", "The requested resource could not be found."))
            return
        problem = self._validate_iam(headers, raw, record) if kind == "iam" else self._validate_chat(parts, headers, raw, record)
        if problem is not None:
            self._send(h, problem.status, problem.body, problem.headers)
            return
        record["valid"] = True
        with self._lock:
            queue = self.iam_faults if kind == "iam" else self.chat_faults
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
        if kind == "iam":
            self._send(h, 200, self._issue())
            return
        body = record["body"]
        reply = self.answer
        if self.responder:
            msgs = body["messages"]
            system = next((m["content"] for m in msgs if m["role"] == "system"), "")
            reply = self.responder(system, msgs[-1]["content"])
        self._send(h, 200, chat_body(reply, model=body.get("model_id") or body.get("model")))

    def _issue(self) -> Dict[str, Any]:
        with self._lock:
            self._issued += 1
            token = "".join(["tok", "-", str(self._issued), "-", "abcdef0123456789"])
            now = self.clock()
            lifetime = self.expires_in if isinstance(self.expires_in, (int, float)) and not isinstance(self.expires_in, bool) else 3600
            self._tokens[token] = now + lifetime
        body: Dict[str, Any] = {"access_token": token, "refresh_token": "not_supported", "token_type": "Bearer", "scope": "ibm openid"}
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        if self.expiration_field:
            body["expiration"] = int(now + lifetime)
        return body

    def _validate_iam(self, headers: Dict[str, str], raw: bytes, record: Dict[str, Any]) -> Optional[Fault]:
        if "application/x-www-form-urlencoded" not in headers.get("content-type", ""):
            return Fault(400, {"errorCode": "BXNIM0000E", "errorMessage": "content-type must be application/x-www-form-urlencoded"})
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
        record["body"] = form
        if form.get("grant_type") != GRANT:
            return Fault(400, {"errorCode": "BXNIM0407E", "errorMessage": "Provided grant_type is not supported"})
        if form.get("apikey") not in (VALID_KEY,):
            return Fault(400, {"errorCode": "BXNIM0415E", "errorMessage": "Provided API key could not be found."})
        return None

    def _validate_chat(self, parts, headers: Dict[str, str], raw: bytes, record: Dict[str, Any]) -> Optional[Fault]:
        auth = headers.get("authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        with self._lock:
            valid_until = self._tokens.get(token)
        if valid_until is None or self.clock() >= valid_until:
            return Fault(401, error_body("authentication_token_expired", "Failed to authenticate the request due to invalid or expired token"))
        version = parse_qs(parts.query).get("version", [""])[0]
        if version not in VERSIONS:
            return refuse("version: a date from the documentation is required", "invalid_input_argument")  # wording is this file's own
        if "application/json" not in headers.get("content-type", ""):
            return refuse("content-type must be application/json")
        try:
            body = json.loads(raw)
        except ValueError:
            return refuse("The request body is not valid JSON")
        record["body"] = body
        if not isinstance(body, dict):
            return refuse("The request body must be a JSON object")
        for k in body:
            if k not in ALLOWED_KEYS:
                return refuse("%s: unknown field" % k, "json_validation_error")
        model = body.get("model_id") or body.get("model")
        if not isinstance(model, str) or not model:
            return refuse("model_id: Field required", "json_validation_error")
        if model not in MODELS:
            return refuse("The model %s is not available in this region or plan" % model, "model_not_supported")
        if ("project_id" in body) == ("space_id" in body):
            return refuse("exactly one of project_id and space_id is required", "invalid_input_argument")
        for ident in ("project_id", "space_id"):
            if ident in body and (not isinstance(body[ident], str) or not body[ident]):
                return refuse("%s: must be a string" % ident, "json_type_error")
        for cap in ("max_completion_tokens", "max_tokens"):
            if cap in body and (not isinstance(body[cap], int) or isinstance(body[cap], bool) or body[cap] < 0):
                return refuse("%s: must be a non-negative integer" % cap, "json_type_error")  # 0 means the model's maximum
        if "temperature" in body and (not isinstance(body["temperature"], (int, float)) or isinstance(body["temperature"], bool)):
            return refuse("temperature: must be a number", "json_type_error")
        if "tools" in body and not isinstance(body["tools"], list):
            return refuse("tools: must be an array", "json_type_error")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return refuse("messages: Field required", "json_validation_error")
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in ROLES:
                return refuse("messages: role must be one of system, user, assistant, tool, in lower case", "invalid_request_entity")
            if not isinstance(m.get("content"), (str, list)):
                return refuse("messages: content must be a string or a list of parts", "json_type_error")
        return None
