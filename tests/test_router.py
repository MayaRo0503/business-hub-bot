"""Option B: what arrives from Maya's router (credentials push, relayed webhooks) must carry its signature."""
import hashlib
import hmac
import json
import time

from bot.connect import router_signature_ok
from bot.vault import Vault
from conftest import wa_text

ADMIN = {"Authorization": "Bearer admin-secret"}
RELAY = "r" * 43


def signed(body: dict, key=RELAY, ts=None):
    raw = json.dumps(body).encode()
    ts = str(ts or int(time.time()))
    sig = hmac.new(key.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"X-Router-Timestamp": ts, "X-Router-Signature": sig, "Content-Type": "application/json"}


def test_shared_vector_with_router():
    # same vector is asserted in the router's tests (buissnes WEB/tests/router.test.mjs)
    expected = "91526c0ae8187bf3b7b9d3b90e34f6bfc19652b54fef372f9df55154400ce267"
    assert router_signature_ok("k" * 43, {"X-Router-Timestamp": "1700000000", "X-Router-Signature": expected},
                               b'{"a":1}', max_age=10 ** 10)



def start_session(client, monkeypatch, router="https://maya-hub.vercel.app"):
    import bot.connect as connect
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://shiran-bot.onrender.com")
    seen = {}

    class R:
        def json(self):
            return {"ok": True, "connectUrl": f"{router}/connect/?s=abc", "expiresIn": 1800}

    def fake_post(url, json=None, timeout=None, **kw):
        seen.update(url=url, body=json)
        return R()

    monkeypatch.setattr(connect.requests, "post", fake_post)
    return client.post("/admin/channels/router/start", json={"routerUrl": router}, headers=ADMIN), seen


def test_router_credentials_and_relayed_webhook(make_app, sent, monkeypatch):
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    monkeypatch.delenv("FACEBOOK_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_ID", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://shiran-bot.onrender.com")
    _, client, store = make_app()
    Vault(store, "admin-secret").update(router_url="https://maya-hub.vercel.app", router_relay_key=RELAY)

    creds = {"credentials": {"whatsapp_phone_id": "105954558954427", "waba_id": "102290129340398",
                             "whatsapp_token": "BUSINESS-TOKEN", "whatsapp_display": "+972 52-111-2233",
                             "whatsapp_coexistence": True, "evil_field": "x"}, "connected": ["whatsapp"]}
    raw, headers = signed(creds, key="wrong-key" + "x" * 34)
    assert client.post("/router/credentials", data=raw, headers=headers).status_code == 403
    raw, headers = signed(creds, ts=int(time.time()) - 3600)  # replay of an old push
    assert client.post("/router/credentials", data=raw, headers=headers).status_code == 403
    raw, headers = signed(creds)
    assert client.post("/router/credentials", data=raw, headers=headers).status_code == 409  # no connect session open
    start_session(client, monkeypatch)
    raw, headers = signed(creds)
    assert client.post("/router/credentials", data=raw, headers=headers).json["ok"]
    assert client.post("/router/credentials", data=raw, headers=headers).status_code == 409  # one push per session

    status = client.get("/admin/channels", headers=ADMIN).json
    assert status["whatsapp"]["connected"] and status["whatsapp"]["coexistence"] and status["via"] == "router"
    assert "evil_field" not in json.dumps(Vault(store, "admin-secret").load())

    # a relayed WhatsApp message is answered with the business token
    raw, headers = signed(wa_text("יש חניה?", msg_id="wamid.r1"))
    assert client.post("/webhook/router", data=raw, headers={**headers, "X-Router-Signature": "0" * 64}).status_code == 403
    assert client.post("/webhook/router", data=raw, headers=headers).status_code == 200
    assert sent and sent[0]["what"] == "whatsapp" and "חניון" in str(sent[0]["body"])

    # disconnecting the last router channel drops the relay key: old signatures stop working
    assert client.delete("/admin/channels/whatsapp", headers=ADMIN).json["ok"]
    stored = Vault(store, "admin-secret").load()
    assert not stored.get("router_relay_key") and not stored.get("connected_via")
    raw, headers = signed(wa_text("שוב", msg_id="wamid.r2"))
    assert client.post("/webhook/router", data=raw, headers=headers).status_code == 403


def test_router_start_requests_a_session(make_app, monkeypatch):
    import bot.connect as connect
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://shiran-bot.onrender.com")
    seen = {}

    class R:
        def json(self):
            return {"ok": True, "connectUrl": "https://maya-hub.vercel.app/connect/?s=abc", "expiresIn": 1800}

    def fake_post(url, json=None, timeout=None, **kw):
        seen.update(url=url, body=json)
        return R()

    monkeypatch.setattr(connect.requests, "post", fake_post)
    _, client, store = make_app()
    assert client.post("/admin/channels/router/start", json={"routerUrl": "http://insecure"}, headers=ADMIN).status_code == 400
    r = client.post("/admin/channels/router/start", json={"routerUrl": "https://maya-hub.vercel.app"}, headers=ADMIN).json
    assert r["url"].endswith("?s=abc")
    assert seen["url"] == "https://maya-hub.vercel.app/api/sessions"
    assert seen["body"]["serverUrl"] == "https://shiran-bot.onrender.com" and len(seen["body"]["relayKey"]) >= 32
    # the same relay key is kept for later sessions (routes already registered keep working)
    first = seen["body"]["relayKey"]
    client.post("/admin/channels/router/start", json={"routerUrl": "https://maya-hub.vercel.app"}, headers=ADMIN)
    assert seen["body"]["relayKey"] == first
    # ...but a different router gets a fresh key
    client.post("/admin/channels/router/start", json={"routerUrl": "https://other-hub.vercel.app"}, headers=ADMIN)
    assert seen["body"]["relayKey"] != first
    # internal addresses are refused
    for bad in ["https://127.0.0.1", "https://localhost:8080", "https://169.254.169.254", "https://db.internal", "https://x"]:
        assert client.post("/admin/channels/router/start", json={"routerUrl": bad}, headers=ADMIN).status_code == 400
    # when the server pins its router, nothing else is accepted
    monkeypatch.setenv("ROUTER_URL", "https://maya-hub.vercel.app")
    assert client.post("/admin/channels/router/start", json={"routerUrl": "https://other-hub.vercel.app"}, headers=ADMIN).status_code == 400
