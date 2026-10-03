"""Where the keys come from. Environment first, then the saved secret files, never source."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SECRETS_DIR = Path(r"C:\Projects\Professional\Operations\.secrets")
MODEL = os.environ.get("SUPPORT_AGENT_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
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


def load_settings() -> Settings:
    groq = os.environ.get("GROQ_API_KEY") or _read_kv(SECRETS_DIR / "groq_test_key.txt").get("GROQ_API_KEY", "")
    shop_file = _read_kv(SECRETS_DIR / "shopify_dev_token.txt")
    shop = os.environ.get("SHOPIFY_SHOP") or shop_file.get("SHOP", "")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN") or shop_file.get("ACCESS_TOKEN", "")
    missing = [n for n, v in (("GROQ_API_KEY", groq), ("SHOPIFY_SHOP", shop), ("SHOPIFY_ACCESS_TOKEN", token)) if not v]
    if missing:
        raise RuntimeError("missing configuration: " + ", ".join(missing))
    return Settings(groq_key=groq, shop=shop, shopify_token=token)
