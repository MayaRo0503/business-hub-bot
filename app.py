"""Business Hub bot server – one deployment per business (Render).

Webhooks:  GET/POST /webhook/meta      WhatsApp + Messenger + Instagram
           POST     /webhook/telegram
Admin:     /admin/*  (Authorization: Bearer ADMIN_TOKEN) – used by scripts/bot.mjs in the hub folder
Health:    GET /health

Webhooks are acknowledged right away and processed in a thread pool, because Meta requires
an HTTP 200 within 5 seconds and smart mode may take longer than that.
"""
import hmac
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from flask import Flask, abort, jsonify, request

from bot import channels
from bot.brain import Brain
from bot.config import Config
from bot.connect import create_blueprint, handle_owner_pairing, router_signature_ok
from bot.knowledge import KnowledgeError, validate
from bot.store import OUTCOME_RULE, Store, iso, now
from bot.vault import Vault

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("bot")

VERSION = "1.0.0"


def create_app(config: Config | None = None, store: Store | None = None, smart=None, executor=None) -> Flask:
    cfg = config or Config()
    db = store or Store(cfg.database_url, cfg.pii_salt or "local")
    if smart is None and cfg.smart_enabled:
        from bot.smart import Smart
        smart = Smart(cfg.anthropic_key, cfg.claude_model)
    brain = Brain(db, cfg, smart)
    pool = executor or ThreadPoolExecutor(max_workers=8)

    # channels connected from the dashboard: encrypted in the DB, overlaid on the environment
    vault = Vault(db, cfg.secret_key or "local-dev-only")
    loaded = {"version": -1}

    def reload_credentials():
        loaded["version"] = vault.version()
        cfg.apply(vault.load())

    def refresh_credentials():
        if vault.version() != loaded["version"]:
            reload_credentials()

    reload_credentials()

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

    def process(inc: channels.Incoming):
        try:
            if not db.first_time(inc.msg_id):
                return  # a retry of a message we already handled
            if inc.channel == "telegram" and inc.text.startswith("/start "):
                paired = handle_owner_pairing(cfg, vault, reload_credentials, inc.user_id, inc.text)
                if paired:
                    channels.send_telegram(cfg, inc.user_id, paired, [])
                    return
            if inc.channel == "telegram" and inc.text.strip() == "/myid":
                channels.send_telegram(cfg, inc.user_id, f"מזהה הצ'אט שלך: {inc.user_id}", [])
                return
            reply = brain.handle(inc.channel, inc.user_id, inc.text, inc.payload, inc.contact)
            if reply is None:
                return
            channels.send(cfg, inc, reply.text, reply.options)
            if reply.handoff:
                channels.notify_owner(cfg, inc, inc.text or inc.payload)
        except Exception:
            log.exception("failed to process %s message", inc.channel)

    # ---------------- webhooks ----------------

    @app.get("/webhook/meta")
    def meta_verify():
        if (request.args.get("hub.mode") == "subscribe" and cfg.meta_verify_token
                and hmac.compare_digest(request.args.get("hub.verify_token", ""), cfg.meta_verify_token)):
            return request.args.get("hub.challenge", ""), 200
        return "Forbidden", 403

    @app.before_request
    def _fresh_credentials():
        refresh_credentials()

    @app.post("/webhook/meta")
    def meta_webhook():
        if not cfg.meta_app_secret:
            log.error("META_APP_SECRET is not set; refusing webhooks")
            return "Server misconfigured", 500
        body = request.get_data()
        if not channels.meta_signature_ok(cfg.meta_app_secret, body, request.headers.get("X-Hub-Signature-256", "")):
            return "Invalid signature", 403
        for inc in channels.parse_meta(request.get_json(silent=True) or {}):
            pool.submit(process, inc)
        return jsonify(status="ok")

    @app.post("/webhook/router")
    def router_webhook():
        """Option B: Meta → Maya's router (verifies Meta's signature) → here, signed with this server's relay key."""
        raw = request.get_data()
        if not router_signature_ok(cfg.router_relay_key, request.headers, raw):
            return "Invalid signature", 403
        for inc in channels.parse_meta(request.get_json(silent=True) or {}):
            pool.submit(process, inc)
        return jsonify(status="ok")

    @app.post("/webhook/telegram")
    def telegram_webhook():
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not cfg.telegram_secret or not hmac.compare_digest(secret, cfg.telegram_secret):
            return "Forbidden", 403
        update = request.get_json(silent=True) or {}
        channels.answer_callback(cfg, update)
        for inc in channels.parse_telegram(update):
            pool.submit(process, inc)
        return jsonify(ok=True)

    @app.get("/health")
    def health():
        return jsonify(status="ok", version=VERSION)

    # ---------------- admin ----------------

    def require_admin():
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not cfg.admin_token or not hmac.compare_digest(token, cfg.admin_token):
            abort(401)

    def since_param(default_days=30) -> str:
        raw = request.args.get("since")
        try:
            return iso(datetime.fromisoformat(raw.replace("Z", "+00:00"))) if raw else iso(now() - timedelta(days=default_days))
        except ValueError:
            abort(400)

    @app.get("/admin/status")
    def admin_status():
        require_admin()
        k = brain.knowledge()
        saved = (db.get_kv("knowledge") or {}).get("_savedAt")
        return jsonify(version=VERSION, mode="smart" if cfg.smart_enabled else "basic",
                       requestedMode=cfg.mode, channels=cfg.channels(),
                       ownerNotifications=bool(cfg.telegram_token and cfg.owner_telegram_chat_id),
                       knowledge={"faq": len(k["faq"]), "savedAt": saved},
                       database="postgres" if db.pg else "sqlite")

    @app.get("/admin/knowledge")
    def admin_get_knowledge():
        require_admin()
        return jsonify(db.get_kv("knowledge") or {})

    @app.put("/admin/knowledge")
    def admin_put_knowledge():
        require_admin()
        raw = request.get_json(silent=True)
        try:
            validate({k: v for k, v in (raw or {}).items() if not str(k).startswith("_")} if isinstance(raw, dict) else raw)
        except KnowledgeError as e:
            return jsonify(error=str(e)), 400
        raw["_savedAt"] = iso(now())
        db.set_kv("knowledge", raw)
        if isinstance(raw.get("voice"), dict):
            db.set_kv("voice", raw["voice"])
        return jsonify(ok=True, savedAt=raw["_savedAt"], faq=len(raw.get("faq", [])))

    @app.post("/admin/test")
    def admin_test():
        """Dry run: what would the bot answer? Nothing is stored or sent."""
        require_admin()
        body = request.get_json(silent=True) or {}
        reply = brain.handle("test", "dry-run", str(body.get("text", ""))[:1000], str(body.get("payload", ""))[:100],
                             dry_run=True, new_session=bool(body.get("newSession", False)))
        if reply is None:
            return jsonify(reply=None)
        return jsonify(reply={"text": reply.text, "options": [t for _, t in reply.options], "intent": reply.intent,
                              "confidence": reply.confidence, "handoff": reply.handoff})

    @app.get("/admin/logs")
    def admin_logs():
        require_admin()
        db.prune_seen()
        return jsonify(schema="israeli-chatbot-analytics/standard-log", outcomeRule=OUTCOME_RULE,
                       sessionTimeoutMinutes=cfg.session_timeout_min,
                       conversations=db.export(since_param(), cfg.session_timeout_min))

    @app.post("/admin/telegram/setup")
    def admin_telegram_setup():
        """Registers this server as the Telegram webhook, with the secret header. Token never leaves the server."""
        require_admin()
        if not (cfg.telegram_token and cfg.telegram_secret):
            return jsonify(error="TELEGRAM_BOT_TOKEN or TELEGRAM_WEBHOOK_SECRET is not set"), 400
        base = os.environ.get("RENDER_EXTERNAL_URL") or request.host_url.rstrip("/")
        if not base.startswith("https://"):
            return jsonify(error=f"Telegram needs an https URL, got {base}"), 400
        ok = channels.telegram_set_webhook(cfg, f"{base}/webhook/telegram")
        return (jsonify(ok=True, url=f"{base}/webhook/telegram") if ok else (jsonify(error="Telegram refused the webhook"), 502))

    @app.get("/admin/handoffs")
    def admin_handoffs():
        require_admin()
        return jsonify(handoffs=db.handoffs(since_param(default_days=7)))

    app.register_blueprint(create_blueprint(cfg, vault, reload_credentials, require_admin))
    return app


if __name__ == "__main__":
    # Local run: python app.py  (production uses gunicorn, see render.yaml)
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", 8080)))
else:
    app = create_app() if os.environ.get("BOT_LAZY") != "1" else None
