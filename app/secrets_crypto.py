"""
secrets_crypto.py
-------------------
Symmetric encryption for admin-entered secrets (Google API keys) stored
in app_settings.jsonb. No encryption mechanism exists anywhere else in
this codebase — every other credential (e.g. platform_credentials.
access_token) is stored plain-text — so this is new, targeted
infrastructure, not a reuse of an existing pattern.

Uses Fernet (symmetric, authenticated) from `cryptography`, keyed by
MARKETINGAI_SETTINGS_ENCRYPTION_KEY. Generate a key with:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_KEY = os.environ.get("MARKETINGAI_SETTINGS_ENCRYPTION_KEY")


def _fernet() -> Fernet:
    if not _KEY:
        raise RuntimeError(
            "MARKETINGAI_SETTINGS_ENCRYPTION_KEY not configured — "
            "generate one with Fernet.generate_key() and set it in .env"
        )
    return Fernet(_KEY.encode())


def encrypt_value(plain: str) -> str:
    """Returns an opaque encrypted token safe to store in jsonb."""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_value(token: str) -> str:
    """Raises ValueError if the token is invalid/undecryptable (wrong key,
    corrupted value) rather than leaking the low-level crypto exception."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Could not decrypt stored value — check MARKETINGAI_SETTINGS_ENCRYPTION_KEY") from exc


def mask_value(plain: Optional[str]) -> Optional[str]:
    """Same masking convention as publishing_routes.py's _mask() — for
    display only, never used to reconstruct the real value."""
    if not plain or len(plain) < 8:
        return None
    return "●" * (len(plain) - 6) + plain[-6:]
