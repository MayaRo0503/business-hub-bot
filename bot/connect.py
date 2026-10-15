"""Connecting channels from the dashboard (option A: each business owns its Meta app).

Admin API (Bearer ADMIN_TOKEN, called by the hub folder – never by the browser directly):
  GET    /admin/channels                      status of every channel (?verify=1 checks tokens live)
  POST   /admin/channels/telegram             {token}           → validate, setWebhook with secret, store
  POST   /admin/channels/telegram/owner-link                    → new one-time link for owner alerts
  DELETE /admin/channels/telegram
  POST   /admin/channels/meta-app             {appId, appSecret} → validate, store; returns what to paste in Meta
  POST   /admin/channels/meta/connect-link                      → one-time URL for the Facebook login popup
  DELETE /admin/channels/meta                                   → disconnect page + instagram
  POST   /admin/channels/whatsapp             {phoneNumberId, wabaId, token, pin?}
  DELETE /admin/channels/whatsapp

Public pages (the owner's browser, opened from the dashboard):
  GET  /connect/meta/start?t=<one-time>   → Facebook Login dialog (state = the one-time value)
  GET  /connect/meta/callback             → code exchange on the server (App Secret never leaves it)
  POST /connect/meta/page                 → choose a page when the owner manages several

Facts from the official docs this follows:
- code→token exchange is server-to-server with the App Secret (Facebook Login security)
- long-lived user token via grant_type=fb_exchange_token (~60 days); page tokens from it have no expiry
- page webhooks: POST /{page-id}/subscribed_apps?subscribed_fields=messages,messaging_postbacks (page token)
- WhatsApp: POST /{phone-number-id}/register {messaging_product, pin}; app subscription POST /{waba-id}/subscribed_apps
- Telegram: setWebhook(url, secret_token [A-Za-z0-9_-]{1,256}, allowed_updates); header X-Telegram-Bot-Api-Secret-Token
"""
import html
import logging
import re
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import Blueprint, jsonify, request

from .vault import mask

log = logging.getLogger("bot.connect")
TIMEOUT = 15
META_SCOPES = ["pages_show_list", "pages_messaging", "pages_manage_metadata", "business_management",
               "instagram_basic", "instagram_manage_messages"]
TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
NONCE_TTL = 600  # seconds


