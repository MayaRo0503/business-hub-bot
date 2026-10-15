import hashlib
import hmac
import json
import os
import sys

import pytest

os.environ["BOT_LAZY"] = "1"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from bot import channels  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.store import Store  # noqa: E402

KNOWLEDGE = {
    "businessName": "מספרת שירן",
    "faq": [
        {"id": "hours", "q": "מה שעות הפתיחה?", "a": "א׳–ה׳ 9:00–19:00, ו׳ 8:00–13:00",
         "title": "שעות פתיחה", "keywords": ["שעות", "פתוחים", "סגורים", "פתוח"]},
        {"id": "parking", "q": "יש חניה?", "a": "כן, חניון ציבורי צמוד – שעה ראשונה חינם",
         "title": "חניה", "keywords": ["חניה", "חנייה", "לחנות", "חניון"]},
        {"id": "price", "q": "כמה עולה תספורת?", "a": "תספורת נשים 180 ₪, כולל חפיפה ופן",
         "title": "מחירים", "keywords": ["מחיר", "מחירים", "עולה", "עלות"]},
    ],
    "hours": {"sun": ["00:00", "24:00"], "mon": ["00:00", "24:00"], "tue": ["00:00", "24:00"],
              "wed": ["00:00", "24:00"], "thu": ["00:00", "24:00"], "fri": ["00:00", "24:00"], "sat": ["00:00", "24:00"]},
}


class SyncPool:
    """Runs submitted work immediately, so tests see the reply right after the request."""
    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def env(monkeypatch):
    values = {
        "ADMIN_TOKEN": "admin-secret", "META_APP_SECRET": "meta-secret", "META_VERIFY_TOKEN": "verify-me",
        "WHATSAPP_PHONE_ID": "123", "FACEBOOK_ACCESS_TOKEN": "wa-token", "PAGE_ACCESS_TOKEN": "page-token",
        "TELEGRAM_BOT_TOKEN": "tg-token", "TELEGRAM_WEBHOOK_SECRET": "tg-secret", "OWNER_TELEGRAM_CHAT_ID": "999",
        "BOT_MODE": "basic", "DATABASE_URL": "sqlite:///:memory:",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return values


@pytest.fixture
def sent(monkeypatch):
    """Captures every outgoing API call instead of hitting Meta/Telegram."""
    calls = []

    def fake_post(url, json_body, *, params=None, headers=None, what=""):
        calls.append({"url": url, "body": json_body, "what": what})
        return True

    monkeypatch.setattr(channels, "_post", fake_post)
    return calls


@pytest.fixture
def make_app(env, monkeypatch):
    def _make(smart=None, **overrides):
        for k, v in overrides.items():
            monkeypatch.setenv(k, v)
        cfg = Config()
        store = Store(cfg.database_url, cfg.pii_salt)
        app = create_app(cfg, store, smart=smart, executor=SyncPool())
        client = app.test_client()
        r = client.put("/admin/knowledge", json=KNOWLEDGE, headers={"Authorization": "Bearer admin-secret"})
        assert r.status_code == 200, r.json
        return app, client, store
    return _make


def meta_post(client, payload: dict, secret="meta-secret"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook/meta", data=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def wa_text(text, msg_id="wamid.1", sender="972501234567"):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": sender, "profile": {"name": "נועה"}}],
        "messages": [{"from": sender, "id": msg_id, "type": "text", "text": {"body": text}}]}}]}]}


def wa_button(button_id, msg_id="wamid.b1", sender="972501234567"):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messages": [{"from": sender, "id": msg_id, "type": "interactive",
                      "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "x"}}}]}}]}]}


def page_text(obj, text, mid="m_1", sender="PSID1", echo=False):
    return {"object": obj, "entry": [{"messaging": [{"sender": {"id": sender}, "recipient": {"id": "PAGE"},
                                                     "message": {"mid": mid, "text": text, "is_echo": echo}}]}]}


def tg_text(text, update_id=1, chat=555):
    return {"update_id": update_id, "message": {"message_id": 1, "chat": {"id": chat},
                                                "from": {"first_name": "דנה", "username": "dana"}, "text": text}}
