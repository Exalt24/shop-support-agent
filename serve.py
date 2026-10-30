"""HTTP front for the agent, so an n8n workflow (or anything else) can call it.

    POST /support   {"message": "...", "email": "customer@example.com", "conversation_id": "optional"}  ->  the agent's Result as JSON
    GET  /health

With ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN set (see docs/enquiry-routing.md) the agent also routes enquiries and
captures consented leads, and the answer carries an "enquiry" object. Send the same conversation_id on every turn of a chat.

If SUPPORT_AGENT_KEY is set, requests must carry it in X-Agent-Key. The agent reads orders, so the
endpoint is not left open by default on any machine that sets a key.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent.audit import AuditLog
from agent.config import load_settings
from agent.core import SupportAgent
from agent.leads import desk_from_env
from agent.llm import GroqLLM
from agent.policy import load_policy
from agent.shopify import ShopifyAdmin

HERE = Path(__file__).resolve().parent


class SupportRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    email: str = Field(min_length=3, max_length=320)
    conversation_id: str | None = Field(default=None, max_length=100)  # lets a lead dialogue span turns; without it the email is the key


def build_default_agent() -> SupportAgent:
    s = load_settings()
    return SupportAgent(GroqLLM(s), ShopifyAdmin(s), load_policy(HERE / "policy.md"),
                        AuditLog(HERE / "logs" / "audit.jsonl"), enquiry=desk_from_env())


def create_app(agent_factory: Callable[[], SupportAgent] = build_default_agent, api_key: str | None = None) -> FastAPI:
    app = FastAPI(title="shop-support-agent")
    state: dict = {}
    required = api_key if api_key is not None else os.environ.get("SUPPORT_AGENT_KEY")

    def agent() -> SupportAgent:
        if "agent" not in state:
            state["agent"] = agent_factory()
        return state["agent"]

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/support")
    def support(req: SupportRequest, x_agent_key: str | None = Header(default=None)) -> dict:
        if required and x_agent_key != required:
            raise HTTPException(status_code=401, detail="bad or missing X-Agent-Key")
        r = agent().handle(req.message, req.email, req.conversation_id)
        out = {"action": r.action, "reply": r.reply, "intent": r.intent, "order": r.order_name,
               "escalation_reason": r.escalation_reason, "steps": r.steps}
        if r.enquiry:  # only when a CRM is configured: the queue and follow-up for n8n or a helpdesk to branch on
            out["enquiry"] = {"intent": r.enquiry.intent, "queue": r.enquiry.queue, "follow_up": r.enquiry.follow_up,
                              "lead": r.enquiry.lead_status}
        return out

    return app


app = create_app()
