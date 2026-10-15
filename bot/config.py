"""All settings come from environment variables (set in Render, or in .env for local runs)."""
import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Config:
    def __init__(self):
        self.admin_token = _env("ADMIN_TOKEN")
        self.database_url = _env("DATABASE_URL", "sqlite:///bot.db")
        self.mode = _env("BOT_MODE", "basic")  # basic | smart
        self.business_name = _env("BUSINESS_NAME")
        self.session_timeout_min = int(_env("SESSION_TIMEOUT_MIN", "30"))  # israeli-chatbot-analytics default
        self.handoff_silence_hours = float(_env("HANDOFF_SILENCE_HOURS", "12"))
        self.pii_salt = _env("PII_SALT") or self.admin_token

        # Meta: one app serves WhatsApp, Messenger and Instagram
        self.meta_app_secret = _env("META_APP_SECRET")
        self.meta_verify_token = _env("META_VERIFY_TOKEN")
        self.graph_version = _env("GRAPH_API_VERSION", "v25.0")
        self.whatsapp_phone_id = _env("WHATSAPP_PHONE_ID")
        self.whatsapp_token = _env("FACEBOOK_ACCESS_TOKEN")  # name used by hebrew-chatbot-builder
        self.page_token = _env("PAGE_ACCESS_TOKEN")          # Messenger + Instagram (via the Facebook Page)

        self.telegram_token = _env("TELEGRAM_BOT_TOKEN")
        self.telegram_secret = _env("TELEGRAM_WEBHOOK_SECRET")
        self.owner_telegram_chat_id = _env("OWNER_TELEGRAM_CHAT_ID")

        # Smart mode
        self.anthropic_key = _env("ANTHROPIC_API_KEY")
        self.claude_model = _env("CLAUDE_MODEL", "claude-haiku-5-5")

    def channels(self) -> dict:
        meta = bool(self.meta_app_secret and self.meta_verify_token)
        return {
            "whatsapp": meta and bool(self.whatsapp_phone_id and self.whatsapp_token),
            "messenger": meta and bool(self.page_token),
            "instagram": meta and bool(self.page_token),
            "telegram": bool(self.telegram_token and self.telegram_secret),
        }

    @property
    def smart_enabled(self) -> bool:
        return self.mode == "smart" and bool(self.anthropic_key)
