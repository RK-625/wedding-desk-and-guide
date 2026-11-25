# Wedding Telegram bots

Two private Telegram chats share one wedding itinerary. Organizers publish events, record private booking notes, maintain guest FAQs, and see RSVP totals. Guests join by invitation, look up times and venues, ask about topics such as parking, and change their party size and vegetarian meal count. A booking is an organizer’s record of arrangements made with the vendor.

## Install and verify locally

Use Python 3.9+ with SQLite and Asia/Kolkata timezone data. A current Python build linked against OpenSSL 1.1.1+ is recommended for HTTPS. In this folder run:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -v
```

The tests use two pyTelegramBotAPI clients, a simulated Telegram API, and a temporary database. They run without credentials.

## Connect Telegram

1. Create two polling bots with @BotFather using /newbot twice.
2. Copy the configuration template with `cp .env.example .env`. Fill in ORGANIZER_BOT_TOKEN and GUEST_BOT_TOKEN with their respective tokens.
3. Set ORGANIZER_IDS to a comma-separated list of numeric Telegram user IDs. To discover yours, start the app with this field empty, send /id to the organizer bot in private, then fill in the returned ID and restart. An empty list locks organizer actions.
4. Run `.venv/bin/python bots.py`. Keep one instance running with internet access. Both bots use long polling. If reusing bots, first disable any existing webhooks and other pollers.

The app loads .env alongside bots.py. WEDDING_DB defaults to wedding.sqlite3 in the current working directory. Keep the database across restarts. The existing starter database upgrades on startup.

## First wedding walkthrough

In the organizer bot’s private chat, send these messages separately:

```
/event 2026-12-12 18:00 | Sangeet | Lotus Hall
/booking 2026-12-12 14:00 | Caterer meeting | Hotel lobby
/faq parking | Use the east gate beside Lotus Hall.
/invite
```

Open the invitation in the guest bot and tap Start. Anyone with the invitation can join. Ask “Where is the sangeet?” or “Where is parking?”, then send /rsvp 3 2. Run /attendance as the organizer. Send /rsvp 2 1 as the same guest to see the totals change.

Use /help in either bot to see its commands. All event times are IST. Guest questions match published event names and FAQ topics. Private bookings are visible only to allowlisted organizers. Use /remove with an item ID from /schedule or /bookings to remove an event or booking.

This text-based MVP runs before adding Sarvam voice transcription, Flagsmith, or NudgeBee. A real Telegram session requires your bot credentials.

## Optional conversational guest guidance

Set DEEPSEEK_API_KEY in .env and restart the bots. Invited guests can ask free-text questions. Wedding Guide sends the current question, published itinerary and host FAQs to DeepSeek. Each question is independent. Commands are handled locally, and keyword matching takes over if the service is unavailable. DEEPSEEK_MODEL optionally overrides deepseek-flash.
