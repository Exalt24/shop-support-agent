"""Key material for the console. Environment first, then a gitignored file under Operations/.secrets, created
on first use. Nothing here is ever written to the repo or the database.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

SECRETS_DIR = Path(r"C:\Projects\Professional\Operations\.secrets")


def _value(env: str, filename: str, make) -> str:
    if os.environ.get(env):
        return os.environ[env]
    path = SECRETS_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    val = make()
    path.write_text(val + "\n", encoding="utf-8")
    return val


def fernet() -> Fernet:
    """Encrypts each store's Shopify token at rest."""
    return Fernet(_value("CONSOLE_FERNET_KEY", "console_fernet_key.txt", lambda: Fernet.generate_key().decode()))


def session_secret() -> str:
    return _value("CONSOLE_SESSION_SECRET", "console_session_secret.txt", lambda: secrets.token_urlsafe(48))


def encrypt_token(token: str) -> str:
    return fernet().encrypt(token.encode()).decode()


def decrypt_token(blob: str) -> str:
    return fernet().decrypt(blob.encode()).decode()