class ConnectError(Exception):
    """Carries a stable code for the dashboard (it shows Hebrew text per code)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def _graph(cfg, method, path, *, token=None, params=None, json_body=None):
    url = f"https://graph.facebook.com/{cfg.graph_version}/{path.lstrip('/')}"
    params = dict(params or {})
    if token:
        params["access_token"] = token
    try:
        r = requests.request(method, url, params=params, json=json_body, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise ConnectError("NETWORK", type(e).__name__)
    data = r.json() if r.headers.get("content-type", "").startswith(("application/json", "text/javascript")) else {}
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        code = err.get("code")
        if code in (190, 102):
            raise ConnectError("META_TOKEN_INVALID", err.get("message", ""))
        if code in (10, 200, 299) or (isinstance(code, int) and 200 <= code < 300):
            raise ConnectError("META_PERMISSION", err.get("message", ""))
        raise ConnectError("META_ERROR", err.get("message", f"HTTP {r.status_code}"))
    return data


def _telegram(token, method, payload=None):
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload or {}, timeout=TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise ConnectError("NETWORK", type(e).__name__)
    if not data.get("ok"):
        if r.status_code in (401, 404):
            raise ConnectError("TELEGRAM_TOKEN_INVALID", data.get("description", ""))
        raise ConnectError("TELEGRAM_ERROR", data.get("description", ""))
    return data["result"]


def _page(title: str, body: str, ok: bool = True) -> str:
    """A small self-contained RTL page for the popup window (no external assets)."""
    color = "#15803d" if ok else "#b45309"
    return f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:17px/1.6 system-ui,-apple-system,"Segoe UI",Arial,sans-serif;background:#f7f7f5;color:#1c1c1a;margin:0;
display:grid;place-items:center;min-height:100vh}}main{{background:#fff;border-radius:18px;padding:32px 28px;max-width:440px;
margin:16px;box-shadow:0 4px 24px rgba(0,0,0,.08)}}h1{{font-size:1.35rem;margin:0 0 10px;color:{color}}}
button,.btn{{font:inherit;border:1px solid #ddd;background:#fff;border-radius:10px;padding:10px 16px;margin:6px 0;
width:100%;text-align:right;cursor:pointer}}button:hover{{border-color:#999}}.muted{{color:#6b6b66;font-size:.92rem}}</style>
</head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""


ROUTER_FIELDS = {"whatsapp_phone_id", "waba_id", "whatsapp_token", "whatsapp_display", "whatsapp_name", "whatsapp_pin",
                 "whatsapp_coexistence", "page_id", "page_name", "page_token", "ig_id", "ig_username"}


def router_signature_ok(relay_key: str, headers, raw: bytes, max_age: int = 300) -> bool:
    """Everything from the router is signed: HMAC-SHA256(relay_key, "<timestamp>.<body>"), timestamp within 5 minutes."""
    import hashlib
    import hmac as _hmac
    ts = headers.get("X-Router-Timestamp", "")
    sig = headers.get("X-Router-Signature", "")
    if not relay_key or not ts.isdigit() or not sig or abs(time.time() - int(ts)) > max_age:
        return False
    expected = _hmac.new(relay_key.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return secrets.compare_digest(sig, expected)


def unroute(cfg, **ids):
    """Option B: tell Maya's router to forget this channel (signed with the relay key). Best effort."""
    import hashlib
    import hmac as _hmac
    import json as _json
    if cfg.connected_via != "router" or not (cfg.router_url and cfg.router_relay_key):
        return
    body = _json.dumps({k: v for k, v in ids.items() if v}).encode()
    ts = str(int(time.time()))
    sig = _hmac.new(cfg.router_relay_key.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    try:
        requests.post(f"{cfg.router_url}/api/unroute", data=body, timeout=TIMEOUT,
                      headers={"Content-Type": "application/json", "X-Router-Timestamp": ts, "X-Router-Signature": sig})
    except requests.RequestException as e:
        log.warning("unroute failed: %s", type(e).__name__)


def create_blueprint(cfg, vault, reload_credentials, require_admin):
    bp = Blueprint("connect", __name__)
    pending: dict[str, dict] = {}  # one-time values for the Facebook popup (single worker, see render.yaml)

    def base_url() -> str:
        url = cfg.public_url or request.host_url.rstrip("/")
        if not url.startswith("https://"):
            raise ConnectError("NEEDS_HTTPS", url)
        return url

    def fail(e: ConnectError, status=400):
        log.warning("connect failed: %s", e)
        return jsonify(ok=False, error=e.code, detail=e.detail[:300]), status

    def new_nonce(kind: str) -> str:
        now = time.time()
        for k in [k for k, v in pending.items() if v["exp"] < now]:
            pending.pop(k, None)
        nonce = secrets.token_urlsafe(24)
        pending[nonce] = {"kind": kind, "exp": now + NONCE_TTL}
        return nonce

    def take_nonce(nonce: str, keep=False):
        entry = pending.get(nonce or "")
        if not entry or entry["exp"] < time.time():
            pending.pop(nonce or "", None)
            return None
        if not keep:
            pending.pop(nonce, None)
        return entry

    # ---------------- status ----------------

    @bp.get("/admin/channels")
    def status():
        require_admin()
        verify = request.args.get("verify") == "1"
        ch = cfg.channels()

        def check(fn):
            if not verify:
                return None
            try:
                fn()
                return "ok"
            except ConnectError as e:
                return e.code

        return jsonify(
            telegram={"connected": ch["telegram"], "account": f"@{cfg.telegram_username}" if cfg.telegram_username else "",
                      "source": cfg.sources.get("telegram_token", ""), "ownerAlerts": bool(cfg.owner_telegram_chat_id),
                      "health": check(lambda: _telegram(cfg.telegram_token, "getMe")) if ch["telegram"] else None},
            metaApp={"configured": bool(cfg.meta_app_id and cfg.meta_app_secret), "appId": cfg.meta_app_id,
                     "secret": mask(cfg.meta_app_secret), "source": cfg.sources.get("meta_app_secret", "")},
            messenger={"connected": ch["messenger"], "account": cfg.page_name, "source": cfg.sources.get("page_token", ""),
                       "health": check(lambda: _graph(cfg, "GET", "me", token=cfg.page_token)) if ch["messenger"] else None},
            instagram={"connected": ch["instagram"] and bool(cfg.ig_id or cfg.sources.get("page_token") == "env"),
                       "account": f"@{cfg.ig_username}" if cfg.ig_username else ""},
            whatsapp={"connected": ch["whatsapp"], "account": " ".join(x for x in (cfg.whatsapp_name, cfg.whatsapp_display) if x),
                      "coexistence": cfg.whatsapp_coexistence,
                      "source": cfg.sources.get("whatsapp_token", ""),
                      "health": check(lambda: _graph(cfg, "GET", cfg.whatsapp_phone_id, token=cfg.whatsapp_token))
                      if ch["whatsapp"] else None},
            via=cfg.connected_via or "",
            webhook={"meta": f"{cfg.public_url}/webhook/meta" if cfg.public_url else "",
                     "verifyToken": cfg.meta_verify_token,
                     "oauthRedirect": f"{cfg.public_url}/connect/meta/callback" if cfg.public_url else ""},
        )

    # ---------------- telegram ----------------

    @bp.post("/admin/channels/telegram")
    def telegram_connect():
        require_admin()
        token = str((request.get_json(silent=True) or {}).get("token", "")).strip()
        if not TELEGRAM_TOKEN_RE.match(token):
            return fail(ConnectError("TELEGRAM_TOKEN_FORMAT"))
        try:
            me = _telegram(token, "getMe")
            secret = secrets.token_urlsafe(32)  # A-Z a-z 0-9 _ - only, as Telegram requires
            _telegram(token, "setWebhook", {"url": f"{base_url()}/webhook/telegram", "secret_token": secret,
                                            "allowed_updates": ["message", "callback_query"], "drop_pending_updates": True})
        except ConnectError as e:
            return fail(e)
        code = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
        vault.update(telegram_token=token, telegram_secret=secret, telegram_username=me.get("username", ""),
                     owner_pairing_code=code)
        reload_credentials()
        return jsonify(ok=True, account=f"@{me.get('username', '')}",
                       ownerLink=f"https://t.me/{me.get('username', '')}?start={code}")

    @bp.post("/admin/channels/telegram/owner-link")
    def telegram_owner_link():
        require_admin()
        if not cfg.telegram_username:
            return fail(ConnectError("TELEGRAM_NOT_CONNECTED"))
        code = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
        vault.update(owner_pairing_code=code)
        reload_credentials()
        return jsonify(ok=True, ownerLink=f"https://t.me/{cfg.telegram_username}?start={code}")

    @bp.delete("/admin/channels/telegram")
    def telegram_disconnect():
        require_admin()
        if cfg.telegram_token:
            try:
                _telegram(cfg.telegram_token, "deleteWebhook", {"drop_pending_updates": True})
            except ConnectError as e:
                log.warning("deleteWebhook: %s", e)  # disconnect locally anyway
        vault.update(telegram_token=None, telegram_secret=None, telegram_username=None,
                     owner_telegram_chat_id=None, owner_pairing_code=None)
        reload_credentials()
        return jsonify(ok=True, stillFromEnv=bool(cfg.telegram_token))

    # ---------------- meta app ----------------

    @bp.post("/admin/channels/meta-app")
    def meta_app():
        require_admin()
        body = request.get_json(silent=True) or {}
        app_id = str(body.get("appId", "")).strip()
        app_secret = str(body.get("appSecret", "")).strip()
        if not re.fullmatch(r"\d{6,20}", app_id) or not re.fullmatch(r"[0-9a-f]{32}", app_secret):
            return fail(ConnectError("META_APP_FORMAT"))
        try:
            app = _graph(cfg, "GET", app_id, token=f"{app_id}|{app_secret}", params={"fields": "name"})
            base = base_url()
        except ConnectError as e:
            return fail(e)
        vault.update(meta_app_id=app_id, meta_app_secret=app_secret)
        reload_credentials()
        return jsonify(ok=True, appName=app.get("name", ""), webhookUrl=f"{base}/webhook/meta",
                       verifyToken=cfg.meta_verify_token, oauthRedirect=f"{base}/connect/meta/callback")

    @bp.post("/admin/channels/meta/connect-link")
    def meta_connect_link():
        require_admin()
        if not (cfg.meta_app_id and cfg.meta_app_secret):
            return fail(ConnectError("META_APP_MISSING"))
        try:
            return jsonify(ok=True, url=f"{base_url()}/connect/meta/start?t={new_nonce('meta')}", expiresIn=NONCE_TTL)
        except ConnectError as e:
            return fail(e)

    @bp.delete("/admin/channels/meta")
    def meta_disconnect():
        require_admin()
        if cfg.page_id and cfg.page_token and cfg.sources.get("page_token") == "dashboard":
            try:
                _graph(cfg, "DELETE", f"{cfg.page_id}/subscribed_apps", token=cfg.page_token)
            except ConnectError as e:
                log.warning("unsubscribe page: %s", e)
        unroute(cfg, page_id=cfg.page_id, ig_id=cfg.ig_id)
        vault.update(page_id=None, page_name=None, page_token=None, ig_id=None, ig_username=None)
        reload_credentials()
        return jsonify(ok=True, stillFromEnv=bool(cfg.page_token))

    # ---------------- facebook login popup (public, guarded by the one-time value) ----------------

    @bp.get("/connect/meta/start")
    def meta_start():
        nonce = request.args.get("t", "")
        if not take_nonce(nonce, keep=True):
            return _page("הקישור פג", "<p>חזרו לדשבורד ולחצו שוב על <b>חיבור</b>.</p>", ok=False), 400
        try:
            redirect_uri = f"{base_url()}/connect/meta/callback"
        except ConnectError:
            return _page("השרת לא מוכן", "<p>כתובת השרת צריכה להתחיל ב-https.</p>", ok=False), 400
        qs = urlencode({"client_id": cfg.meta_app_id, "redirect_uri": redirect_uri, "state": nonce,
                        "response_type": "code", "scope": ",".join(META_SCOPES)})
        return "", 302, {"Location": f"https://www.facebook.com/{cfg.graph_version}/dialog/oauth?{qs}"}

    @bp.get("/connect/meta/callback")
    def meta_callback():
        nonce = request.args.get("state", "")
        if request.args.get("error"):
            take_nonce(nonce)
            return _page("החיבור בוטל", "<p>אפשר לסגור את החלון ולנסות שוב מהדשבורד.</p>", ok=False)
        if not take_nonce(nonce, keep=True):
            return _page("הקישור פג", "<p>חזרו לדשבורד ולחצו שוב על <b>חיבור</b>.</p>", ok=False), 400
        try:
            redirect_uri = f"{base_url()}/connect/meta/callback"
            short = _graph(cfg, "GET", "oauth/access_token", params={
                "client_id": cfg.meta_app_id, "client_secret": cfg.meta_app_secret,
                "redirect_uri": redirect_uri, "code": request.args.get("code", "")})
            long_lived = _graph(cfg, "GET", "oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": cfg.meta_app_id,
                "client_secret": cfg.meta_app_secret, "fb_exchange_token": short["access_token"]})
            pages = _graph(cfg, "GET", "me/accounts", token=long_lived["access_token"], params={
                "fields": "id,name,access_token,instagram_business_account{id,username}", "limit": 50}).get("data", [])
        except ConnectError as e:
            take_nonce(nonce)
            return _page("החיבור לא הצליח", f"<p>פייסבוק החזירה שגיאה ({html.escape(e.code)}). אפשר לסגור ולנסות שוב.</p>",
                         ok=False), 400
        if not pages:
            take_nonce(nonce)
            return _page("לא נמצא עמוד", "<p>החשבון לא מנהל אף עמוד פייסבוק, או שלא סימנתם עמוד בחלון ההרשאות.</p>", ok=False)
        if len(pages) == 1:
            take_nonce(nonce)
            return _finish_page(pages[0])
        pending[nonce]["pages"] = {p["id"]: p for p in pages}  # tokens stay on the server
        buttons = "".join(
            f'<button name="page" value="{html.escape(p["id"])}">{html.escape(p["name"])}'
            + (f' <span class="muted">· @{html.escape(p["instagram_business_account"]["username"])}</span>'
               if p.get("instagram_business_account", {}).get("username") else "") + "</button>"
            for p in pages)
        return _page("איזה עמוד לחבר?", f'<form method="post" action="/connect/meta/page">'
                     f'<input type="hidden" name="state" value="{html.escape(nonce)}">{buttons}</form>')

    @bp.post("/connect/meta/page")
    def meta_choose_page():
        entry = take_nonce(request.form.get("state", ""))
        page = (entry or {}).get("pages", {}).get(request.form.get("page", ""))
        if not page:
            return _page("הקישור פג", "<p>חזרו לדשבורד ולחצו שוב על <b>חיבור</b>.</p>", ok=False), 400
        return _finish_page(page)

    def _finish_page(page):
        try:
            _graph(cfg, "POST", f"{page['id']}/subscribed_apps", token=page["access_token"],
                   params={"subscribed_fields": "messages,messaging_postbacks"})
        except ConnectError as e:
            return _page("העמוד לא חובר", f"<p>לא הצלחתי לרשום את הבוט לעמוד ({html.escape(e.code)}).</p>", ok=False), 400
        ig = page.get("instagram_business_account") or {}
        vault.update(page_id=page["id"], page_name=page.get("name", ""), page_token=page["access_token"],
                     ig_id=ig.get("id"), ig_username=ig.get("username"))
        reload_credentials()
        ig_line = (f"<p>גם אינסטגרם <b>@{html.escape(ig['username'])}</b> מחובר דרך העמוד.</p>" if ig.get("username")
                   else '<p class="muted">לעמוד הזה לא מחובר חשבון אינסטגרם מקצועי, אז רק Messenger פעיל.</p>')
        return _page("חובר ✓", f"<p>העמוד <b>{html.escape(page.get('name', ''))}</b> מחובר לבוט.</p>{ig_line}"
                     '<p class="muted">אפשר לסגור את החלון ולחזור לדשבורד.</p>')

    # ---------------- whatsapp (option A: token of the business's own system user) ----------------

    @bp.post("/admin/channels/whatsapp")
    def whatsapp_connect():
        require_admin()
        body = request.get_json(silent=True) or {}
        phone_id = str(body.get("phoneNumberId", "")).strip()
        waba_id = str(body.get("wabaId", "")).strip()
        token = str(body.get("token", "")).strip()
        pin = str(body.get("pin", "")).strip()
        if not (re.fullmatch(r"\d{6,25}", phone_id) and re.fullmatch(r"\d{6,25}", waba_id) and len(token) > 40):
            return fail(ConnectError("WHATSAPP_FORMAT"))
        if pin and not re.fullmatch(r"\d{6}", pin):
            return fail(ConnectError("WHATSAPP_PIN_FORMAT"))
        if not (cfg.meta_app_id and cfg.meta_app_secret):
            return fail(ConnectError("META_APP_MISSING"))
        try:
            phone = _graph(cfg, "GET", phone_id, token=token, params={"fields": "display_phone_number,verified_name"})
            _graph(cfg, "POST", f"{waba_id}/subscribed_apps", token=token)
            if pin:
                _graph(cfg, "POST", f"{phone_id}/register", token=token, json_body={"messaging_product": "whatsapp", "pin": pin})
        except ConnectError as e:
            return fail(e)
        vault.update(whatsapp_phone_id=phone_id, waba_id=waba_id, whatsapp_token=token,
                     whatsapp_display=phone.get("display_phone_number", ""), whatsapp_name=phone.get("verified_name", ""))
        reload_credentials()
        return jsonify(ok=True, account=f"{phone.get('verified_name', '')} {phone.get('display_phone_number', '')}".strip())

    @bp.delete("/admin/channels/whatsapp")
    def whatsapp_disconnect():
        require_admin()
        unroute(cfg, waba_id=cfg.waba_id)
        vault.update(whatsapp_phone_id=None, waba_id=None, whatsapp_token=None, whatsapp_display=None, whatsapp_name=None,
                     whatsapp_pin=None, whatsapp_coexistence=None)
        reload_credentials()
        return jsonify(ok=True, stillFromEnv=bool(cfg.whatsapp_token))

    # ---------------- option B: Maya's router (Tech Provider app, Embedded Signup v4) ----------------

    @bp.post("/admin/channels/router/start")
    def router_start():
        """Ask the router for a one-time connect link. The router URL comes from the hub package (hub.config.json)."""
        require_admin()
        router = str((request.get_json(silent=True) or {}).get("routerUrl", "")).rstrip("/")
        if not re.fullmatch(r"https://[a-z0-9.-]+(:\d+)?", router):
            return fail(ConnectError("BAD_ROUTER_URL"))
        relay_key = cfg.router_relay_key or secrets.token_urlsafe(32)
        try:
            r = requests.post(f"{router}/api/sessions", json={"serverUrl": base_url(), "relayKey": relay_key}, timeout=TIMEOUT)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            return fail(ConnectError("ROUTER_UNREACHABLE", type(e).__name__))
        except ConnectError as e:
            return fail(e)
        if not data.get("ok"):
            return fail(ConnectError("ROUTER_ERROR", str(data.get("error", ""))))
        vault.update(router_url=router, router_relay_key=relay_key)
        reload_credentials()
        return jsonify(ok=True, url=data["connectUrl"], expiresIn=data.get("expiresIn"))

    @bp.post("/router/credentials")
    def router_credentials():
        """The router pushes the tokens of this business only, signed with this server's relay key."""
        raw = request.get_data()
        if not router_signature_ok(cfg.router_relay_key, request.headers, raw):
            return jsonify(ok=False, error="BAD_SIGNATURE"), 403
        payload = request.get_json(silent=True) or {}
        creds = {k: v for k, v in (payload.get("credentials") or {}).items() if k in ROUTER_FIELDS and v is not None}
        if not creds:
            return jsonify(ok=False, error="EMPTY"), 400
        creds["whatsapp_coexistence"] = str(bool(creds.get("whatsapp_coexistence")))
        vault.update(connected_via="router", **{k: str(v) for k, v in creds.items()})
        reload_credentials()
        log.info("router connected: %s", ", ".join(payload.get("connected") or []))
        return jsonify(ok=True)

    return bp


def handle_owner_pairing(cfg, vault, reload_credentials, chat_id: str, text: str):
    """'/start <code>' from the owner's Telegram links their chat for handoff alerts. Returns reply text or None."""
    m = re.fullmatch(r"/start\s+([A-Za-z0-9]{8,64})", text.strip())
    stored = vault.load().get("owner_pairing_code")
    if not m or not stored or not secrets.compare_digest(m.group(1), stored):
        return None
    vault.update(owner_telegram_chat_id=chat_id, owner_pairing_code=None)
    reload_credentials()
    return "✅ מעכשיו תקבלו כאן התראה בכל פעם שלקוח מבקש לדבר עם מישהו מהצוות."
