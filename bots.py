"""Two Telegram bots, one wedding, one local database. Python 3.9+."""
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import urlopen, Request

DB = os.getenv("WEDDING_DB", "wedding.sqlite3")
ORG_HELP = """Organizer commands (times are IST)
/event 2026-12-12 18:00 | Sangeet | Lotus Hall
/booking 2026-12-12 14:00 | Caterer meeting | Hotel lobby
/schedule — guest itinerary
/bookings — private booking notes
/remove 3 — delete item by ID
/invite — guest invitation link
/attendance — confirmed guests and vegetarian meals"""
GUEST_HELP = """Welcome to the wedding! Try asking “Where is the sangeet?” or “What’s next?”
/schedule — times and venues
/rsvp 3 2 — confirm 3 guests including 2 vegetarian meals
/myrsvp — check your reply"""


def setup(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS guests(id INTEGER PRIMARY KEY, party INTEGER, veg INTEGER);
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT,
        at TEXT, name TEXT, place TEXT);
    CREATE TABLE IF NOT EXISTS cursor(role TEXT PRIMARY KEY, offset INTEGER);
    """)
    db.execute("INSERT OR IGNORE INTO config VALUES ('invite',?)", (secrets.token_urlsafe(18),))
    db.commit()


def items(db, kind):
    return db.execute("SELECT id,at,name,place FROM items WHERE kind=? ORDER BY at,id", (kind,)).fetchall()


def render(rows, private=False):
    return "\n".join(f"{'#' + str(i) + ' · ' if private else ''}{name} · {at} IST · {place}"
                     for i, at, name, place in rows) or "No entries yet."


def reply(db, role, uid, message, admins, botname):
    command, _, arg = message.strip().partition(" ")
    command = command.split("@")[0].lower()
    invite = db.execute("SELECT value FROM config WHERE key='invite'").fetchone()[0]
    if role == "organizer":
        if uid not in admins:
            return "This bot is for the wedding organizers. Ask your host for the guest invitation."
        if command in ("/event", "/booking"):
            fields = [field.strip() for field in arg.split("|")]
            try:
                at, name, place = fields
                stamp = datetime.strptime(at, "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M")
                if not name or not place:
                    raise ValueError()
            except ValueError:
                return f"Use {command} YYYY-MM-DD HH:MM | Name | Venue (IST)."
            kind = command[1:]
            row = db.execute("INSERT INTO items(kind,at,name,place) VALUES(?,?,?,?)", (kind, stamp, name, place))
            visibility = "Visible to invited guests" if kind == "event" else "Private organizer note"
            return f"{visibility}. Saved #{row.lastrowid}."
        if command == "/remove":
            changed = db.execute("DELETE FROM items WHERE id=?", (arg.strip(),)).rowcount
            return "Removed." if changed else "No item with that ID."
        if command == "/invite":
            return f"Share this wedding invitation with your guests: https://t.me/{botname}?start={invite}"
        if command in ("/schedule", "/bookings"):
            return render(items(db, "event" if command == "/schedule" else "booking"), True)
        if command == "/attendance":
            count, party, veg = db.execute("SELECT count(*),sum(party),sum(veg) FROM guests WHERE party IS NOT NULL").fetchone()
            return f"{count} RSVPs · {party or 0} guests · {veg or 0} vegetarian meals"
        return ORG_HELP

    if command == "/start" and arg == invite:
        db.execute("INSERT OR IGNORE INTO guests(id) VALUES(?)", (uid,))
    guest = db.execute("SELECT party,veg FROM guests WHERE id=?", (uid,)).fetchone()
    if guest is None:
        return "Open the invitation link from your wedding organizer, then tap Start."
    if command == "/rsvp":
        try:
            party, veg = map(int, arg.split())
            if not 0 <= veg <= party <= 10000:
                raise ValueError()
        except ValueError:
            return "Use /rsvp 3 2 for 3 guests including 2 vegetarian meals. Use /rsvp 0 0 to decline."
        db.execute("UPDATE guests SET party=?, veg=? WHERE id=?", (party, veg, uid))
        return f"RSVP saved: {party} guests, {veg} vegetarian meals."
    if command == "/myrsvp":
        return f"{guest[0]} guests, {guest[1]} vegetarian meals." if guest[0] is not None else "No RSVP yet. Try /rsvp 3 2."
    events = items(db, "event")
    if command == "/schedule":
        return render(events)
    if command in ("/start", "/help"):
        return GUEST_HELP
    question = message.casefold()
    if "next" in question.split() or "next?" in question:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M")
        upcoming = [event for event in events if event[1] >= now]
        return render(upcoming[:1]) if upcoming else "No upcoming event is listed yet."
    matches = [event for event in events if event[2].casefold() in question]
    return render(matches) if matches else GUEST_HELP


def api(token, method, **params):
    req = Request(f"https://api.telegram.org/bot{token}/{method}", data=urlencode(params).encode())
    with urlopen(req, timeout=40) as res:
        return json.load(res)["result"]


def poll(role, token, admins, guest_name):
    with sqlite3.connect(DB, timeout=15) as db:
        while True:
            try:
                row = db.execute("SELECT offset FROM cursor WHERE role=?", (role,)).fetchone()
                for update in api(token, "getUpdates", offset=row[0] if row else 0, timeout=25):
                    msg = update.get("message", {})
                    chat = msg.get("chat", {})
                    with db:
                        answer = None
                        if chat.get("type") == "private":
                            answer = reply(db, role, msg["from"]["id"], msg.get("text") or "/help", admins, guest_name)
                        db.execute("INSERT OR REPLACE INTO cursor VALUES(?,?)", (role, update["update_id"] + 1))
                    if answer:
                        for start in range(0, len(answer), 3500):
                            api(token, "sendMessage", chat_id=chat["id"], text=answer[start:start+3500])
            except Exception as exc:
                logging.warning("%s Telegram operation failed (%s). Retrying polling.", role, type(exc).__name__)
                time.sleep(3)


if __name__ == "__main__":
    tokens = {role: os.environ[role.upper() + "_BOT_TOKEN"] for role in ("organizer", "guest")}
    if len(set(tokens.values())) != 2:
        raise SystemExit("Set two distinct BotFather tokens.")
    admins = {int(x.strip()) for x in os.environ["ORGANIZER_IDS"].split(",")}
    guest_name = api(tokens["guest"], "getMe")["username"]
    with sqlite3.connect(DB) as connection:
        setup(connection)
    for role, token in tokens.items():
        threading.Thread(target=poll, args=(role, token, admins, guest_name)).start()
