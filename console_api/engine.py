"""Builds the agent engine for ONE store: its Shopify connection and its policy. A new client is a new tenant row,
not new code."""
from __future__ import annotations

from agent.config import Settings, llm_settings_from_env
from agent.core import SupportAgent
from agent.llm import build_llm
from agent.policy import parse_policy
from agent.shopify import ShopifyAdmin

from .secrets_store import decrypt_token


def _settings(tenant: dict) -> Settings:
    return Settings(shop=tenant["shop_domain"], shopify_token=decrypt_token(tenant["shopify_token_enc"]), **llm_settings_from_env())


def store_shop(tenant: dict) -> ShopifyAdmin:
    """The store's Shopify client, for the staff lookup panel."""
    return ShopifyAdmin(_settings(tenant))


def build_agent(tenant: dict) -> SupportAgent:
    s = _settings(tenant)
    return SupportAgent(build_llm(s), ShopifyAdmin(s), parse_policy(tenant["policy_md"]), audit=None)
