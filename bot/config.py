"""Settings: environment variables (Render / .env) plus channels connected from the dashboard.

Channel credentials can come from two places. A value connected from the dashboard (stored
encrypted, see vault.py) wins over the environment variable, so a business can connect or
replace a channel without touching Render. Everything else is environment-only.
"""
import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# attribute → environment variable, for every credential the dashboard can also set
CREDENTIALS = {
    "meta_app_id": "META_APP_ID",
    "meta_app_secret": "META_APP_SECRET",
    "whatsapp_phone_id": "WHATSAPP_PHONE_ID",
    "whatsapp_token": "FACEBOOK_ACCESS_TOKEN",  # name used by hebrew-chatbot-builder
    "waba_id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
    "page_token": "PAGE_ACCESS_TOKEN",          # Messenger + Instagram (via the Facebook Page)
    "page_id": "PAGE_ID",
    "telegram_token": "TELEGRAM_BOT_TOKEN",
    "telegram_secret": "TELEGRAM_WEBHOOK_SECRET",
    "owner_telegram_chat_id": "OWNER_TELEGRAM_CHAT_ID",
}


class Config:
    def __init__(self):
        self.admin_token = _env("ADMIN_TOKEN")
        self.secret_key = _env("SECRET_KEY") or self.admin_token
        self.database_url = _env("DATABASE_URL", "sqlite:///bot.db")
        self.mode = _env("BOT_MODE", "basic")  # basic | smart
        self.business_name = _env("BUSINESS_NAME")
        self.session_timeout_min = int(_env("SESSION_TIMEOUT_MIN", "30"))  # israeli-chatbot-analytics default
        self.handoff_silence_hours = float(_env("HANDOFF_SILENCE_HOURS", "12"))
        self.pii_salt = _env("PII_SALT") or self.admin_token
        self.public_url = (_env("PUBLIC_URL") or _env("RENDER_EXTERNAL_URL")).rstrip("/")

        self.meta_verify_token = _env("META_VERIFY_TOKEN")
        self.graph_version = _env("GRAPH_API_VERSION", "v25.0")

        # Smart mode
        self.anthropic_key = _env("ANTHROPIC_API_KEY")
        self.claude_model = _env("CLAUDE_MODEL", "claude-haiku-5-5")

        # display-only details filled by dashboard connections
        self.telegram_username = self.page_name = self.ig_id = self.ig_username = ""
        self.whatsapp_display = self.whatsapp_name = ""
        self.router_url = self.router_relay_key = self.connected_via = ""
        self.whatsapp_coexistence = False

        self._env_creds = {attr: _env(var) for attr, var in CREDENTIALS.items()}
        self.sources: dict[str, str] = {}
        self.apply({})

    def apply(self, stored: dict):
        """Overlay credentials connected from the dashboard on top of the environment."""
        for attr in CREDENTIALS:
            value = stored.get(attr) or self._env_creds[attr]
            setattr(self, attr, value)
            self.sources[attr] = "dashboard" if stored.get(attr) else ("env" if value else "")
        for attr in ("telegram_username", "page_name", "ig_id", "ig_username", "whatsapp_display", "whatsapp_name",
                     "router_url", "router_relay_key", "connected_via"):
            setattr(self, attr, stored.get(attr, ""))
        self.whatsapp_coexistence = stored.get("whatsapp_coexistence") == "True"

    def channels(self) -> dict:
        # option A: the business's own app verifies Meta's signature here.
        # option B: Maya's router verifies it and relays, signed with router_relay_key.
        meta = bool(self.meta_app_secret and self.meta_verify_token) or bool(self.router_relay_key)
        return {
            "whatsapp": meta and bool(self.whatsapp_phone_id and self.whatsapp_token),
            "messenger": meta and bool(self.page_token),
            "instagram": meta and bool(self.page_token),
            "telegram": bool(self.telegram_token and self.telegram_secret),
        }

    @property
    def smart_enabled(self) -> bool:
        return self.mode == "smart" and bool(self.anthropic_key)
