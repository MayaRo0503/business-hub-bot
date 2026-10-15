"""The bot's knowledge base: what Claude Code builds in /chatbot and pushes to the server.

Schema: templates/knowledge.template.json in the hub folder. Missing fields get defaults,
so a minimal {"faq": [{"q": ..., "a": ...}]} is valid.
"""
import copy

DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]

DEFAULTS = {
    "version": 1,
    "businessName": "",
    "welcome": "היי! 👋 אני הבוט של {business}. אפשר לשאול אותי שאלה, או לבחור מהאפשרויות:",
    "menu": [],
    "faq": [],
    "hours": {
        "sun": ["09:00", "18:00"], "mon": ["09:00", "18:00"], "tue": ["09:00", "18:00"],
        "wed": ["09:00", "18:00"], "thu": ["09:00", "18:00"], "fri": None, "sat": None,
    },
    "handoff": {
        "open": "העברתי את הפנייה לצוות שלנו 🙏 נחזור אליך כאן בהקדם.",
        "closed": "קיבלנו את הפנייה 🙏 כרגע אנחנו סגורים, ונחזור אליך בשעות הפעילות.",
    },
    "fallback": "לא בטוח שהבנתי 🤔 אפשר לנסח אחרת, או לבחור מהאפשרויות:",
    "fallbackHandoff": "נראה שאני לא מצליח לעזור בזה. להעביר את השאלה לצוות?",
    "closing": "בשמחה! אם יש עוד משהו, אני כאן 🙂",
    "labels": {"menu": "תפריט", "handoff": "לדבר עם נציג", "more": "אפשרויות"},
}

MAX_FAQ = 300
MAX_TEXT = 1000


class KnowledgeError(ValueError):
    pass


def validate(raw) -> dict:
    """Return a normalized knowledge dict or raise KnowledgeError (message in Hebrew-free English for logs)."""
    if not isinstance(raw, dict):
        raise KnowledgeError("knowledge must be an object")
    k = copy.deepcopy(DEFAULTS)
    for key in ("version", "businessName", "welcome", "fallback", "fallbackHandoff", "closing"):
        if key in raw:
            k[key] = raw[key]
    for key in ("handoff", "labels"):
        if isinstance(raw.get(key), dict):
            k[key].update({kk: str(v)[:MAX_TEXT] for kk, v in raw[key].items()})

    faq = raw.get("faq", [])
    if not isinstance(faq, list) or len(faq) > MAX_FAQ:
        raise KnowledgeError(f"faq must be a list of up to {MAX_FAQ} items")
    seen = set()
    k["faq"] = []
    for i, item in enumerate(faq):
        if not isinstance(item, dict) or not str(item.get("q", "")).strip() or not str(item.get("a", "")).strip():
            raise KnowledgeError(f"faq[{i}] needs q and a")
        fid = str(item.get("id") or f"q{i + 1}")[:40]
        if fid in seen:
            raise KnowledgeError(f"duplicate faq id {fid}")
        seen.add(fid)
        k["faq"].append({
            "id": fid,
            "q": str(item["q"])[:300],
            "a": str(item["a"])[:MAX_TEXT],
            "title": str(item.get("title") or item["q"])[:24],
            "keywords": [str(w)[:40] for w in item.get("keywords", [])][:30],
        })

    menu = raw.get("menu")
    if menu is None:
        menu = [f["id"] for f in k["faq"][:4]]
    k["menu"] = [m for m in menu if m in seen][:9]

    hours = raw.get("hours")
    if isinstance(hours, dict):
        for day in DAYS:
            span = hours.get(day, k["hours"][day])
            if span is not None and not (isinstance(span, list) and len(span) == 2 and all(_is_hhmm(t) for t in span)):
                raise KnowledgeError(f"hours.{day} must be null or [\"HH:MM\", \"HH:MM\"]")
            k["hours"][day] = span
    return k


def _is_hhmm(value) -> bool:
    try:
        h, m = str(value).split(":")
        return 0 <= int(h) <= 24 and 0 <= int(m) < 60
    except ValueError:
        return False


def faq_by_id(k: dict, fid: str):
    return next((f for f in k["faq"] if f["id"] == fid), None)
