"""Search public hotel inventory via TinyFish. The worker never makes reservations."""
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timezone
from urllib.parse import urlsplit, urlencode
from zoneinfo import ZoneInfo

import requests
import tiny_browser

API = "https://agent.tinyfish.ai/v1"
PROVIDERS = {"booking": "Booking.com", "expedia": "Expedia"}
HOSTS = {"booking": {"www.booking.com", "booking.com"},
         "expedia": {"www.expedia.com", "expedia.com", "www.expedia.co.in", "expedia.co.in"}}
POLL = 20
BROWSER_LEASE = 6 * 60
LEASE = 65
DEADLINE = 30 * 60
TEXT = {"type": "string"}
ROOM = {"type": "object", "properties": {"adults": {"type": "integer"},
        "child_ages": {"type": "array", "items": {"type": "integer"}}},
        "required": ["adults", "child_ages"]}
FIELDS = {k: TEXT for k in (
    "hotel", "url", "check_in", "check_out", "room_configuration", "currency", "taxes",
    "availability_evidence", "total_evidence", "requirements_evidence", "distance", "locality", "gaps")}
FIELDS.update(rooms={"type": "array", "items": ROOM}, total={"type": "number", "nullable": True},
              full_stay_total={"type": "boolean"}, taxes_included={"type": "boolean"},
              requirements_met={"type": "boolean"})
SCHEMA = {"type": "object", "properties": {
    "quotes": {"type": "array", "maxItems": 3, "items": {
        "type": "object", "properties": FIELDS, "required": list(FIELDS)}},
    "note": TEXT}, "required": ["quotes", "note"]}


