"""Connecting channels from the dashboard: Telegram, Meta app, Facebook page + Instagram, WhatsApp."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from bot import connect
from bot.vault import Vault
from conftest import tg_text

ADMIN = {"Authorization": "Bearer admin-secret"}
TG_TOKEN = "123456789:" + "A" * 35
APP_ID, APP_SECRET = "1234567890", "0123456789abcdef0123456789abcdef"


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._data


@pytest.fixture
def api(monkeypatch):
    """Fake Telegram + Graph. Records every call; routes by URL."""
    calls = []
    state = {"pages": [{"id": "111", "name": "מספרת שירן", "access_token": "PAGE-TOKEN-LONG",
                        "instagram_business_account": {"id": "999", "username": "shiran.salon"}}],
             "tg_ok": True}

    def fake_post(url, json=None, timeout=None):
        calls.append(("POST", url, json))
        if "/getMe" in url:
            return FakeResponse({"ok": True, "result": {"username": "ShiranBot"}}) if state["tg_ok"] \
                else FakeResponse({"ok": False, "description": "Unauthorized"}, 401)
        return FakeResponse({"ok": True, "result": True})

    def fake_request(method, url, params=None, json=None, timeout=None):
        calls.append((method, url, dict(params or {}), json))
        path = urlparse(url).path.split("/", 2)[-1]
        if path == APP_ID:
            return FakeResponse({"name": "Shiran Bot App", "id": APP_ID})
        if path == "oauth/access_token":
            if (params or {}).get("grant_type") == "fb_exchange_token":
                return FakeResponse({"access_token": "USER-LONG", "expires_in": 5183944})
            return FakeResponse({"access_token": "USER-SHORT"})
        if path == "me/accounts":
            return FakeResponse({"data": state["pages"]})
        if path.endswith("/subscribed_apps"):
            return FakeResponse({"success": True})
        if path == "105954558954427":  # whatsapp phone number id
            return FakeResponse({"display_phone_number": "+972 52-111-2233", "verified_name": "מספרת שירן"})
        if path == "105954558954427/register":
            return FakeResponse({"success": True})
        return FakeResponse({"error": {"code": 100, "message": f"unexpected {path}"}}, 400)

    monkeypatch.setattr(connect.requests, "post", fake_post)
    monkeypatch.setattr(connect.requests, "request", fake_request)
    return {"calls": calls, "state": state}


@pytest.fixture
def https(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://shiran-bot.onrender.com")


# ---------------- vault ----------------

def test_vault_encrypts_and_rejects_wrong_key(make_app):
    _, _, store = make_app()
    v = Vault(store, "key-one")
    v.update(telegram_token=TG_TOKEN)
    assert TG_TOKEN not in json.dumps(store.get_kv(Vault.KEY))  # never plaintext in the DB
    assert Vault(store, "key-one").load()["telegram_token"] == TG_TOKEN
    assert Vault(store, "another-key").load() == {}
    with pytest.raises(ValueError):
        v.update(random_field="x")


# ---------------- telegram ----------------

def test_telegram_connect_end_to_end(make_app, api, https, sent, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("OWNER_TELEGRAM_CHAT_ID", raising=False)
    _, client, store = make_app()
    assert client.post("/admin/channels/telegram", json={"token": "nope"}, headers=ADMIN).json["error"] == "TELEGRAM_TOKEN_FORMAT"

    r = client.post("/admin/channels/telegram", json={"token": TG_TOKEN}, headers=ADMIN).json
    assert r["ok"] and r["account"] == "@ShiranBot" and r["ownerLink"].startswith("https://t.me/ShiranBot?start=")
    set_hook = next(c for c in api["calls"] if c[1].endswith("/setWebhook"))
    secret = set_hook[2]["secret_token"]
    assert set_hook[2]["url"] == "https://shiran-bot.onrender.com/webhook/telegram"
    assert set_hook[2]["allowed_updates"] == ["message", "callback_query"]
    assert 1 <= len(secret) <= 256 and all(ch.isalnum() or ch in "_-" for ch in secret)

    status = client.get("/admin/channels", headers=ADMIN).json["telegram"]
    assert status["connected"] and status["account"] == "@ShiranBot" and status["source"] == "dashboard"
    assert TG_TOKEN not in json.dumps(client.get("/admin/channels", headers=ADMIN).json)  # never returned

    # webhooks now need the new secret
    assert client.post("/webhook/telegram", json=tg_text("היי"), headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"}).status_code == 403
    assert client.post("/webhook/telegram", json=tg_text("היי"), headers={"X-Telegram-Bot-Api-Secret-Token": secret}).status_code == 200

    # the owner opens the link → '/start <code>' → their chat gets handoff alerts
    code = r["ownerLink"].split("start=")[1]
    sent.clear()
    client.post("/webhook/telegram", json=tg_text(f"/start {code}", update_id=7, chat=4242),
                headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert "התראה" in sent[0]["body"]["text"]
    assert client.get("/admin/channels", headers=ADMIN).json["telegram"]["ownerAlerts"]
    # the code is single-use
    sent.clear()
    client.post("/webhook/telegram", json=tg_text(f"/start {code}", update_id=8, chat=9999),
                headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert "התראה" not in str(sent)


def test_telegram_wrong_token_and_https_required(make_app, api, monkeypatch):
    _, client, _ = make_app()
    api["state"]["tg_ok"] = False
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
    assert client.post("/admin/channels/telegram", json={"token": TG_TOKEN}, headers=ADMIN).json["error"] == "TELEGRAM_TOKEN_INVALID"
    api["state"]["tg_ok"] = True
    monkeypatch.delenv("RENDER_EXTERNAL_URL")
    _, client, _ = make_app()
    assert client.post("/admin/channels/telegram", json={"token": TG_TOKEN}, headers=ADMIN).json["error"] == "NEEDS_HTTPS"


def test_admin_required(make_app):
    _, client, _ = make_app()
    for method, path in [("get", "/admin/channels"), ("post", "/admin/channels/telegram"),
                         ("post", "/admin/channels/meta-app"), ("post", "/admin/channels/whatsapp")]:
        assert getattr(client, method)(path, json={}).status_code == 401


# ---------------- meta: app, facebook page + instagram ----------------

def connect_meta_app(client):
    r = client.post("/admin/channels/meta-app", json={"appId": APP_ID, "appSecret": APP_SECRET}, headers=ADMIN).json
    assert r["ok"], r
    return r


def test_meta_app_returns_what_to_paste_in_meta(make_app, api, https):
    _, client, _ = make_app()
    assert client.post("/admin/channels/meta-app", json={"appId": "x", "appSecret": "y"}, headers=ADMIN).json["error"] == "META_APP_FORMAT"
    r = connect_meta_app(client)
    assert r["webhookUrl"] == "https://shiran-bot.onrender.com/webhook/meta"
    assert r["oauthRedirect"] == "https://shiran-bot.onrender.com/connect/meta/callback"
    assert r["verifyToken"] == "verify-me"
    assert client.get("/admin/channels", headers=ADMIN).json["metaApp"]["secret"] != APP_SECRET


def test_facebook_login_flow_single_page(make_app, api, https, monkeypatch):
    monkeypatch.delenv("PAGE_ACCESS_TOKEN", raising=False)
    _, client, _ = make_app()
    connect_meta_app(client)
    link = client.post("/admin/channels/meta/connect-link", headers=ADMIN).json["url"]
    nonce = parse_qs(urlparse(link).query)["t"][0]

    start = client.get(f"/connect/meta/start?t={nonce}")
    dialog = urlparse(start.headers["Location"])
    q = parse_qs(dialog.query)
    assert dialog.netloc == "www.facebook.com" and q["client_id"] == [APP_ID] and q["state"] == [nonce]
    assert q["redirect_uri"] == ["https://shiran-bot.onrender.com/connect/meta/callback"]
    assert "pages_messaging" in q["scope"][0] and "instagram_manage_messages" in q["scope"][0]

    done = client.get(f"/connect/meta/callback?code=abc&state={nonce}")
    assert done.status_code == 200 and "חובר" in done.get_data(as_text=True)
    exchange = [c for c in api["calls"] if c[1].endswith("oauth/access_token")]
    assert exchange[0][2]["client_secret"] == APP_SECRET            # server-to-server, with the secret
    assert exchange[1][2]["grant_type"] == "fb_exchange_token"       # long-lived user token → non-expiring page token
    sub = next(c for c in api["calls"] if c[1].endswith("111/subscribed_apps"))
    assert sub[0] == "POST" and sub[2]["subscribed_fields"] == "messages,messaging_postbacks"
    assert sub[2]["access_token"] == "PAGE-TOKEN-LONG"

    st = client.get("/admin/channels", headers=ADMIN).json
    assert st["messenger"]["connected"] and st["messenger"]["account"] == "מספרת שירן"
    assert st["instagram"]["connected"] and st["instagram"]["account"] == "@shiran.salon"
    # the one-time value cannot be replayed
    assert client.get(f"/connect/meta/callback?code=abc&state={nonce}").status_code == 400


def test_facebook_login_page_picker(make_app, api, https):
    _, client, _ = make_app()
    connect_meta_app(client)
    api["state"]["pages"].append({"id": "222", "name": "עמוד שני", "access_token": "PAGE2"})
    nonce = parse_qs(urlparse(client.post("/admin/channels/meta/connect-link", headers=ADMIN).json["url"]).query)["t"][0]
    client.get(f"/connect/meta/start?t={nonce}")
    picker = client.get(f"/connect/meta/callback?code=abc&state={nonce}").get_data(as_text=True)
    assert "איזה עמוד" in picker and "PAGE2" not in picker  # page tokens never reach the browser
    done = client.post("/connect/meta/page", data={"state": nonce, "page": "222"})
    assert "עמוד שני" in done.get_data(as_text=True)
    assert client.get("/admin/channels", headers=ADMIN).json["messenger"]["account"] == "עמוד שני"


def test_connect_link_unknown_or_missing_app(make_app, api, https):
    _, client, _ = make_app()
    assert client.get("/connect/meta/start?t=forged").status_code == 400
    assert client.get("/connect/meta/callback?code=x&state=forged").status_code == 400


# ---------------- whatsapp ----------------

def test_whatsapp_connect(make_app, api, https):
    _, client, _ = make_app()
    connect_meta_app(client)
    bad = client.post("/admin/channels/whatsapp", json={"phoneNumberId": "x"}, headers=ADMIN).json
    assert bad["error"] == "WHATSAPP_FORMAT"
    r = client.post("/admin/channels/whatsapp", json={"phoneNumberId": "105954558954427", "wabaId": "102290129340398",
                                                      "token": "E" * 60, "pin": "123456"}, headers=ADMIN).json
    assert r["ok"] and "+972 52-111-2233" in r["account"]
    assert any(c[1].endswith("102290129340398/subscribed_apps") and c[0] == "POST" for c in api["calls"])
    reg = next(c for c in api["calls"] if c[1].endswith("105954558954427/register"))
    assert reg[3] == {"messaging_product": "whatsapp", "pin": "123456"}
    st = client.get("/admin/channels", headers=ADMIN).json["whatsapp"]
    assert st["connected"] and st["source"] == "dashboard"
