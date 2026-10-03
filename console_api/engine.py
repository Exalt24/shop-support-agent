"""Builds the agent engine for ONE store: its Shopify connection and its policy. A new client is a new tenant row,
not new code."""
from __future__ import annotations

import os

from agent.config import SECRETS_DIR, Settings, _read_kv
from agent.core import SupportAgent
from agent.llm import GroqLLM
from agent.policy import parse_policy
from agent.shopify import ShopifyAdmin

from .secrets_store import decrypt_token


def _groq_key() -> str:
    return os.environ.get("GROQ_API_KEY") or _read_kv(SECRETS_DIR / "groq_test_key.txt").get("GROQ_API_KEY", "")


def _settings(tenant: dict) -> Settings:
    return Settings(groq_key=_groq_key(), shop=tenant["shop_domain"],
                    shopify_token=decrypt_token(tenant["shopify_token_enc"]))


def store_shop(tenant: dict) -> ShopifyAdmin:
    """The store's Shopify client, for the staff lookup panel."""
    return ShopifyAdmin(_settings(tenant))


def build_agent(tenant: dict) -> SupportAgent:
    s = _settings(tenant)
    return SupportAgent(GroqLLM(s), ShopifyAdmin(s), parse_policy(tenant["policy_md"]), audit=None)
