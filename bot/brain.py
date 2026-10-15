"""Decides what the bot answers. Channel-agnostic: gets text (or a button id), returns a Reply.

Basic mode: FAQ matching with Hebrew prefix handling, a menu, business hours and handoff.
Smart mode adds Claude for questions the matcher did not catch (bot/smart.py).
"""
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import hebrew
from .knowledge import DAYS, faq_by_id
from .store import iso, now

IL = ZoneInfo("Asia/Jerusalem")

GREETINGS = ["שלום", "היי", "הי", "הלו", "אהלן", "בוקר טוב", "ערב טוב", "צהריים טובים", "מה נשמע", "מה קורה", "hi", "hello"]
HANDOFF_WORDS = ["נציג", "נציגה", "בן אדם", "בנאדם", "אדם אמיתי", "לדבר עם מישהו", "לדבר עם מישהי", "תעביר", "תעבירי",
                 "מנהל", "מנהלת", "תלונה", "להתלונן", "שיחזרו אליי", "שיחזרו אלי", "תחזרו אליי", "agent", "human"]
THANKS = ["תודה", "תודה רבה", "תודה רבה!", "מעולה תודה", "אחלה", "סבבה תודה", "ביי", "להתראות", "יום טוב", "thanks"]
MENU_WORDS = ["תפריט", "menu", "start", "/start", "התחלה"]

MATCH_THRESHOLD = 0.5
DAY_HE = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]


@dataclass
class Reply:
    text: str
    options: list = field(default_factory=list)  # [(id, title)]
    intent: str = ""
    confidence: float | None = None
    handoff: bool = False


def is_open(hours: dict, at: datetime | None = None) -> bool:
    at = (at or datetime.now(IL)).astimezone(IL)
    span = hours.get(DAYS[(at.weekday() + 1) % 7])  # Python: Monday=0; ours: Sunday first
    if not span:
        return False
    hhmm = at.strftime("%H:%M")
    return span[0] <= hhmm < span[1]


def match_faq(knowledge: dict, text: str):
    """Best FAQ for the text, by overlap of prefix-stripped content tokens. Returns (faq, score)."""
    words = hebrew.tokens(text)
    if not words:
        return None, 0.0
    best, best_score = None, 0.0
    for f in knowledge["faq"]:
        kw = set()
        for w in f["keywords"]:
            kw |= hebrew.tokens(w)
        q = hebrew.tokens(f["q"])
        hits_kw = len(words & kw)
        hits_q = len(words & q)
        # explicit keywords are strong signals; question words count relative to the question length
        score = max(min(1.0, hits_kw * 0.6), hits_q / max(1, min(len(q), 3)))
        if score > best_score:
            best, best_score = f, score
    return (best, round(best_score, 2)) if best_score >= MATCH_THRESHOLD else (None, round(best_score, 2))


