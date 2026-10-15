"""Local preview: what would the bot answer, using the hub's knowledge.json? Nothing is sent or stored.

    python simulate.py <knowledge.json> "יש חניה?" ["מה המחיר?" ...]

Basic mode only (no API key needed); standard library only, so it runs before `pip install`.
Prints one JSON object per question.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.brain import Brain  # noqa: E402
from bot.knowledge import KnowledgeError, validate  # noqa: E402


class MemoryStore:
    pg = False

    def __init__(self, knowledge):
        self.kv = {"knowledge": knowledge}

    def get_kv(self, k):
        return self.kv.get(k)


class Cfg:
    business_name = ""
    smart_enabled = False
    session_timeout_min = 30
    handoff_silence_hours = 12


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        raw = json.load(f)
    try:
        validate({k: v for k, v in raw.items() if not k.startswith("_")})
    except KnowledgeError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(2)
    brain = Brain(MemoryStore(raw), Cfg())
    for question in sys.argv[2:]:
        # mid-conversation: shows the real answer or fallback, not the first-contact welcome
        reply = brain.handle("test", "preview", question, dry_run=True, new_session=False)
        print(json.dumps({
            "question": question,
            "answer": reply.text if reply else None,
            "buttons": [t for _, t in reply.options] if reply else [],
            "intent": reply.intent if reply else None,
            "confidence": reply.confidence if reply else None,
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
