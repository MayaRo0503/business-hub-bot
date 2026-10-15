# Business Hub – bot server

One deployment per business. Answers customers on WhatsApp, Messenger, Instagram and Telegram
from a knowledge base that the business builds in Claude Code (`/chatbot` in the hub folder).

- **Basic mode:** FAQ matching with Hebrew prefix and spelling handling, menu, business hours, handoff. No AI cost.
- **Smart mode** (`BOT_MODE=smart` + `ANTHROPIC_API_KEY`): Claude Haiku 5.5 answers free-form questions, only from the
  knowledge base, only when basic matching found nothing. Any error falls back to basic.
- **Handoff:** the bot goes quiet for that customer for `HANDOFF_SILENCE_HOURS`, and the owner gets a Telegram alert.
- **Logs** are stored in the `israeli-chatbot-analytics` standard schema, with phone numbers, IDs and emails masked.

## Endpoints
| | |
|---|---|
| `GET/POST /webhook/meta` | WhatsApp + Messenger + Instagram (X-Hub-Signature-256 verified; refused if `META_APP_SECRET` is unset) |
| `POST /webhook/telegram` | Telegram (secret header verified) |
| `GET /health` | health check |
| `/admin/status`, `/admin/knowledge` (GET/PUT), `/admin/test`, `/admin/logs`, `/admin/handoffs`, `/admin/telegram/setup` | `Authorization: Bearer ADMIN_TOKEN`, used by `scripts/bot.mjs` in the hub folder |

Webhooks are acknowledged immediately and processed in a thread pool (Meta's 5-second limit).
Duplicate deliveries are ignored by message id.

## Run locally
```bash
python -m venv .venv
```
```bash
.venv/Scripts/pip install -r requirements.txt pytest
```
```bash
.venv/Scripts/python -m pytest -q
```
Copy `.env.example` to `.env`, export the values, then run `python app.py` (port 8080).
To try answers without a server: `python simulate.py knowledge.json "יש חניה?"` (stdlib only).

## Deploy
`render.yaml` is a Render Blueprint. The step-by-step meeting guide is in the hub folder:
`docs/הטמעה/מדריך-פגישת-הטמעה.md`.
