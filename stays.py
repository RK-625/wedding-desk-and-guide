"""Collect quote-only hotel requests from invited guests, in the caller's transaction."""
import json
import os
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from openai import OpenAI, OpenAIError
from sponsor_services import flag_enabled

INTEREST = re.compile(
    r"\b(hotels?|accommodations?|lodging|check[- ]?in|check[- ]?out|guest ?houses?|"
    r"rooms?|bedrooms?|place to (?:sleep|stay)|somewhere to stay|need (?:a )?stay)\b", re.I)
CONFIRM = {"search", "confirm search", "yes", "yes please", "yes search", "search hotels", "submit"}
CANCEL = {"cancel", "cancel search", "cancel request", "cancel hotel request", "/cancel"}
FIELDS = ("destination", "check_in", "check_out", "rooms", "budget", "requirements")
OPENING = ("Tell me the city or area, check-in and check-out dates, and the adults and children "
           "staying in each room (include children's ages). Include your budget and any must-haves. "
           "I’ll show you the details before you start the quote search.")


def setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS stay_requests(
        id INTEGER PRIMARY KEY, guest INTEGER NOT NULL, details TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS stay_drafts(
        guest INTEGER PRIMARY KEY, draft TEXT NOT NULL, ready INTEGER NOT NULL)""")


def today():
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def extract(text, history):
    instruction = (
        f"Today is {today()} in India. Extract wedding hotel-search requirements ONLY from the "
        "guest's messages. Return a JSON object with intent (boolean: wants help finding a stay), "
        "destination (specific city/area/venue), check_in and check_out (ISO dates), rooms "
        "(one object per requested room: adults integer, child_ages array of ages), budget "
        "(text including currency and nightly/per-room/full-stay basis exactly as supplied), "
        "requirements (text: hard constraints, number of bedrooms, separate beds, location, "
        "accessibility, etc.), and clarify (one short question, or null). Use null for unknowns. "
        "Reconstruct the current request from history and edits. Never infer a date year for "
        "ambiguous dates, child ages, allocation across rooms, or who is staying from wedding attendance. "
        "If budget is a bare amount without currency or basis, use clarify. No budget or special "
        "needs mentioned is fine. A room and a bedroom can differ: preserve explicit bedroom needs. "
        "Ask when required details or rejected edits are ambiguous, including a reference to an "
        "unknown venue. Exclude names, contact information and irrelevant personal details from "
        "all extracted fields. A wedding FAQ about the venue or a question about the status of "
        "a booking alone is not a new search request. Never make a reservation."
    )
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise ValueError("missing extraction service")
    with OpenAI(api_key=key, base_url="https://api.deepseek.com",
                timeout=12.0, max_retries=0) as client:
        result = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_MODEL") or "deepseek-flash",
            messages=[{"role": "system", "content": instruction}, *history,
                      {"role": "user", "content": text}],
            response_format={"type": "json_object"}, max_tokens=1000,
            extra_body={"thinking": {"type": "disabled"}})
    data = json.loads(result.choices[0].message.content)
    if not isinstance(data, dict) or type(data.get("intent")) is not bool:
        raise ValueError("invalid extraction")
    return data


def clean_text(value, limit):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid text")
    return " ".join(value.split())


def normalize(raw):
    details = {f: clean_text(raw.get(f), 480) for f in FIELDS if f != "rooms"}
    details["rooms"] = []
    if not details["destination"]:
        return details, "Which city and area or venue should I search near?"
    try:
        start, end = (date.fromisoformat(details[f]) for f in ("check_in", "check_out"))
    except ValueError:
        return details, "What are your check-in and check-out dates, including the year?"
    if start < today() or end <= start:
        return details, "Please give a check-in date today or later and a later check-out date."
    rooms = raw.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        return details, "How many rooms do you need, and how many adults and children (with ages) will sleep in each?"
    for room in rooms:
        if not isinstance(room, dict):
            raise ValueError("invalid room")
        adults, ages = room.get("adults"), room.get("child_ages")
        if (type(adults) is not int or adults < 1 or not isinstance(ages, list)
                or any(type(age) is not int or not 0 <= age <= 17 for age in ages)):
            return details, "Please give at least one adult per room and the age of each child, or say adults only."
        details["rooms"].append({"adults": adults, "child_ages": ages})
    return details, clean_text(raw.get("clarify"), 320)


def summary(details):
    room_lines = []
    for i, room in enumerate(details["rooms"], 1):
        ages = room["child_ages"]
        children = ", children aged " + ", ".join(map(str, ages)) if ages else ", no children"
        room_lines.append(f"Room {i}: {room['adults']} adult(s){children}")
    return (f"{details['destination']} · {details['check_in']} to {details['check_out']}\n"
            + "\n".join(room_lines)
            + f"\nBudget: {details['budget'] or 'Not specified'}\nMust-haves: {details['requirements'] or 'None specified'}")


def message_key(text):
    return " ".join(text.lower().strip(".!? ").split())


def handle(db, uid, message, admins):
    text = message.strip()
    key = message_key(text)
    command = key.split(" ", 1)[0].split("@")[0]
    row = db.execute("SELECT draft,ready FROM stay_drafts WHERE guest=?", (uid,)).fetchone()
    if command.startswith("/") and command not in {"/stay", "/cancel"}:
        return None
    if key in CANCEL:
        if not row:
            return None
        db.execute("DELETE FROM stay_drafts WHERE guest=?", (uid,))
        return "Hotel request draft cancelled."
    if key in CONFIRM and row and row[1]:
        draft = json.loads(row[0])
        details = draft["details"]
        if date.fromisoformat(details["check_in"]) < today():
            db.execute("UPDATE stay_drafts SET ready=0 WHERE guest=?", (uid,))
            return "The check-in date has passed. What new dates should I use?"
        if not flag_enabled("hotel_search_enabled", default=True):
            return "New hotel searches are paused. Your draft is saved. Try “search” again later."
        if not admins or not os.getenv("TINYFISH_API_KEY", "").strip():
            return "The organizer’s hotel search is unavailable. Your draft is saved. Try “search” again later."
        consumed = db.execute("DELETE FROM stay_drafts WHERE guest=? AND ready=1 AND draft=?", (uid, row[0])).rowcount
        if not consumed:
            return "This request has already been processed."
        request_id = db.execute("INSERT INTO stay_requests(guest,details) VALUES(?,?)",
                                (uid, json.dumps(details))).lastrowid
        db.executemany("INSERT INTO stay_jobs(request_id,provider,status,created_at) VALUES(?,?,'queued',?)",
                       [(request_id, provider, int(time.time())) for provider in ("booking", "expedia")])
        notice = (f"Guest hotel request #{request_id} · Telegram guest ID {uid}\n"
                  f"{summary(details)}\nSearching Booking.com and Expedia.")
        # Each Unicode character occupies at most two UTF-16 units on Telegram.
        chunks = [notice[i:i + 2000] for i in range(0, len(notice), 2000)]
        db.executemany("INSERT INTO replies(role,chat,body) VALUES('organizer',?,?)",
                       [(admin, chunk) for admin in admins for chunk in chunks])
        return f"Hotel request #{request_id} sent. Your organizer will receive the quotes."
    if not row and key in CONFIRM:
        if key in {"search", "confirm search", "submit"}:
            previous = db.execute("SELECT max(id) FROM stay_requests WHERE guest=?", (uid,)).fetchone()[0]
            if previous:
                return f"Your latest hotel request is #{previous}. Use /stay to start a new one."
        return None
    if not row and command != "/stay" and not INTEREST.search(text):
        return None
    draft = json.loads(row[0]) if row else {"history": [], "details": {}}
    if command == "/stay":
        text = text.partition(" ")[2].strip()
        if not text:
            if row and row[1]:
                return summary(draft["details"]) + "\nReply “search” to submit or tell me what to change."
            db.execute("INSERT OR IGNORE INTO stay_drafts VALUES(?,?,0)", (uid, json.dumps(draft)))
            return OPENING
    try:
        raw = extract(text, draft["history"])
        if not row and command != "/stay" and not raw["intent"]:
            return None
        details, question = normalize(raw)
    except (OpenAIError, ValueError, TypeError, AttributeError, IndexError):
        if row:
            db.execute("UPDATE stay_drafts SET ready=0 WHERE guest=?", (uid,))
        return "I couldn’t read those hotel details. Please try again. Your existing draft is saved." if row else (
            "I couldn’t read those hotel details. Please try again with /stay.")
    result = question or (summary(details) + "\nReply “search” to send this request to the organizer and "
                          "check Booking.com and Expedia for quotes, or tell me what to change. Type “cancel” to discard.")
    draft["history"] = (draft["history"] + [{"role": "user", "content": text},
                                             {"role": "assistant", "content": result}])[-12:]
    draft["details"] = details
    db.execute("INSERT OR REPLACE INTO stay_drafts VALUES(?,?,?)", (uid, json.dumps(draft), int(not question)))
    return result
