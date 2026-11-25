# Wedding Telegram bots

Run one organizer bot and one guest bot for a single wedding. Both read the same SQLite file. The organizer posts the guest itinerary and separately records private booking or meeting notes. Recording a booking does not reserve a venue or contact a vendor.

## Run locally

Use Python 3.9+ with the Asia/Kolkata time zone installed. No pip packages are required.

1. In Telegram, open @BotFather and use /newbot twice. Give each bot its own username. Use fresh bots configured for polling.
2. Obtain the organizer's numeric Telegram user ID. For a fresh organizer bot, send it a private message, then call getUpdates before starting the program. The ID is in message.from.id.
3. Open a terminal in this folder and set the environment variables below, replacing the placeholders. Keep both tokens private.

```sh
export ORGANIZER_BOT_TOKEN='organizer-token'
export GUEST_BOT_TOKEN='guest-token'
export ORGANIZER_IDS='123456789'
python3 bots.py
```

For multiple organizers, separate their numeric IDs with commas. Keep the process running with internet access. WEDDING_DB optionally sets the database path. Keep the database private and reuse it across restarts.

## Try the two sides

In a private chat with the organizer bot, send these as separate messages:

```
/event 2026-12-12 18:00 | Sangeet | Lotus Hall
/booking 2026-12-12 14:00 | Caterer meeting | Hotel lobby
/invite
```

Open the returned guest link in Telegram and tap Start. Anyone with the link can join. In the guest chat, send “Where is the sangeet?”, then /rsvp 3 2. In the organizer chat, /attendance shows the updated totals. A second RSVP from the same account replaces its earlier counts. Use /help in either chat for commands.

Guest guidance currently supports text queries by event name, the next event, and the schedule. This starter runs independently of the three hackathon sponsor services.
