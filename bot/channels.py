"""Channel adapters: turn a webhook payload into Incoming messages, and a Reply into an API call.

WhatsApp, Messenger and Instagram arrive on the same Meta webhook (one app, one signature secret).
Telegram has its own webhook with a secret header.
"""
import hashlib
import hmac
import logging
from dataclasses import dataclass

import requests

log = logging.getLogger("bot.channels")
TIMEOUT = 10


@dataclass
class Incoming:
    channel: str
    user_id: str
    text: str = ""
    payload: str = ""
    msg_id: str = ""
    contact: str = ""


# ---------------- Meta (WhatsApp / Messenger / Instagram) ----------------

def meta_signature_ok(app_secret: str, body: bytes, header: str) -> bool:
    # Never skip this when the secret is missing: refuse instead (see hebrew-chatbot-builder).
    if not app_secret or not header:
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


def parse_meta(data: dict) -> list[Incoming]:
    out: list[Incoming] = []
    obj = data.get("object")
    for entry in data.get("entry", []) or []:
        if obj == "whatsapp_business_account":
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts", []) or []}
                for m in value.get("messages", []) or []:
                    sender = m.get("from", "")
                    inc = Incoming("whatsapp", sender, msg_id=m.get("id", ""),
                                   contact=f"+{sender} {names.get(sender, '')}".strip())
                    mtype = m.get("type")
                    if mtype == "text":
                        inc.text = (m.get("text") or {}).get("body", "")
                    elif mtype == "interactive":
                        it = m.get("interactive") or {}
                        reply = it.get("button_reply") or it.get("list_reply") or {}
                        inc.payload = reply.get("id", "")
                    elif mtype == "button":  # template quick-reply button
                        inc.text = (m.get("button") or {}).get("text", "")
                    else:
                        inc.text = ""  # media: answered with the fallback/welcome flow
                    out.append(inc)
        elif obj in ("page", "instagram"):
            channel = "messenger" if obj == "page" else "instagram"
            for ev in entry.get("messaging", []) or []:
                msg = ev.get("message") or {}
                if msg.get("is_echo"):
                    continue  # our own outgoing message
                sender = (ev.get("sender") or {}).get("id", "")
                if msg:
                    out.append(Incoming(channel, sender, text=msg.get("text", ""),
                                        payload=(msg.get("quick_reply") or {}).get("payload", ""),
                                        msg_id=msg.get("mid", ""), contact=f"{channel}:{sender}"))
                elif ev.get("postback"):
                    pb = ev["postback"]
                    out.append(Incoming(channel, sender, payload=pb.get("payload", ""),
                                        msg_id=pb.get("mid", ""), contact=f"{channel}:{sender}"))
    return [m for m in out if m.user_id and (m.text or m.payload or m.channel == "whatsapp")]


def _post(url: str, json_body: dict, *, params=None, headers=None, what=""):
    try:
        r = requests.post(url, json=json_body, params=params, headers=headers, timeout=TIMEOUT)
        if r.status_code >= 400:
            log.error("%s send failed: %s %s", what, r.status_code, r.text[:300])
        return r.status_code < 400
    except requests.RequestException as e:
        log.error("%s send error: %s", what, type(e).__name__)
        return False


