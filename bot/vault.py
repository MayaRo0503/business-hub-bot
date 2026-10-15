"""Encrypted storage for channel credentials connected from the dashboard.

Tokens are encrypted with Fernet (AES-128-CBC + HMAC) before they reach the database, using
SECRET_KEY from the environment (Render generates it). Values never leave the server: the admin
API only returns masked previews.
"""
import base64
import hashlib
import json
import logging

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("bot.vault")

# Field names that may be stored. Anything else is rejected, so a bug cannot stash arbitrary data.
FIELDS = {
    # telegram
    "telegram_token", "telegram_secret", "telegram_username", "owner_telegram_chat_id", "owner_pairing_code",
    # telegram channel the business publishes to (content library → "פרסום")
    "telegram_channel_id", "telegram_channel_title", "telegram_channel_username", "telegram_channel_candidates",
    # meta app (option A: the business owns the app)
    "meta_app_id", "meta_app_secret",
    # facebook page + instagram (through the page)
    "page_id", "page_name", "page_token", "ig_id", "ig_username",
    # whatsapp cloud api
    "whatsapp_phone_id", "whatsapp_token", "waba_id", "whatsapp_display", "whatsapp_name",
    "whatsapp_pin", "whatsapp_coexistence",
    # option B: connected through Maya's router (Tech Provider app)
    "router_url", "router_relay_key", "connected_via",
}
SECRET_FIELDS = {"telegram_token", "telegram_secret", "meta_app_secret", "page_token", "whatsapp_token", "owner_pairing_code",
                 "whatsapp_pin", "router_relay_key"}


def _fernet(secret_key: str) -> Fernet:
    # any string works as SECRET_KEY: derive a proper 32-byte Fernet key from it
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest()))


class Vault:
    KEY = "channel_credentials"

    def __init__(self, store, secret_key: str):
        if not secret_key:
            raise ValueError("SECRET_KEY (or ADMIN_TOKEN) is required to store credentials")
        self.store = store
        self.f = _fernet(secret_key)

    def load(self) -> dict:
        blob = self.store.get_kv(self.KEY)
        if not blob:
            return {}
        try:
            return json.loads(self.f.decrypt(blob["data"].encode()).decode())
        except (InvalidToken, KeyError, ValueError):
            log.error("stored credentials cannot be decrypted (SECRET_KEY changed?) – reconnect the channels")
            return {}

    def save(self, data: dict) -> int:
        unknown = set(data) - FIELDS
        if unknown:
            raise ValueError(f"unknown credential fields: {sorted(unknown)}")
        clean = {k: str(v) for k, v in data.items() if v not in (None, "")}
        version = int((self.store.get_kv(self.KEY) or {}).get("version", 0)) + 1
        self.store.set_kv(self.KEY, {"version": version, "data": self.f.encrypt(json.dumps(clean).encode()).decode()})
        return version

    def update(self, **fields) -> int:
        data = self.load()
        for k, v in fields.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        return self.save(data)

    def version(self) -> int:
        return int((self.store.get_kv(self.KEY) or {}).get("version", 0))


def mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-2:] if len(value) > 10 else "…"
