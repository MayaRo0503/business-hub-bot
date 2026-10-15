"""Hebrew text helpers: normalization, prefix-aware tokens, PII masking.

Prefix handling and the entity patterns follow hebrew-chatbot-builder (SKILL.md,
"Caveats of naive substring matching" and extract_israeli_entities).
"""
import re

NIKUD = re.compile(r"[֑-ׇ]")
PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
# Attached prefixes: ו ה ב כ ל מ ש (and common pairs like "וב", "של", "מה")
PREFIX_LETTERS = set("והבכלמש")
FINAL = str.maketrans("ךםןףץ", "כמנפצ")

STOPWORDS = {
    "מה", "של", "יש", "את", "זה", "זו", "אני", "אתם", "אתן", "אתה", "את", "לי", "לך", "לכם",
    "על", "עם", "אפשר", "כמה", "איך", "מתי", "איפה", "האם", "גם", "רק", "או", "אם", "כי",
    "הוא", "היא", "הם", "הן", "אצלכם", "שלכם", "שלך", "בבקשה", "תודה", "היי", "שלום", "רציתי",
    "לשאול", "רוצה", "רוצים", "צריך", "צריכה", "בערך", "ממש", "קצת", "עוד", "כבר", "אז",
}


def normalize(text: str) -> str:
    text = NIKUD.sub("", text or "")
    text = text.replace("״", '"').replace("׳", "'")
    text = PUNCT.sub(" ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _variants(word: str) -> set[str]:
    """The word plus versions with up to two attached prefix letters removed."""
    out = {word}
    w = word
    for _ in range(2):
        if len(w) > 3 and w[0] in PREFIX_LETTERS:
            w = w[1:]
            out.add(w)
        else:
            break
    # full vs. defective spelling: "חנייה" / "חניה", "שוויון" / "שויון"
    out |= {v.replace("יי", "י").replace("וו", "ו") for v in out}
    return {v.translate(FINAL) for v in out}


def tokens(text: str) -> set[str]:
    """Content tokens (stopwords removed), each expanded with its prefix-stripped variants."""
    result: set[str] = set()
    for word in normalize(text).split():
        if word in STOPWORDS or len(word) < 2:
            continue
        result |= _variants(word)
    return result


def has_any(text: str, phrases) -> bool:
    norm = f" {normalize(text)} "
    return any(f" {normalize(p)} " in norm for p in phrases)


# --- PII masking (stored logs keep the question, not the identifiers) ---

_PHONE = [
    r"(?<!\d)05\d[\s-]?\d{3}[\s-]?\d{4}(?!\d)",
    r"(?<!\d)0[2-9][\s-]?\d{3}[\s-]?\d{4}(?!\d)",
    r"(?<!\d)\+?972[\s-]?\d{1,2}[\s-]?\d{3}[\s-]?\d{4}(?!\d)",
]
_EMAIL = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
_TZ = r"(?<!\d)\d{9}(?!\d)"
_CARD = r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"


def is_valid_teudat_zehut(value: str) -> bool:
    if not value.isdigit() or len(value) != 9:
        return False
    total = 0
    for i, ch in enumerate(value):
        n = int(ch) * (1 if i % 2 == 0 else 2)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def mask_pii(text: str) -> str:
    text = re.sub(_EMAIL, "[מייל]", text or "")
    text = re.sub(_CARD, "[מספר]", text)
    text = re.sub(_TZ, lambda m: "[ת״ז]" if is_valid_teudat_zehut(m.group()) else m.group(), text)
    for pattern in _PHONE:
        text = re.sub(pattern, "[טלפון]", text)
    return text