def send_whatsapp(cfg, to: str, text: str, options: list):
    url = f"https://graph.facebook.com/{cfg.graph_version}/{cfg.whatsapp_phone_id}/messages"
    headers = {"Authorization": f"Bearer {cfg.whatsapp_token}"}
    body = {"messaging_product": "whatsapp", "to": to}
    if not options:
        body.update(type="text", text={"body": text[:4096]})
    elif len(options) <= 3:
        body.update(type="interactive", interactive={
            "type": "button", "body": {"text": text[:1024]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": oid, "title": title[:20]}} for oid, title in options]},
        })
    else:
        body.update(type="interactive", interactive={
            "type": "list", "body": {"text": text[:1024]},
            "action": {"button": "אפשרויות", "sections": [{"title": "אפשר לבחור", "rows": [
                {"id": oid, "title": title[:24]} for oid, title in options[:10]]}]},
        })
    return _post(url, body, headers=headers, what="whatsapp")


def send_page(cfg, channel: str, to: str, text: str, options: list):
    """Messenger and Instagram (Instagram messaging through the connected Facebook Page)."""
    url = f"https://graph.facebook.com/{cfg.graph_version}/me/messages"
    message = {"text": text[:2000]}
    if options:
        message["quick_replies"] = [{"content_type": "text", "title": title[:20], "payload": oid} for oid, title in options[:13]]
    body = {"recipient": {"id": to}, "messaging_type": "RESPONSE", "message": message}
    return _post(url, body, params={"access_token": cfg.page_token}, what=channel)


# ---------------- Telegram ----------------

def parse_telegram(update: dict) -> list[Incoming]:
    uid = f"tg:{update.get('update_id', '')}"
    if "callback_query" in update:
        cq = update["callback_query"]
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
        user = cq.get("from") or {}
        return [Incoming("telegram", str(chat), payload=cq.get("data", ""), msg_id=uid,
                         contact=_tg_contact(user, chat))] if chat else []
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return []
    chat = (msg.get("chat") or {}).get("id")
    return [Incoming("telegram", str(chat), text=msg["text"], msg_id=uid,
                     contact=_tg_contact(msg.get("from") or {}, chat))]


def _tg_contact(user: dict, chat) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    handle = f"@{user['username']}" if user.get("username") else f"chat {chat}"
    return f"{handle} {name}".strip()


def send_telegram(cfg, chat_id: str, text: str, options: list):
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    body = {"chat_id": chat_id, "text": text[:4096]}
    if options:
        body["reply_markup"] = {"inline_keyboard": [[{"text": title, "callback_data": oid[:64]}] for oid, title in options]}
    return _post(url, body, what="telegram")


# my_chat_member: the bot was added to a channel, so the owner can pick it for publishing (bot/publish.py)
TELEGRAM_UPDATES = ["message", "callback_query", "my_chat_member"]


def telegram_set_webhook(cfg, url: str) -> bool:
    return _post(f"https://api.telegram.org/bot{cfg.telegram_token}/setWebhook",
                 {"url": url, "secret_token": cfg.telegram_secret, "allowed_updates": TELEGRAM_UPDATES},
                 what="telegram-setWebhook")


def answer_callback(cfg, update: dict):
    if "callback_query" in update:
        _post(f"https://api.telegram.org/bot{cfg.telegram_token}/answerCallbackQuery",
              {"callback_query_id": update["callback_query"].get("id")}, what="telegram-callback")


# ---------------- dispatch ----------------

def send(cfg, inc: Incoming, text: str, options: list):
    if inc.channel == "whatsapp":
        return send_whatsapp(cfg, inc.user_id, text, options)
    if inc.channel in ("messenger", "instagram"):
        return send_page(cfg, inc.channel, inc.user_id, text, options)
    if inc.channel == "telegram":
        return send_telegram(cfg, inc.user_id, text, options)
    return False


def notify_owner(cfg, inc: Incoming, last_text: str):
    """Tell the business owner that a customer asked for a person (Telegram, if configured)."""
    if not (cfg.telegram_token and cfg.owner_telegram_chat_id):
        return False
    names = {"whatsapp": "WhatsApp", "messenger": "Messenger", "instagram": "Instagram", "telegram": "Telegram"}
    text = (f"🔔 לקוח ביקש לדבר עם מישהו מהצוות\n"
            f"ערוץ: {names.get(inc.channel, inc.channel)}\n"
            f"פרטים: {inc.contact or inc.user_id}\n"
            f"ההודעה האחרונה: {last_text[:500]}")
    return send_telegram(cfg, cfg.owner_telegram_chat_id, text, [])