class Brain:
    def __init__(self, store, config, smart=None):
        self.store = store
        self.config = config
        self.smart = smart
        self._k_cache = (None, None)

    # -- knowledge --
    def knowledge(self) -> dict:
        from .knowledge import validate
        raw = self.store.get_kv("knowledge") or {}
        key = raw.get("_savedAt")
        if self._k_cache[1] is None or self._k_cache[0] != key:
            self._k_cache = (key, validate({k: v for k, v in raw.items() if not k.startswith("_")}))
        return self._k_cache[1]

    def business(self, k: dict) -> str:
        return k.get("businessName") or self.config.business_name or "העסק"

    def menu(self, k: dict, with_handoff=True):
        opts = [(f"faq:{fid}", faq_by_id(k, fid)["title"]) for fid in k["menu"]]
        if with_handoff:
            opts.append(("handoff", k["labels"]["handoff"]))
        return opts

    # -- main entry --
    def handle(self, channel: str, user_id: str, text: str = "", payload: str = "", contact: str = "", dry_run=False,
               new_session=True):
        """Process one incoming message. Returns a Reply, or None when the bot should stay silent."""
        started = time.monotonic()
        k = self.knowledge()
        if dry_run:
            session = {"session_id": "dry-run", "state": "start", "fallback_count": 0, "handoff_until": None,
                       "new": new_session, "channel": channel}
        else:
            session = self.store.session_for(channel, user_id, self.config.session_timeout_min)
            self.store.add_message(session["session_id"], "user", hebrew.mask_pii(text or payload))

        reply = self._decide(k, session, text.strip(), payload, contact, dry_run)
        if reply is None:
            return None
        if not dry_run:
            ms = int((time.monotonic() - started) * 1000)
            self.store.add_message(session["session_id"], "bot", reply.text, reply.intent, reply.confidence, ms)
            fields = {"state": "handoff" if reply.handoff else ("fallback" if reply.intent == "fallback" else "active")}
            if reply.intent == "fallback":
                fields["fallback_count"] = session["fallback_count"] + 1
            elif reply.intent.startswith(("faq:", "smart_answer")):
                fields["fallback_count"] = 0
            if reply.handoff:
                fields["handoff_until"] = iso(now() + timedelta(hours=self.config.handoff_silence_hours))
                fields["handoff_at"] = iso(now())
                fields["contact"] = contact or user_id
            self.store.update_session(session, **fields)
        return reply

    def _decide(self, k, session, text, payload, contact, dry_run):
        # A person took over: the bot stays quiet unless the customer asks for the menu again.
        if session.get("handoff_until") and session["handoff_until"] > iso(now()):
            if payload == "menu" or hebrew.has_any(text, MENU_WORDS):
                return Reply(k["welcome"].replace("{business}", self.business(k)), self.menu(k), "welcome")
            return None

        # Buttons / quick replies
        if payload:
            if payload == "handoff":
                return self._handoff(k)
            if payload == "menu":
                return Reply(k["welcome"].replace("{business}", self.business(k)), self.menu(k), "welcome")
            if payload.startswith("faq:") and (f := faq_by_id(k, payload[4:])):
                return Reply(f["a"], self._after_answer(k), f"faq:{f['id']}", 1.0)

        # Menu by number ("2") – for channels without buttons
        if text.isdigit() and 1 <= int(text) <= len(k["menu"]):
            f = faq_by_id(k, k["menu"][int(text) - 1])
            return Reply(f["a"], self._after_answer(k), f"faq:{f['id']}", 1.0)

        if hebrew.has_any(text, MENU_WORDS):
            return Reply(k["welcome"].replace("{business}", self.business(k)), self.menu(k), "welcome")
        if hebrew.has_any(text, HANDOFF_WORDS):
            return self._handoff(k)

        f, score = match_faq(k, text)
        if f:
            prefix = f"היי! אני הבוט של {self.business(k)} 🤖\n\n" if session["new"] else ""
            return Reply(prefix + f["a"], self._after_answer(k), f"faq:{f['id']}", score)

        if hebrew.has_any(text, THANKS) and not session["new"]:
            return Reply(k["closing"], [], "closing")
        if session["new"] or hebrew.has_any(text, GREETINGS):
            return Reply(k["welcome"].replace("{business}", self.business(k)), self.menu(k), "welcome")

        if self.smart and self.config.smart_enabled:
            smart = self._smart(k, session, text, dry_run)
            if smart:
                return smart

        if session["fallback_count"] >= 1:
            return Reply(k["fallbackHandoff"], [("handoff", k["labels"]["handoff"]), ("menu", k["labels"]["menu"])],
                         "fallback", score)
        return Reply(k["fallback"], self.menu(k), "fallback", score)

    def _smart(self, k, session, text, dry_run):
        from .smart import build_system
        voice = self.store.get_kv("voice") or {}
        system = build_system(k, self.business(k), voice.get("register", "informal"), voice.get("gender", "neutral"))
        history = [] if dry_run else self.store.recent_messages(session["session_id"])[:-1]
        at = datetime.now(IL)
        context = f"[עכשיו בישראל: יום {DAY_HE[(at.weekday() + 1) % 7]} {at.strftime('%H:%M')}. העסק כרגע {'פתוח' if is_open(k['hours'], at) else 'סגור'}]"
        result = self.smart.ask(system, history, text, context)
        if not result:
            return None
        if result["action"] == "handoff":
            return self._handoff(k)
        if result["action"] == "unknown":
            return Reply(result["text"], [("handoff", k["labels"]["handoff"]), ("menu", k["labels"]["menu"])], "fallback")
        return Reply(result["text"], self._after_answer(k), "smart_answer")

    def _after_answer(self, k):
        return [("menu", k["labels"]["menu"]), ("handoff", k["labels"]["handoff"])]

    def _handoff(self, k):
        text = k["handoff"]["open" if is_open(k["hours"]) else "closed"]
        return Reply(text, [], "handoff", handoff=True)
