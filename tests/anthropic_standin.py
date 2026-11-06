# LIFESPAN: permanent
"""A stand-in for the Anthropic Messages API, written from the pages saved in docs/vendor-contract/anthropic/ (retrieved
2026-11-06). It is NOT Anthropic: it checks what the saved pages say a caller must send, and answers in the documented shapes,
so a test can break the client in either direction (a bad request is refused here, a bad reply is produced here) without a
network, a key or a credit. It is a real HTTP server on 127.0.0.1, so the client's real httpx call, timeouts and dropped
connections are exercised too.

POST /v1/messages
  - auth: "Authorization: Bearer <key>" or "x-api-key: <key>" (the overview page: Authorization is required "unless x-api-key is
    set"); a key this server does not know is 401 authentication_error
  - "anthropic-version" must be sent (the overview page: Required "Yes"); the value must be the one the saved versioning page
    names; "content-type" must be application/json
  - body: model (a model id from the models overview), max_tokens (a positive integer) and messages are required; messages end
    with a user message (api-errors.txt: "The conversation must end with a user message"); system is a string or text blocks;
    temperature, top_p and top_k are refused (api-versioning.txt, 2026-10-21: "returns a 400 error on every model");
    thinking: between_tools only for claude-sonnet-5-5, disabled and enabled refused for it (api-errors.txt); any other key
    is "Extra inputs are not permitted" (the phrase api-errors.txt shows for an unknown field)
  - the messages the pages do not give (the exact wording of a missing header, a bad max_tokens) are this file's own and say so
  - errors are {"type": "error", "error": {"type", "message"}, "request_id"} with a request-id header (api-errors.txt)

Faults are queued with `script(...)`; each valid request consumes the next one, and an empty queue (or a bare Fault()) means a
normal answer.
A request that fails validation is refused first and does not consume a fault, so a test cannot pass by accident.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

VERSION = "2026-10-21"  # api-versioning.txt
MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}  # models-overview.txt
SONNET = "claude-sonnet-5-5"
ALLOWED_KEYS = {"model", "max_tokens", "messages", "system", "thinking", "metadata", "stop_sequences", "stream", "tools",
                "tool_choice", "cache_control", "container", "diagnostics", "inference_geo", "output_config", "service_tier", "speed"}
REMOVED = ("temperature", "top_p", "top_k")  # api-versioning.txt
VALID_KEYS = {"sk-ant-valid-key-0001"}


def error_body(error_type: str, message: str, request_id: str = "req_011CSHoEeqs5C35K2UUqR7Fy", **extra) -> Dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message, **extra}, "request_id": request_id}


def message_body(content: List[Dict[str, Any]], stop_reason: Optional[str] = "end_turn", model: str = SONNET, **extra) -> Dict[str, Any]:
    body = {"id": "msg_013Zva2CMHLNnXjNJJKqJ2EF", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop_reason, "stop_sequence": None, "stop_details": None,
            "usage": {"input_tokens": 12, "output_tokens": 9}}
    body.update(extra)
    return body


def text(s: str) -> Dict[str, Any]:
    return {"type": "text", "text": s}


def thinking(s: str = "...") -> Dict[str, Any]:
    return {"type": "thinking", "thinking": s, "signature": "sig"}


class Fault:
    """What the next valid request gets instead of the normal answer."""

    def __init__(self, status: int = 200, body: Any = None, headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None,
                 drop: bool = False, delay: float = 0.0):
        self.status, self.body, self.headers, self.raw, self.drop, self.delay = status, body, headers or {}, raw, drop, delay


def http_error(status: int, error_type: str, message: str = "scripted", headers: Optional[Dict[str, str]] = None, **extra) -> Fault:
    return Fault(status, error_body(error_type, message, **extra), headers)


def refuse_validation(message: str) -> Fault:
    return Fault(400, error_body("invalid_request_error", message))


class StandIn:
    def __init__(self, responder: Optional[Callable[[str, str], str]] = None):
        self.answer = "Your order has shipped."
        self.responder = responder  # (system, user) -> reply text, for tests that drive the whole agent
        self.requests: List[Dict[str, Any]] = []
        self.faults: List[Fault] = []
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
        self.url = self.base + "/v1/messages"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def start(self) -> "StandIn":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script(self, *faults: Fault) -> None:
        with self._lock:
            self.faults.extend(faults)

    def valid_requests(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["valid"]]

    # ---- one request ---------------------------------------------------------------------------------------------------

    def _send(self, h: BaseHTTPRequestHandler, status: int, body: Any, headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None) -> None:
        payload = raw if raw is not None else json.dumps(body).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(payload)))
        h.send_header("request-id", "req_018EeWyXxfu5pfWkrYcMdjWG")
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
        record: Dict[str, Any] = {"path": h.path, "headers": headers, "raw": raw, "body": None, "valid": False}
        with self._lock:
            self.requests.append(record)
        problem = self._validate(h.path, headers, raw, record)
        if problem is not None:
            self._send(h, problem.status, problem.body, problem.headers)
            return
        record["valid"] = True
        with self._lock:
            fault = self.faults.pop(0) if self.faults else None
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
        body = record["body"]
        reply = self.answer
        if self.responder:
            system = body.get("system", "")
            system = system if isinstance(system, str) else "".join(b.get("text", "") for b in system)
            reply = self.responder(system, body["messages"][-1]["content"])
        self._send(h, 200, message_body([text(reply)], model=body["model"]))

    def _validate(self, path: str, headers: Dict[str, str], raw: bytes, record: Dict[str, Any]) -> Optional[Fault]:
        if path != "/v1/messages":
            return Fault(404, error_body("not_found_error", "The requested resource could not be found."))
        auth = headers.get("authorization", "")
        key = auth[len("Bearer "):] if auth.startswith("Bearer ") else headers.get("x-api-key", "")
        if key not in VALID_KEYS:
            return Fault(401, error_body("authentication_error", "invalid x-api-key"))
        if "anthropic-version" not in headers:
            return refuse_validation("anthropic-version: header is required")  # wording is this file's own
        if headers["anthropic-version"] != VERSION:
            return refuse_validation("anthropic-version: %s is not a version this stand-in knows" % headers["anthropic-version"])
        if "application/json" not in headers.get("content-type", ""):
            return refuse_validation("content-type must be application/json")
        try:
            body = json.loads(raw)
        except ValueError:
            return refuse_validation("The request body is not valid JSON")
        record["body"] = body
        if not isinstance(body, dict):
            return refuse_validation("The request body must be a JSON object")
        for k in body:
            if k not in ALLOWED_KEYS:
                return refuse_validation("%s: Extra inputs are not permitted" % k)
        for k in REMOVED:
            if k in body:  # api-versioning.txt
                return refuse_validation("%s: this parameter is removed in API version 2026-10-21" % k)
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return refuse_validation("model: Field required")
        if model not in MODELS:
            return Fault(404, error_body("not_found_error", "model: %s" % model))
        mt = body.get("max_tokens")
        if not isinstance(mt, int) or isinstance(mt, bool) or mt < 1:
            return refuse_validation("max_tokens: must be a positive integer")  # wording is this file's own
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return refuse_validation("messages: Field required")
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), (str, list)):
                return refuse_validation("messages: each message needs a role (user or assistant) and content")
        if messages[-1]["role"] != "user":
            return refuse_validation("This model does not support assistant message prefill. The conversation must end with a user message.")
        system = body.get("system")
        if system is not None and not (isinstance(system, str) or (isinstance(system, list) and all(
                isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) for b in system))):
            return refuse_validation("system: must be a string or an array of text blocks")
        if "thinking" in body:
            t = body["thinking"]
            kind = t.get("type") if isinstance(t, dict) else None
            if kind == "between_tools" and model != SONNET:
                return refuse_validation('"thinking.type.between_tools" is not supported for this model.')
            if kind == "disabled" and model == SONNET:
                return refuse_validation('To turn thinking off on this model, send "thinking": {"type": "between_tools"} instead of {"type": "disabled"}.')
            if kind == "enabled" and model == SONNET:
                return refuse_validation('"thinking.type.enabled" is not supported for this model.')
            if kind not in ("between_tools", "adaptive", "disabled", "enabled"):
                return refuse_validation("thinking.type: unknown value")
        return None