def setup(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS stay_jobs (
          request_id INTEGER, provider TEXT, status TEXT DEFAULT 'queued', run_id TEXT,
          created_at INTEGER NOT NULL, next_at INTEGER DEFAULT 0, started INTEGER,
          marker TEXT, lease TEXT, quotes TEXT, note TEXT, checked INTEGER,
          PRIMARY KEY(request_id,provider));
        CREATE INDEX IF NOT EXISTS due_stay_jobs ON stay_jobs(status,next_at);
        CREATE TABLE IF NOT EXISTS stay_browser_cleanup(
            session_id TEXT PRIMARY KEY, due INTEGER NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS stay_reports (request_id INTEGER PRIMARY KEY,body TEXT NOT NULL);
    """)

    # Existing deployments may already have in-flight TinyAgent runs. Migrate without
    # relaunching them or confusing their remote IDs with new browser sessions.
    columns = {row[1] for row in db.execute("PRAGMA table_info(stay_jobs)")}
    with db:
        if "engine" not in columns:
            db.execute("ALTER TABLE stay_jobs ADD COLUMN engine TEXT NOT NULL DEFAULT 'tinybrowser'")
            db.execute("UPDATE stay_jobs SET engine='tinyagent' WHERE status!='queued'")
        if "attempts" not in columns:
            db.execute("ALTER TABLE stay_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")


def search_url(provider, details):
    if provider == "expedia":
        query = {"destination": details["destination"], "startDate": details["check_in"],
                 "endDate": details["check_out"], "rooms": len(details["rooms"]),
                 "adults": ",".join(str(room["adults"]) for room in details["rooms"])}
        children = [f"{n}_{age}" for n, room in enumerate(details["rooms"], 1) for age in room["child_ages"]]
        if children:
            query["children"] = ",".join(children)
        return "https://www.expedia.com/Hotel-Search?" + urlencode(query)
    rooms = details["rooms"]
    return "https://www.booking.com/searchresults.html?" + urlencode({
        "ss": details["destination"], "checkin": details["check_in"], "checkout": details["check_out"],
        "no_rooms": len(rooms), "group_adults": sum(r["adults"] for r in rooms),
        "group_children": sum(len(r["child_ages"]) for r in rooms),
        "age": [a for r in rooms for a in r["child_ages"]]}, doseq=True)


def legacy_get(path, **kwargs):
    key = os.getenv("TINYFISH_API_KEY", "").strip()
    if not key:
        raise ValueError("Missing TinyFish key")
    response = requests.get(API + path, headers={"X-API-Key": key},
                                timeout=(5, 20), allow_redirects=False, **kwargs)
    response.raise_for_status()
    return response.json()


def safe_text(value, limit=240):
    if not isinstance(value, str):
        return ""
    # Only the separately validated provider link may be included as a clickable URL.
    value = re.sub(r"(?:https?://|www\.)\S+", "", value, flags=re.I)
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:limit]


def property_url(url, provider):
    if not isinstance(url, str) or len(url) > 1300 or not url.isascii() or re.search(r"[\s\\\x00-\x1f]", url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc.lower() not in HOSTS[provider]:
            return None
        path = parts.path
        if provider == "booking" and not re.fullmatch(r"/hotel/[a-z]{2}/[^/]+\.html", path):
            return None
        if provider == "expedia" and not re.search(r"\.h\d+\.Hotel-Information/?$", path, re.I):
            return None
        return url
    except ValueError:
        return None


def occupancy(rooms):
    if not isinstance(rooms, list):
        return None
    try:
        if any(type(r["adults"]) is not int or r["adults"] < 1 or not isinstance(r["child_ages"], list)
               or any(type(a) is not int or not 0 <= a <= 17 for a in r["child_ages"]) for r in rooms):
            return None
        return sorted((r["adults"], tuple(sorted(r["child_ages"]))) for r in rooms)
    except (KeyError, TypeError):
        return None


MONEY = re.compile(r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?)\s*([kK])?(?![\d.,])")
CURRENCIES = {"INR": ("INR", "₹", "rupees", "Rs.", "Rs "),
              "USD": ("USD", "US$"), "GBP": ("GBP", "£"), "EUR": ("EUR", "€"),
              "AUD": ("AUD", "AU$"), "CAD": ("CAD", "CA$")}
NEGATIVE = re.compile(r"\b(?:unconfirmed|unverified|unavailable|sold out|not available|"
                      r"not confirmed|not specified|not listed|unclear|unknown|no rooms|"
                      r"not accessible|no step.free|no lift|no elevator|not guaranteed)\b", re.I)


def amounts(text):
    return [float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
            for m in MONEY.finditer(text)]


def currency_in(text, currency):
    tokens = CURRENCIES.get(currency, (currency,))
    return any(tok.casefold() in text.casefold() for tok in tokens)


def budget_issue(details, price, currency):
    budget = details["budget"]
    if not budget or budget.casefold().strip() in {"none", "open", "flexible", "not specified", "no budget"}:
        return ""
    numbers = amounts(budget)
    if len(numbers) != 1 or price is None or not currency_in(budget, currency):
        return "budget or currency requires comparison"
    limit = numbers[0]
    nightly = bool(re.search(r"(?:per|/|each|a)\s*night|nightly", budget, re.I))
    per_room = bool(re.search(r"(?:per|/|each|a)\s*room", budget, re.I))
    if nightly:
        nights = (date.fromisoformat(details["check_out"]) - date.fromisoformat(details["check_in"])).days
        limit *= nights
    if per_room:
        limit *= len(details["rooms"])
    if not nightly and not per_room and not re.search(r"total|full stay|entire stay|whole stay|all.in", budget, re.I):
        return "budget basis needs confirmation"
    return "over budget" if price > limit else ""


def distance_km(text):
    match = re.search(r"(\d+(?:\.\d+)?)\s*(km|kilomet(?:res?|ers?)|miles?|mi|met(?:res?|ers?)|m)\b", text, re.I)
    if not match:
        return None
    n = float(match.group(1))
    unit = match.group(2).lower()
    return n * 1.609344 if unit.startswith("mi") else n if unit.startswith("k") else n / 1000


def words(text):
    return set(re.findall(r"[^\W\d_]{3,}", text.casefold())) - {
        "near", "the", "and", "with", "hotel", "hotels", "city", "venue", "india", "district"}


def bedroom_counts(text):
    numbers = "one two three four five six seven eight nine ten".split()
    pattern = r"\b(\d+|" + "|".join(numbers) + r")[ -]+bed[ -]?rooms?\b"
    return {int(n) if n.isdigit() else numbers.index(n) + 1
            for n in re.findall(pattern, text.casefold())}


def validate_quote(raw, provider, details):
    if not isinstance(raw, dict):
        return None
    url = property_url(raw.get("url"), provider)
    name = safe_text(raw.get("hotel"), 120)
    if not url or not name:
        return None
    gaps = []
    def check(condition, missing):
        if not condition:
            gaps.append(missing)
    check(raw.get("check_in") == details["check_in"] and raw.get("check_out") == details["check_out"], "dates")
    check(occupancy(raw.get("rooms")) == occupancy(details["rooms"]), "room-by-room occupancy")
    availability = safe_text(raw.get("availability_evidence"))
    check(bool(availability) and not NEGATIVE.search(availability)
          and bool(re.search(r"availability|available|\d+ (?:rooms?|left)|select (?:a )?room|reserve|book now",
                             availability, re.I)), "availability evidence")
    locality = safe_text(raw.get("locality"))
    destination_words = words(details["destination"]) - {"nearby", "close", "within", "from"}
    observed_area = words(locality + " " + safe_text(raw.get("distance")))
    if "bangalore" in observed_area or "bengaluru" in observed_area:
        observed_area.update({"bangalore", "bengaluru"})
    check(bool(destination_words) and destination_words.issubset(observed_area), "destination evidence")
    config = safe_text(raw.get("room_configuration"))
    check(bool(config), "room configuration")
    price = raw.get("total")
    currency = safe_text(raw.get("currency"), 8)
    valid_price = (type(price) in (float, int) and 0 < price < 1e12 and math.isfinite(price)
                   and re.fullmatch("[A-Z]{3}", currency) and raw.get("full_stay_total") is True
                   and safe_text(raw.get("total_evidence")))
    evidence = safe_text(raw.get("total_evidence"))
    if valid_price:
        explicit_currency = currency_in(evidence, currency) or (
            currency == "USD" and "$" in evidence and not any(x in evidence for x in ("AU$", "CA$", "NZ$", "S$")))
        valid_price = (explicit_currency and price in amounts(evidence)
                       and not re.search(r"\bfrom\b|per night|/night|nightly|per room", evidence, re.I)
                       and bool(re.search(r"total|full stay|entire stay|all nights", evidence, re.I)))
    if not valid_price:
        price = None
    check(price is not None, "full-stay total")
    taxes = safe_text(raw.get("taxes"), 120)
    check(raw.get("taxes_included") is True and taxes and not re.search(
        r"exclud|extra|additional|payable|not included|unknown|unclear|unconfirmed", taxes, re.I), "mandatory taxes and fees")
    budget_gap = budget_issue(details, price, currency)
    if budget_gap:
        gaps.append(budget_gap)
    preferences = safe_text(raw.get("requirements_evidence"))
    check(raw.get("requirements_met") is True, "requested criteria or budget")
    required = details["requirements"]
    if required.casefold().strip() in {"none", "none specified", "not specified", "no requirements",
                                             "no special requirements", "no special requests"}:
        required = ""
    if required:
        requested_bedrooms = bedroom_counts(required)
        if requested_bedrooms and not requested_bedrooms.issubset(bedroom_counts(preferences + " " + config)):
            gaps.append("requested bedroom count")
        check(bool(preferences) and not re.search(r"\bno\s+(?!charge|fee)|\bnot\s+|\black", preferences, re.I)
              and not NEGATIVE.search(preferences), "must-have evidence")
        for constraint in re.split(r"\band\b|[,;]", required, flags=re.I):
            terms = words(constraint)
            if terms and not terms & words(preferences + " " + config + " " + locality + " " + safe_text(raw.get("distance"))):
                gaps.append("confirm " + safe_text(constraint, 70))
    observed_distance = safe_text(raw.get("distance"), 140)
    max_km = distance_km(required)
    actual_km = distance_km(observed_distance)
    if max_km is not None and (actual_km is None or actual_km > max_km):
        gaps.append("distance constraint")

    other = safe_text(raw.get("gaps"), 160)
    if other and other.casefold() not in {"none", "none.", "n/a", "no gaps"}:
        gaps.append(other)
    return {"hotel": name, "url": url, "price": price, "currency": currency, "taxes": taxes,
            "configuration": config, "availability": availability, "criteria": preferences,
            "distance": observed_distance, "km": actual_km, "locality": locality, "gaps": gaps, "provider": provider}


def report(db, request_id):
    row = db.execute("SELECT body FROM stay_reports WHERE request_id=?", (request_id,)).fetchone()
    return row[0] if row else None


def publish(db, request_id, admins):
    if report(db, request_id):
        return
    jobs = db.execute("SELECT provider,status,quotes,note,checked FROM stay_jobs WHERE request_id=?",
                      (request_id,)).fetchall()
    if any(status != "done" for _, status, *_ in jobs):
        return
    details = json.loads(db.execute("SELECT details FROM stay_requests WHERE id=?", (request_id,)).fetchone()[0])
    options = []
    notes = []
    for provider, _, packed, note, checked in jobs:
        candidates = json.loads(packed or "[]")
        timestamp = datetime.fromtimestamp(checked, timezone.utc).strftime("%d %b %H:%M UTC")
        for q in candidates:
            q["checked"] = timestamp
            options.append(q)
        if note:
            notes.append(f"{PROVIDERS.get(provider, provider)} · {timestamp}: {note}")
        if not candidates and provider in PROVIDERS:
            url = search_url(provider, details)
            if len(url) <= 1400:
                notes.extend([f"Manual {PROVIDERS[provider]} search · check each room allocation and price", url])
    # Only compare prices within a currency. Unknown totals sort last.
    options.sort(key=lambda q: (bool(q["gaps"]), len(q["gaps"]), q["currency"], q["price"] or math.inf,
                                q["km"] if q["km"] is not None else math.inf))
    lines = [f"Hotel search #{request_id} · organizer review"]
    if not options:
        lines.append("No source-backed hotel quote was available for this request.")
    for q in options[:4]:
        label = "Partial match" if q["gaps"] else "Source-reported match"
        total = f"{q['currency']} {q['price']:,.2f}" if q["price"] is not None else "unconfirmed"
        lines.extend([f"\n{q['hotel']} · {PROVIDERS[q['provider']]} · {label}",
                      q["locality"] or "Location unconfirmed", q["configuration"] or "Room setup unconfirmed",
                      f"All rooms and nights: {total}. Taxes/fees: {q['taxes'] or 'unconfirmed'}",
                      f"Source evidence: {q['availability'] or 'unconfirmed'}"])
        if q["criteria"]:
            lines.append(q["criteria"])
        if q["distance"]:
            lines.append(f"Distance: {q['distance']}")
        if q["gaps"]:
            lines.append("Missing or unmet: " + ", ".join(q["gaps"]))
        lines.extend([f"Checked {q['checked']}", q["url"]])
    lines.extend(notes)
    body = "\n".join(lines)
    db.execute("INSERT INTO stay_reports VALUES(?,?)", (request_id, body))
    # Keep property links intact across Telegram message boundaries.
    chunks = [""]
    for line in lines:
        if len(chunks[-1]) + len(line) > 1800:
            chunks.append("")
        chunks[-1] += line + "\n"
    for admin in admins:
        db.executemany("INSERT INTO replies(role,chat,body) VALUES('organizer',?,?)",
                       [(admin, part.rstrip()) for part in chunks if part.strip()])


def browser_search(endpoint, provider, details, url, schema):
    from hotel_browser_agent import search
    return search(endpoint, provider, details, url, schema)


def release_session(db, session_id):
    """Delete idempotently; an unavailable API leaves a durable cleanup task."""
    try:
        tiny_browser.close(session_id)
    except Exception as exc:
        logging.warning("TinyBrowser release failed (%s)", type(exc).__name__)
        with db:
            db.execute("UPDATE stay_browser_cleanup SET due=? WHERE session_id=?",
                       (int(time.time()) + 30, session_id))
    else:
        with db:
            db.execute("DELETE FROM stay_browser_cleanup WHERE session_id=?", (session_id,))


def cleanup_due(db, now):
    with db:
        db.execute("BEGIN IMMEDIATE")
        # The server-side inactivity TTL independently expires orphaned sessions.
        db.execute("DELETE FROM stay_browser_cleanup WHERE expires<?", (now,))
        row = db.execute("SELECT session_id FROM stay_browser_cleanup WHERE due<=? ORDER BY due LIMIT 1",
                         (now,)).fetchone()
        if row:
            db.execute("UPDATE stay_browser_cleanup SET due=? WHERE session_id=?", (now + LEASE, row[0]))
    if row:
        release_session(db, row[0])
    return bool(row)


def validated_result(output, provider, details):
    if not isinstance(output, dict) or not isinstance(output.get("quotes"), list):
        raise ValueError("Invalid quote result")
    quotes = []
    for raw in output["quotes"][:3]:
        candidate = validate_quote(raw, provider, details)
        if candidate and all(q["url"] != candidate["url"] for q in quotes):
            quotes.append(candidate)
    note = safe_text(output.get("note")) or ("" if quotes else "No usable property quote returned.")
    return quotes, note


def do_browser_job(db, request_id, provider, lease, details):
    session_id = None
    try:
        session_id, endpoint = tiny_browser.create()
        with db:
            deadline = int(time.time()) + BROWSER_LEASE
            db.execute("INSERT INTO stay_browser_cleanup VALUES(?,?,?)",
                       (session_id, deadline, deadline + 600))
            saved = db.execute("""UPDATE stay_jobs SET status='browsing',run_id=?,next_at=?
                WHERE request_id=? AND provider=? AND lease=? AND status='creating'""",
                (session_id, deadline, request_id, provider, lease))
            if not saved.rowcount:
                raise RuntimeError("Browser work lease has expired")
        # The capability URL is deliberately kept out of SQLite, Telegram and logs.
        output = browser_search(endpoint, provider, details, search_url(provider, details), SCHEMA)
        return validated_result(output, provider, details)
    finally:
        if session_id:
            release_session(db, session_id)


def step(db, admins):
    now = int(time.time())
    if cleanup_due(db, now):
        return True
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT j.request_id,j.provider,j.status,j.run_id,j.started,j.marker,r.details,
            j.created_at,j.engine,j.attempts FROM stay_jobs j JOIN stay_requests r ON r.id=j.request_id
            WHERE j.status!='done' AND j.next_at<=? ORDER BY j.next_at,j.created_at,j.request_id,j.provider LIMIT 1""",
                         (now,)).fetchone()
        if not row:
            return False
        request_id, provider, status, run_id, started, marker, packed, created, engine, attempts = row
        lease = secrets.token_hex(10)
        duration = BROWSER_LEASE if engine == "tinybrowser" else LEASE
        new_status = "creating" if status == "queued" and engine == "tinybrowser" else status
        db.execute("""UPDATE stay_jobs SET status=?,next_at=?,lease=?,started=?,attempts=attempts+?
            WHERE request_id=? AND provider=?""",
            (new_status, now + duration, lease, started or now, int(status == "queued"),
             request_id, provider))
    next_status, quotes, note, delay = "done", [], "", POLL
    checked = now
    try:
        details = json.loads(packed)
        if now - (started or now) > DEADLINE or now - created > 86400:
            note = "Search timed out. Availability is unconfirmed."
        elif not os.getenv("TINYFISH_API_KEY", "").strip():
            note = "TinyFish API key is missing."
        elif status == "queued" and date.fromisoformat(details["check_in"]) < datetime.now(ZoneInfo("Asia/Kolkata")).date():
            note = "The requested check-in date has passed."
        elif engine == "tinybrowser":
            if status == "queued":
                quotes, note = do_browser_job(db, request_id, provider, lease, details)
                checked = int(time.time())
            elif status == "browsing" and run_id and attempts < 2:
                # After process death the saved CDP URL is unavailable. The previous session's
                # cleanup was persisted before navigation. Allow one fresh read-only attempt.
                next_status = "queued"
            elif status == "creating":
                note = "Browser startup was interrupted. The original session will expire automatically."
            else:
                note = "The browser search was interrupted. Availability is unconfirmed."
        elif status == "starting" and marker:
            matches = legacy_get("/runs", params={"goal": marker, "limit": 5})
            ids = [r["run_id"] for r in matches["data"] if marker in str(r.get("goal", ""))]
            if len(ids) == 1:
                run_id, next_status = ids[0], "running"
            elif len(ids) == 0 and now - (started or now) < 180:
                next_status = "starting"
            else:
                note = "Could not identify the previous TinyAgent run."
        elif status == "running" and isinstance(run_id, str) and re.fullmatch(r"[\w-]{1,128}", run_id):
            result = legacy_get("/runs/" + run_id, params={"screenshots": "none", "html": "none"})
            state = result["status"]
            if state in ("PENDING", "RUNNING"):
                next_status = "running"
            elif state == "COMPLETED":
                quotes, note = validated_result(result["result"], provider, details)
                if result.get("finished_at"):
                    try:
                        finished = datetime.fromisoformat(result["finished_at"].replace("Z", "+00:00"))
                        if finished.tzinfo and started and started - 60 <= finished.timestamp() <= now + 60:
                            checked = int(finished.timestamp())
                    except (ValueError, TypeError, AttributeError):
                        pass
            else:
                note = f"Previous TinyAgent run ended with {safe_text(state) or 'unknown status'}."
        else:
            note = "Previous search state could not be recovered."
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if engine == "tinyagent" and (code == 429 or code >= 500):
            next_status = status
        elif engine == "tinybrowser" and status == "queued" and code == 429 and attempts < 1:
            next_status = "queued"
        else:
            note = f"Search service returned HTTP {code}. Availability is unconfirmed."
        retry = exc.response.headers.get("Retry-After", "") if exc.response is not None else ""
        delay = min(DEADLINE, max(60, int(retry))) if retry.isdigit() and len(retry) < 6 else 60
    except Exception as exc:
        # Provider/Playwright exceptions can contain the CDP auth URL; never log the message.
        logging.warning("Hotel lookup failed (%s)", type(exc).__name__)
        if engine == "tinyagent" and isinstance(exc, requests.RequestException):
            next_status = status
        else:
            note = "The browser search could not be verified. Availability is unconfirmed."
    with db:
        updated = db.execute("""UPDATE stay_jobs SET status=?,run_id=COALESCE(?,run_id),next_at=?,quotes=?,note=?,checked=?
            WHERE request_id=? AND provider=? AND lease=? AND status!='done'""",
            (next_status, run_id if engine == "tinyagent" else None, int(time.time()) + delay,
             json.dumps(quotes), note, checked, request_id, provider, lease))
        if updated.rowcount and next_status == "done":
            publish(db, request_id, admins)
    return True


def worker(database, admins, stop):
    with sqlite3.connect(database, timeout=15) as db:
        while not stop.is_set():
            try:
                if not step(db, admins):
                    stop.wait(2)
            except (sqlite3.Error, OSError, ValueError) as exc:
                logging.warning("Hotel search worker: %s", type(exc).__name__)
                stop.wait(3)
