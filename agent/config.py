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
# IBM watsonx.ai on IBM Cloud. Taken from the pages saved in docs/vendor-contract/ibm-watsonx/ (see its README): the IAM token
# endpoint, the chat path and the version date of the reasoning-model example. The Dallas host is from the brief, not from a saved
# page. WATSONX_URL and WATSONX_MODEL_ID are read when the settings are loaded, so a region or a model is a deployment setting.
WATSONX_IAM_URL = "https://iam.cloud.ibm.com/identity/token"
WATSONX_DEFAULT_URL = "https://us-south.ml.cloud.ibm.com"
WATSONX_API_VERSION = "2025-10-25"
WATSONX_DEFAULT_MODEL_ID = "ibm/granite-4-h-small"
# xAI (Grok chat and Grok Imagine), from the pages saved in docs/vendor-contract/xai-*.txt (retrieved 2026-11-15): the base URL and the
# bearer header are on every code sample there, grok-4.7 is the model of the chat guide, the two imagine model names are on the
# image and video guides. XAI_BASE_URL is read when the settings are loaded, so a regional endpoint is a deployment setting.
XAI_DEFAULT_URL = "https://api.x.ai/v1"
XAI_DEFAULT_MODEL = "grok-4.7"
XAI_IMAGE_MODEL = "grok-imagine-image-2.0"
XAI_VIDEO_MODEL = "grok-imagine-video-1.5"
LLM_PROVIDERS = ("groq", "anthropic", "watsonx", "xai")
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
    watsonx_key: str = field(default="", repr=False)  # never printed, even by a stray repr(settings)
    watsonx_project_id: str = ""
    watsonx_url: str = WATSONX_DEFAULT_URL
    watsonx_model_id: str = WATSONX_DEFAULT_MODEL_ID
    xai_key: str = field(default="", repr=False)  # never printed, even by a stray repr(settings)
    xai_base_url: str = XAI_DEFAULT_URL
    xai_model: str = XAI_DEFAULT_MODEL


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
        "watsonx_key": os.environ.get("WATSONX_API_KEY") or _read_kv(SECRETS_DIR / "watsonx_key.txt").get("WATSONX_API_KEY", ""),
        "watsonx_project_id": os.environ.get("WATSONX_PROJECT_ID", "").strip(),
        "watsonx_url": (os.environ.get("WATSONX_URL") or WATSONX_DEFAULT_URL).strip(),
        "watsonx_model_id": (os.environ.get("WATSONX_MODEL_ID") or WATSONX_DEFAULT_MODEL_ID).strip(),
        "xai_key": os.environ.get("XAI_API_KEY") or _read_kv(SECRETS_DIR / "xai_key.txt").get("XAI_API_KEY", ""),
        "xai_base_url": (os.environ.get("XAI_BASE_URL") or XAI_DEFAULT_URL).strip(),
        "xai_model": (os.environ.get("XAI_MODEL") or XAI_DEFAULT_MODEL).strip(),
    }


def load_settings() -> Settings:
    llm = llm_settings_from_env()
    shop_file = _read_kv(SECRETS_DIR / "shopify_dev_token.txt")
    shop = os.environ.get("SHOPIFY_SHOP") or shop_file.get("SHOP", "")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN") or shop_file.get("ACCESS_TOKEN", "")
    model_keys = {"anthropic": (("ANTHROPIC_API_KEY", llm["anthropic_key"]),),
                  "watsonx": (("WATSONX_API_KEY", llm["watsonx_key"]), ("WATSONX_PROJECT_ID", llm["watsonx_project_id"])),
                  "xai": (("XAI_API_KEY", llm["xai_key"]),)
                  }.get(llm["llm_provider"], (("GROQ_API_KEY", llm["groq_key"]),))
    missing = [n for n, v in (*model_keys, ("SHOPIFY_SHOP", shop), ("SHOPIFY_ACCESS_TOKEN", token)) if not v]
    if missing:
        raise RuntimeError("missing configuration: " + ", ".join(missing))
    return Settings(shop=shop, shopify_token=token, **llm)
