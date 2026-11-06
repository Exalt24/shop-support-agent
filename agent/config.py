"""Where the keys come from. Environment first, then the saved secret files, never source."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SECRETS_DIR = Path(r"C:\Projects\Professional\Operations\.secrets")
MODEL = os.environ.get("SUPPORT_AGENT_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Anthropic Messages API. Every value below is from the pages saved in docs/vendor-contract/anthropic/ (see its README).
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2026-10-21"
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
LLM_PROVIDERS = ("groq", "anthropic")
SHOPIFY_API_VERSION = "2025-07"


def _read_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


@dataclass(frozen=True)
class Settings:
    groq_key: str
    shop: str
    shopify_token: str
    # The model provider. Groq stays the default, so a deployment that sets nothing new behaves exactly as before.
    llm_provider: str = "groq"
    anthropic_key: str = field(default="", repr=False)  # never printed, even by a stray repr(settings)
    anthropic_workspace: str = ""


def llm_settings_from_env() -> dict[str, str]:
    """Which model provider and which keys, from the environment first and then the saved secret files. Does not demand a key:
    load_settings does that for the provider in use, and the console builds its engine without raising."""
    provider = (os.environ.get("LLM_PROVIDER") or "groq").strip().lower()
    if provider not in LLM_PROVIDERS:
        raise RuntimeError("LLM_PROVIDER must be one of: " + ", ".join(LLM_PROVIDERS))
    return {
        "llm_provider": provider,
        "groq_key": os.environ.get("GROQ_API_KEY") or _read_kv(SECRETS_DIR / "groq_test_key.txt").get("GROQ_API_KEY", ""),
        "anthropic_key": os.environ.get("ANTHROPIC_API_KEY") or _read_kv(SECRETS_DIR / "anthropic_key.txt").get("ANTHROPIC_API_KEY", ""),
        "anthropic_workspace": os.environ.get("ANTHROPIC_WORKSPACE_ID", ""),
    }


def load_settings() -> Settings:
    llm = llm_settings_from_env()
    shop_file = _read_kv(SECRETS_DIR / "shopify_dev_token.txt")
    shop = os.environ.get("SHOPIFY_SHOP") or shop_file.get("SHOP", "")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN") or shop_file.get("ACCESS_TOKEN", "")
    model_key = ("ANTHROPIC_API_KEY", llm["anthropic_key"]) if llm["llm_provider"] == "anthropic" else ("GROQ_API_KEY", llm["groq_key"])
    missing = [n for n, v in (model_key, ("SHOPIFY_SHOP", shop), ("SHOPIFY_ACCESS_TOKEN", token)) if not v]
    if missing:
        raise RuntimeError("missing configuration: " + ", ".join(missing))
    return Settings(shop=shop, shopify_token=token, **llm)
