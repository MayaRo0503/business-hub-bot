"""Publishing to the business's Telegram channel: pick the channel, check rights, post text / photo / video."""
import json

import pytest

from bot.publish import post_link, split_text
from bot.vault import Vault
from conftest import tg_text

ADMIN = {"Authorization": "Bearer admin-secret"}
TG = {"X-Telegram-Bot-Api-Secret-Token": "tg-secret"}
CHANNEL = -1001234567890


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._data


@pytest.fixture
def tg(monkeypatch):
    """Fake Telegram Bot API: records calls; the bot is an admin who may post, unless told otherwise."""
    state = {"calls": [], "can_post": True, "next_id": 40, "fail": None}

    def fake_post(url, json=None, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        state["calls"].append({"method": method, "json": json, "data": data, "files": files})
        if state["fail"] and method == state["fail"][0]:
            return FakeResponse({"ok": False, "description": state["fail"][2]}, state["fail"][1])
        if method == "getMe":
            return FakeResponse({"ok": True, "result": {"id": 777, "username": "ShiranBot"}})
        if method == "getChat":
            chat = (json or {}).get("chat_id")
            if chat in (CHANNEL, str(CHANNEL), "@shiran_news"):
                return FakeResponse({"ok": True, "result": {"id": CHANNEL, "type": "channel", "title": "מספרת שירן – עדכונים",
                                                            "username": "shiran_news"}})
            if chat == "@somegroup":
                return FakeResponse({"ok": True, "result": {"id": -55, "type": "supergroup", "title": "קבוצה"}})
            return FakeResponse({"ok": False, "description": "Bad Request: chat not found"}, 400)
        if method == "getChatMember":
            return FakeResponse({"ok": True, "result": {"status": "administrator", "can_post_messages": state["can_post"]}})
        if method in ("sendMessage", "sendPhoto", "sendVideo"):
            state["next_id"] += 1
            return FakeResponse({"ok": True, "result": {"message_id": state["next_id"]}})
        return FakeResponse({"ok": True, "result": True})

    import bot.connect as connect
    monkeypatch.setattr(connect.requests, "post", fake_post)
    return state


OWNER = 999  # OWNER_TELEGRAM_CHAT_ID in conftest


def added_to_channel(can_post=True, status="administrator", update_id=50, by=OWNER, chat_id=CHANNEL, title="מספרת שירן – עדכונים"):
    return {"update_id": update_id, "my_chat_member": {
        "chat": {"id": chat_id, "type": "channel", "title": title, "username": "shiran_news"},
        "from": {"id": by, "first_name": "שירן", "username": "shiran"},
        "new_chat_member": {"status": status, "can_post_messages": can_post, "user": {"id": 777, "is_bot": True}}}}


def test_split_text_keeps_paragraphs_and_limits():
    text = ("א" * 3000) + "\n\n" + ("ב" * 3000)
    parts = split_text(text)
    assert parts == ["א" * 3000, "ב" * 3000]
    assert all(len(p) <= 4096 for p in split_text("מילה " * 3000))


def test_post_links():
    assert post_link(CHANNEL, "shiran_news", 41) == "https://t.me/shiran_news/41"
    assert post_link(CHANNEL, "", 41) == "https://t.me/c/1234567890/41"


def test_pick_channel_after_adding_the_bot(make_app, tg, monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://shiran-bot.onrender.com")
    _, client, store = make_app()

    r = client.post("/admin/channels/telegram/channel/prepare", headers=ADMIN).json
    assert r["addLink"] == "https://t.me/ShiranBot?startchannel&admin=post_messages+edit_messages"
    hook = next(c for c in tg["calls"] if c["method"] == "setWebhook")["json"]
    assert "my_chat_member" in hook["allowed_updates"] and hook["secret_token"] == "tg-secret"

    # Telegram tells the bot it was added as an admin: it becomes a candidate, nothing is connected yet
    assert client.post("/webhook/telegram", json=added_to_channel(), headers=TG).status_code == 200
    ch = client.get("/admin/channels", headers=ADMIN).json["telegram"]["channel"]
    assert not ch["connected"] and ch["candidates"][0]["title"] == "מספרת שירן – עדכונים"
    # a forged update without the webhook secret is ignored
    assert client.post("/webhook/telegram", json=added_to_channel(update_id=51)).status_code == 403

    # the owner picks it in the dashboard; the server checks the bot may post there
    r = client.post("/admin/channels/telegram/channel", json={"chatId": str(CHANNEL)}, headers=ADMIN).json
    assert r["ok"] and r["account"] == "מספרת שירן – עדכונים"
    ch = client.get("/admin/channels", headers=ADMIN).json["telegram"]["channel"]
    assert ch["connected"] and ch["username"] == "shiran_news" and ch["candidates"] == []

    # removed from the channel later → shown as disconnected instead of failing at publish time
    client.post("/webhook/telegram", json=added_to_channel(status="left", update_id=52), headers=TG)
    assert not client.get("/admin/channels", headers=ADMIN).json["telegram"]["channel"]["connected"]


def test_strangers_cannot_put_their_channel_in_the_owners_list(make_app, tg, monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://shiran-bot.onrender.com")
    _, client, store = make_app()
    cands = lambda: client.get("/admin/channels", headers=ADMIN).json["telegram"]["channel"]["candidates"]

    # before the owner pressed "add the bot to a channel": ignored, even from the owner
    client.post("/webhook/telegram", json=added_to_channel(), headers=TG)
    assert cands() == []

    client.post("/admin/channels/telegram/channel/prepare", headers=ADMIN)
    # a stranger adds the (public) bot to their own channel during the window: ignored, the owner is paired
    for i in range(6):
        client.post("/webhook/telegram", json=added_to_channel(update_id=60 + i, by=123, chat_id=-1009000000000 - i,
                                                               title=f"ערוץ זר {i}"), headers=TG)
    assert cands() == []
    # the owner's own add counts, and shows who added it
    client.post("/webhook/telegram", json=added_to_channel(update_id=70), headers=TG)
    assert [c["title"] for c in cands()] == ["מספרת שירן – עדכונים"] and cands()[0]["by"] == "שירן @shiran"

    # the window closes after 15 minutes
    import bot.publish as publish
    real = publish.time.time
    monkeypatch.setattr(publish.time, "time", lambda: real() + 16 * 60)
    client.post("/webhook/telegram", json=added_to_channel(update_id=71, chat_id=-1007777777777, title="מאוחר"), headers=TG)
    assert "מאוחר" not in [c["title"] for c in cands()]


def test_no_paired_owner_still_needs_the_window_and_list_never_overflows(make_app, tg, monkeypatch):
    monkeypatch.delenv("OWNER_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://shiran-bot.onrender.com")
    _, client, _ = make_app()
    client.post("/admin/channels/telegram/channel/prepare", headers=ADMIN)
    for i in range(7):
        client.post("/webhook/telegram", json=added_to_channel(update_id=80 + i, by=500 + i, chat_id=-1008000000000 - i,
                                                               title=f"ערוץ {i}"), headers=TG)
    titles = [c["title"] for c in client.get("/admin/channels", headers=ADMIN).json["telegram"]["channel"]["candidates"]]
    # the first five stay; later adds cannot push them out
    assert len(titles) == 5 and "ערוץ 0" in titles and "ערוץ 6" not in titles


def test_connect_by_username_and_rights(make_app, tg):
    _, client, _ = make_app()
    bad = lambda body: client.post("/admin/channels/telegram/channel", json=body, headers=ADMIN).json["error"]
    assert bad({"username": "x"}) == "CHANNEL_FORMAT"
    assert bad({"chatId": "12345"}) == "CHANNEL_FORMAT"
    assert bad({"username": "@nosuchchannel"}) == "CHANNEL_NOT_FOUND"
    assert bad({"username": "@somegroup"}) == "NOT_A_CHANNEL"
    tg["can_post"] = False
    assert bad({"username": "@shiran_news"}) == "CHANNEL_NO_RIGHTS"
    tg["can_post"] = True
    assert client.post("/admin/channels/telegram/channel", json={"username": "https://t.me/shiran_news"}, headers=ADMIN).json["ok"]


def connected(make_app, tg):
    app, client, store = make_app()
    Vault(store, "admin-secret").update(telegram_channel_id=str(CHANNEL), telegram_channel_title="עדכונים",
                                        telegram_channel_username="shiran_news")
    return client


def test_publish_text_photo_and_video(make_app, tg):
    client = connected(make_app, tg)
    assert client.post("/admin/publish/telegram", data={"text": "מבצע סתיו 🍂"}).status_code == 401  # admin only

    r = client.post("/admin/publish/telegram", data={"text": "מבצע סתיו 🍂\r\n\r\n#מספרה"}, headers=ADMIN).json
    assert r["ok"] and r["url"] == "https://t.me/shiran_news/41"
    msg = tg["calls"][-1]
    assert msg["method"] == "sendMessage" and msg["data"] == {"chat_id": str(CHANNEL), "text": "מבצע סתיו 🍂\n\n#מספרה"}

    # short text rides along as the caption of the photo
    r = client.post("/admin/publish/telegram", headers=ADMIN, content_type="multipart/form-data",
                    data={"text": "התספורת של השבוע", "file": (__import__("io").BytesIO(b"\xff\xd8jpeg"), "week.jpg")}).json
    photo = tg["calls"][-1]
    assert r["ok"] and photo["method"] == "sendPhoto" and photo["data"]["caption"] == "התספורת של השבוע"
    assert photo["files"]["photo"][1] == b"\xff\xd8jpeg"

    # a video bigger than 1 MB (the server's usual limit) goes through on this route, long text follows separately
    big = b"\x00" * (3 * 1024 * 1024)
    long_text = "טיפ " * 400
    calls_before = len(tg["calls"])
    r = client.post("/admin/publish/telegram", headers=ADMIN, content_type="multipart/form-data",
                    data={"text": long_text, "file": (__import__("io").BytesIO(big), "tip.mp4")}).json
    new = [c["method"] for c in tg["calls"][calls_before:]]
    assert r["ok"] and new == ["sendVideo", "sendMessage"]
    assert "caption" not in tg["calls"][calls_before]["data"]


def test_publish_errors_are_plain(make_app, tg):
    _, client, _ = make_app()
    assert client.post("/admin/publish/telegram", data={"text": "x"}, headers=ADMIN).json["error"] == "CHANNEL_NOT_CONNECTED"
    client = connected(make_app, tg)
    io = __import__("io")
    r = client.post("/admin/publish/telegram", headers=ADMIN, content_type="multipart/form-data",
                    data={"text": "x", "file": (io.BytesIO(b"x"), "doc.pdf")})
    assert r.json["error"] == "UNSUPPORTED_FILE"
    r = client.post("/admin/publish/telegram", headers=ADMIN, content_type="multipart/form-data",
                    data={"file": (io.BytesIO(b"\x00" * (10 * 1024 * 1024 + 1)), "huge.png")})
    assert r.json["error"] == "FILE_TOO_BIG"
    tg["fail"] = ("sendMessage", 403, "Forbidden: bot is not a member of the channel chat")
    r = client.post("/admin/publish/telegram", data={"text": "x"}, headers=ADMIN)
    assert r.status_code == 403 and r.json["error"] == "CHANNEL_NO_RIGHTS"
    # other admin routes keep the 1 MB limit
    assert client.post("/admin/test", data=b"x" * (2 * 1024 * 1024), headers={**ADMIN, "Content-Type": "application/json"}).status_code == 413


def test_disconnecting_the_bot_forgets_the_channel(make_app, tg):
    client = connected(make_app, tg)
    client.delete("/admin/channels/telegram", headers=ADMIN)
    assert not client.get("/admin/channels", headers=ADMIN).json["telegram"]["channel"]["connected"]


def test_regular_messages_still_answered(make_app, tg, sent):
    _, client, _ = make_app()
    client.post("/webhook/telegram", json=tg_text("יש חניה?"), headers=TG)
    assert sent and "חניון" in json.dumps(sent[-1]["body"], ensure_ascii=False)
