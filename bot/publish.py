"""Publishing from the content library: dashboard → this server → the platform.

Telegram channel (the business's own bot posts as the channel):
  POST   /admin/channels/telegram/channel/prepare  → make sure the bot hears "added to a channel" updates, and return
                                                     the t.me link that opens Telegram's "add to a channel as admin" screen
  POST   /admin/channels/telegram/channel          {chatId} or {username} → check the bot may post there, store it
  DELETE /admin/channels/telegram/channel
  POST   /admin/publish/telegram                   multipart: text, file? (photo ≤ 10 MB, video ≤ 50 MB)

Channels the bot is added to are only remembered as candidates. Nothing is published anywhere until the owner picks the
channel in the dashboard (admin token), so adding this bot to someone else's channel achieves nothing.
Bot API: https://core.telegram.org/bots/api (sendMessage 4096 chars, captions 1024, uploads: photo 10 MB, other 50 MB).
"""
import json
import logging
import re
import time

import requests
from flask import Blueprint, jsonify, request

from .channels import TELEGRAM_UPDATES as ALLOWED_UPDATES
from .connect import ConnectError, _telegram

log = logging.getLogger("bot.publish")

TEXT_LIMIT, CAPTION_LIMIT = 4096, 1024
PHOTO_MAX, VIDEO_MAX = 10 * 1024 * 1024, 50 * 1024 * 1024
UPLOAD_TIMEOUT = 180
CANDIDATE_TTL = 7 * 24 * 3600
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4"}


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split a long post at paragraph, then line, then word boundaries, so no part exceeds Telegram's limit."""
    text = text.strip()
    parts = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def post_link(channel_id: str, username: str, message_id) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    cid = str(channel_id)
    return f"https://t.me/c/{cid[4:] if cid.startswith('-100') else cid.lstrip('-')}/{message_id}"


def _candidates(stored: dict) -> list[dict]:
    try:
        items = json.loads(stored.get("telegram_channel_candidates") or "[]")
    except ValueError:
        return []
    return [c for c in items if time.time() - c.get("at", 0) < CANDIDATE_TTL]


def note_channel(cfg, vault, reload_credentials, update: dict) -> bool:
    """A my_chat_member update: the bot was added to (or removed from) a channel. Returns True if it was one."""
    mcm = update.get("my_chat_member") or {}
    chat = mcm.get("chat") or {}
    if chat.get("type") != "channel":
        return bool(mcm)
    member = mcm.get("new_chat_member") or {}
    can_post = member.get("status") == "administrator" and member.get("can_post_messages", False)
    stored = vault.load()
    cands = [c for c in _candidates(stored) if str(c["id"]) != str(chat.get("id"))]
    changes = {}
    if can_post:
        cands.insert(0, {"id": str(chat["id"]), "title": str(chat.get("title", ""))[:120],
                         "username": str(chat.get("username", "")), "at": int(time.time())})
    elif str(stored.get("telegram_channel_id")) == str(chat.get("id")):
        # the bot lost its rights in the connected channel: show it as disconnected rather than failing later
        changes.update(telegram_channel_id=None, telegram_channel_title=None, telegram_channel_username=None)
    changes["telegram_channel_candidates"] = json.dumps(cands[:5], ensure_ascii=False) if cands else None
    vault.update(**changes)
    reload_credentials()
    return True


def telegram_status(cfg, stored: dict) -> dict:
    connected = bool(cfg.telegram_token and stored.get("telegram_channel_id"))
    return {
        "connected": connected,
        "title": stored.get("telegram_channel_title", "") if connected else "",
        "username": stored.get("telegram_channel_username", "") if connected else "",
        "candidates": [{"id": c["id"], "title": c["title"], "username": c.get("username", "")}
                       for c in _candidates(stored)] if cfg.telegram_token else [],
    }


