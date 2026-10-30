"""The chat widget on two demo websites, with a scripted model and a fake Zoho. NOTHING HERE IS LIVE.

    python demo_widget.py

Starts the chat server on :8100 and the two demo sites on :8101 and :8102, then prints the three addresses. Open
http://127.0.0.1:8101/site_a.html and http://127.0.0.1:8102/site_b.html. Ask either for a quote ("can I get a quote for a new
boiler? My name is Tom Reyes and my email is tom@example.com"), press the yes button, and the lead lands in that site's own fake
Zoho. Ctrl-C stops it. tests/test_widget_browser.py starts the same stack on free ports and drives both pages in Chromium.

The model is a keyword script (KeywordModel): it stands in for the real triage model so a run needs no key and gives the same
answer every time. Zoho is agent/zoho_fake.py, one fake per tenant, so a lead for one site can never show up in the other.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn

from agent.chat_api import ChatBackend
from agent.chat_guard import ConversationSigner
from agent.core import TRIAGE_SYSTEM, SupportAgent
from agent.llm import LLMError
from agent.tenants import TenantRegistry, desk_for
from agent.zoho_crm import ZohoCrmClient, ZohoSettings
from agent.zoho_fake import FakeZoho
from serve import create_app

HERE = Path(__file__).resolve().parent
DEMO_DIR = HERE / "widget" / "demo"
DEFAULT_CHAT, DEFAULT_A, DEFAULT_B = 8100, 8101, 8102
CALM = {"angry": False, "legal_threat": False, "chargeback": False, "wants_human": False}
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
PHONE = re.compile(r"\+?\d[\d ()\-]{7,16}\d")
NAME = re.compile(r"\b(?i:my name is) ([A-Z][a-z]+(?: [A-Z][a-z]+)?)")


class KeywordModel:
    """The scripted stand-in for the triage model: a fixed keyword table, and lead values copied from the visitor's words."""

    def complete(self, system, user, *, max_tokens=1800):
        if system != TRIAGE_SYSTEM:
            raise LLMError("the demo model only triages")
        text = user.lower()
        if re.search(r"\b(quote|estimate)\b", text):
            intent = "quote_request"
        elif re.search(r"\b(leak|burst|repair|broken|service|visit|not working|boiler is)\b", text):
            intent = "service_request"
        elif re.search(r"\b(buy|bulk|wholesale|pricing|price|stock|order of)\b", text):
            intent = "sales_enquiry"
        else:
            intent = "general"
        lead = {}
        for key, rx in (("name", NAME), ("email", EMAIL), ("phone", PHONE)):
            m = rx.search(user)
            if m:
                lead[key] = m.group(1) if key == "name" else m.group(0)
        return json.dumps({"intent": "other", "order_number": None, "product_query": None, "enquiry_intent": intent,
                           "lead": lead, **CALM})


class NoShop:
    """These demo sites have no store behind them: the widget is about enquiries."""

    def find_order(self, order_name, email):
        return None

    def search_products(self, text, limit=3):
        return []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tenants_for(origins: dict[str, str], path: Path = DEMO_DIR / "tenants.demo.json") -> dict:
    """The demo tenants file with each tenant's allowed origin swapped for the one this run really serves it from."""
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg = copy.deepcopy(cfg)
    for t in cfg["tenants"]:
        t["allowed_origins"] = [origins[t["id"]]]
    return cfg


class StaticSite(ThreadingHTTPServer):
    """Serves widget/demo/*.html. The pages name the default chat address; each response swaps in the one in use."""

    def __init__(self, port: int, chat_origin: str):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                name = self.path.split("?")[0].lstrip("/")
                target = DEMO_DIR / name
                if not name.endswith(".html") or "/" in name or not target.is_file():
                    self.send_error(404)
                    return
                body = target.read_text(encoding="utf-8").replace("http://127.0.0.1:%d" % DEFAULT_CHAT, chat_origin).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        super().__init__(("127.0.0.1", port), Handler)
        self.port = port


class DemoStack:
    """The real chat server (uvicorn) and the demo sites, on the given ports (0 or None: a free one each)."""

    def __init__(self, chat_port: int | None = None, a_port: int | None = None, b_port: int | None = None,
                 extra_site_ports: int = 0, **backend_options):
        self.chat_port, a_port, b_port = chat_port or free_port(), a_port or free_port(), b_port or free_port()
        self.chat_origin = "http://127.0.0.1:%d" % self.chat_port
        self.origins = {"harbour-heating": "http://127.0.0.1:%d" % a_port, "loomhouse-homewares": "http://127.0.0.1:%d" % b_port}
        self.registry = TenantRegistry.from_dict(tenants_for(self.origins))
        self.zoho = {t.zoho: FakeZoho() for t in self.registry.tenants}  # one fake CRM per tenant
        settings = ZohoSettings("cid", "csecret", "rtoken")

        def agent_for(tenant):
            crm = ZohoCrmClient(settings, self.zoho[tenant.zoho], sleep=lambda s: None)
            return SupportAgent(KeywordModel(), NoShop(), [], None, None, desk_for(tenant, crm))

        self.backend = ChatBackend(self.registry, ConversationSigner(secrets.token_hex(32).encode()), agent_for, **backend_options)
        self.app = create_app(chat=self.backend)
        self._uvicorn = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.chat_port, log_level="error"))
        self._sites = [StaticSite(a_port, self.chat_origin), StaticSite(b_port, self.chat_origin)]
        self._sites += [StaticSite(free_port(), self.chat_origin) for _ in range(extra_site_ports)]
        self._threads: list[threading.Thread] = []

    @property
    def site_a(self) -> str:
        return self.origins["harbour-heating"] + "/site_a.html"

    @property
    def site_b(self) -> str:
        return self.origins["loomhouse-homewares"] + "/site_b.html"

    @property
    def stranger_origins(self) -> list[str]:
        """Origins that serve the same pages and belong to no tenant."""
        return ["http://127.0.0.1:%d" % s.port for s in self._sites[2:]]

    def start(self) -> "DemoStack":
        self._threads = [threading.Thread(target=self._uvicorn.run, daemon=True)]
        self._threads += [threading.Thread(target=s.serve_forever, daemon=True) for s in self._sites]
        for t in self._threads:
            t.start()
        for _ in range(200):  # poll in short steps for the server to be up
            if self._uvicorn.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("the chat server did not start")

    def stop(self) -> None:
        self._uvicorn.should_exit = True
        for s in self._sites:
            s.shutdown()
            s.server_close()
        for t in self._threads:
            t.join(timeout=5)

    def __enter__(self) -> "DemoStack":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def main() -> int:
    stack = DemoStack(DEFAULT_CHAT, DEFAULT_A, DEFAULT_B).start()
    print("chat server  %s  (scripted model, fake Zoho, nothing live)" % stack.chat_origin)
    print("site A       %s" % stack.site_a)
    print("site B       %s" % stack.site_b)
    print("Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        stack.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