def _send(token: str, method: str, data: dict, files=None) -> dict:
    """Telegram call with plain-language error codes for the dashboard."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files,
                          timeout=UPLOAD_TIMEOUT if files else 30)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        raise ConnectError("NETWORK", type(e).__name__)
    if body.get("ok"):
        return body["result"]
    desc = str(body.get("description", ""))
    low = desc.lower()
    if r.status_code == 429:
        raise ConnectError("RATE_LIMITED", str((body.get("parameters") or {}).get("retry_after", "")))
    if r.status_code == 413 or "too big" in low or "too large" in low:
        raise ConnectError("FILE_TOO_BIG", desc)
    if r.status_code == 403 or "not enough rights" in low or "administrator" in low or "not a member" in low:
        raise ConnectError("CHANNEL_NO_RIGHTS", desc)
    if "chat not found" in low:
        raise ConnectError("CHANNEL_NOT_FOUND", desc)
    raise ConnectError("TELEGRAM_ERROR", desc)


def create_publish_blueprint(cfg, vault, reload_credentials, require_admin):
    bp = Blueprint("publish", __name__)

    def fail(e: ConnectError, status=400):
        log.warning("publish failed: %s", e)
        return jsonify(ok=False, error=e.code, detail=e.detail[:300]), status

    def bot_username() -> str:
        return cfg.telegram_username or _telegram(cfg.telegram_token, "getMe").get("username", "")

    @bp.post("/admin/channels/telegram/channel/prepare")
    def channel_prepare():
        require_admin()
        if not (cfg.telegram_token and cfg.telegram_secret):
            return fail(ConnectError("TELEGRAM_NOT_CONNECTED"))
        try:
            base = cfg.public_url or request.host_url.rstrip("/")
            if not base.startswith("https://"):
                raise ConnectError("NEEDS_HTTPS", base)
            # bots connected before this update listened to messages only; add "added to a channel" events
            _telegram(cfg.telegram_token, "setWebhook", {"url": f"{base}/webhook/telegram", "secret_token": cfg.telegram_secret,
                                                         "allowed_updates": ALLOWED_UPDATES})
            username = bot_username()
        except ConnectError as e:
            return fail(e)
        # deep link: Telegram asks which channel, with "post messages" already ticked
        return jsonify(ok=True, addLink=f"https://t.me/{username}?startchannel&admin=post_messages+edit_messages",
                       bot=f"@{username}")

    @bp.post("/admin/channels/telegram/channel")
    def channel_connect():
        require_admin()
        if not cfg.telegram_token:
            return fail(ConnectError("TELEGRAM_NOT_CONNECTED"))
        body = request.get_json(silent=True) or {}
        chat_id = str(body.get("chatId", "")).strip()
        username = str(body.get("username", "")).strip().lstrip("@").removeprefix("https://t.me/")
        if chat_id and not re.fullmatch(r"-100\d{5,20}", chat_id):
            return fail(ConnectError("CHANNEL_FORMAT"))
        if not chat_id and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
            return fail(ConnectError("CHANNEL_FORMAT"))
        try:
            chat = _telegram(cfg.telegram_token, "getChat", {"chat_id": chat_id or f"@{username}"})
            if chat.get("type") != "channel":
                raise ConnectError("NOT_A_CHANNEL")
            me = _telegram(cfg.telegram_token, "getMe")
            member = _telegram(cfg.telegram_token, "getChatMember", {"chat_id": chat["id"], "user_id": me["id"]})
            if member.get("status") != "administrator" or not member.get("can_post_messages", False):
                raise ConnectError("CHANNEL_NO_RIGHTS")
        except ConnectError as e:
            if e.code == "TELEGRAM_ERROR" and "not found" in e.detail.lower():
                e = ConnectError("CHANNEL_NOT_FOUND", e.detail)
            return fail(e)
        stored = vault.load()
        cands = [c for c in _candidates(stored) if str(c["id"]) != str(chat["id"])]
        vault.update(telegram_channel_id=str(chat["id"]), telegram_channel_title=str(chat.get("title", ""))[:120],
                     telegram_channel_username=str(chat.get("username", "")),
                     telegram_channel_candidates=json.dumps(cands, ensure_ascii=False) if cands else None)
        reload_credentials()
        return jsonify(ok=True, account=chat.get("title", ""))

    @bp.delete("/admin/channels/telegram/channel")
    def channel_disconnect():
        require_admin()
        vault.update(telegram_channel_id=None, telegram_channel_title=None, telegram_channel_username=None)
        reload_credentials()
        return jsonify(ok=True)

    @bp.post("/admin/publish/telegram")
    def publish_telegram():
        require_admin()
        request.max_content_length = VIDEO_MAX + 2 * 1024 * 1024  # this route only; everything else stays at 1 MB
        stored = vault.load()
        channel = stored.get("telegram_channel_id")
        if not (cfg.telegram_token and channel):
            return fail(ConnectError("CHANNEL_NOT_CONNECTED"))
        # multipart turns every line break into CRLF; keep Telegram's text as written
        text = (request.form.get("text") or "").replace("\r\n", "\n").strip()
        upload = request.files.get("file")
        if not text and not upload:
            return fail(ConnectError("EMPTY"))
        if len(text) > 4 * TEXT_LIMIT:
            return fail(ConnectError("TEXT_TOO_LONG"))
        try:
            first = None
            rest = split_text(text) if text else []
            if upload:
                name = (upload.filename or "").lower()
                ext = name[name.rfind("."):] if "." in name else ""
                data = upload.read()
                if ext in PHOTO_EXT:
                    method, field, limit = "sendPhoto", "photo", PHOTO_MAX
                elif ext in VIDEO_EXT:
                    method, field, limit = "sendVideo", "video", VIDEO_MAX
                else:
                    raise ConnectError("UNSUPPORTED_FILE", ext)
                if len(data) > limit:
                    raise ConnectError("FILE_TOO_BIG", f"{len(data)}")
                fields = {"chat_id": channel}
                if text and len(text) <= CAPTION_LIMIT:
                    fields["caption"], rest = text, []  # short text rides along as the caption
                if method == "sendVideo":
                    fields["supports_streaming"] = "true"
                first = _send(cfg.telegram_token, method, fields, files={field: (name or field, data)})
            for part in rest:
                msg = _send(cfg.telegram_token, "sendMessage", {"chat_id": channel, "text": part})
                first = first or msg
        except ConnectError as e:
            if e.code == "CHANNEL_NO_RIGHTS":
                return fail(e, 403)
            return fail(e, 502 if e.code in ("NETWORK", "TELEGRAM_ERROR") else 400)
        url = post_link(channel, stored.get("telegram_channel_username", ""), first["message_id"])
        log.info("published to telegram channel %s", channel)
        return jsonify(ok=True, url=url, channel=stored.get("telegram_channel_title", ""))

    return bp
