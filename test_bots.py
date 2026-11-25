"""Exercise both real SDK clients with an in-process Telegram API fixture."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from requests import Response, ConnectionError
from telebot import TeleBot, apihelper
from bots import setup, process_updates
import hotels_worker
from test_hotels_worker import DETAILS, QUOTE


class WeddingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "wedding.db"
        self.db = sqlite3.connect(self.path)
        setup(self.db)
        self.addCleanup(lambda: self.db.close())
        self.clients = {r: TeleBot(f"{i}:testing", threaded=False)
                        for i, r in enumerate(("organizer", "guest"), 1)}
        self.messages = {}
        self.sent = []
        self.offline = False
        sender = patch.object(apihelper, "CUSTOM_REQUEST_SENDER", self.telegram)
        sender.start()
        self.addCleanup(sender.stop)

    def telegram(self, method, url, params, **kwargs):
        if url.endswith("/getUpdates"):
            result = [u for u in self.messages[self.role] if u["update_id"] >= int(params.get("offset", 0))]
        elif url.endswith("/sendMessage"):
            if self.offline:
                raise ConnectionError("simulated")
            self.sent.append(params["text"])
            result = {"message_id": 1, "date": 1, "chat": {"id": 1, "type": "private"}, "text": params["text"]}
        else:
            self.fail("Unexpected API method")
        response = Response()
        response.status_code = 200
        response._content = json.dumps({"ok": True, "result": result}).encode()
        return response

    def send(self, role, text, uid=None, chat_type="private", voice=None):
        self.role = role
        updates = self.messages.setdefault(role, [])
        uid = uid or (10 if role == "organizer" else 20)
        updates.append({"update_id": len(updates)+1, "message": {
            "message_id": 1, "date": 1, "chat": {"id": uid, "type": chat_type},
            "from": {"id": uid, "first_name": "Test", "is_bot": False}, "text": text,
            **({"voice": voice} if voice else {})}})
        self.sent.clear()
        process_updates(self.db, role, self.clients[role], {10}, "wedding_test_bot")
        return "\n".join(self.sent)

    def test_wedding(self):
        org = lambda cmd: self.send("organizer", cmd)
        guest = lambda cmd: self.send("guest", cmd)
        self.assertIn("organizers", self.send("organizer", "/event bad", 20))
        self.assertIn("invitation", guest("/schedule"))
        self.assertIn("Use /event", org("/event bad"))
        org("/event 2099-01-01 17:00 | Sangeet | Lotus Hall")
        org("/booking 2099-01-01 12:00 | Caterer meeting | Private venue")
        invite = org("/invite").split("?start=")[1]
        self.assertIn("invitation", guest("/start wrong"))
        self.assertIn("Welcome", guest("/start " + invite))
        self.assertIn("Private venue", org("/bookings"))
        self.assertNotIn("Private venue", guest("/schedule"))
        self.assertIn("Lotus Hall", guest("Where is the sangeet?"))
        self.assertIn("Sangeet", guest("What's next?"))
        org("/faq parking | East gate")
        self.assertIn("East gate", guest("Where is parking?"))
        org("/faq parking | West gate")
        self.assertIn("West gate", guest("/faqs"))
        org("/unfaq parking")
        self.assertNotIn("West gate", guest("Where is parking?"))
        guest("/rsvp 3 2")
        guest("/rsvp 2 1")
        self.assertIn("Use /rsvp", guest("/rsvp 1 2"))
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.assertIn("2 guests, 1 vegetarian", guest("/myrsvp"))
        self.assertIn("2 guests · 1 vegetarian", org("/attendance"))
        self.assertEqual("", self.send("organizer", "/schedule", chat_type="group"))
        org("/remove 1")
        self.assertIn("No entries", guest("/schedule"))

    @patch.dict("os.environ", TINYFISH_API_KEY="test")
    def test_family_hotel_request_and_organizer_report(self):
        def guest(text):
            return self.send("guest", text)

        def desk(text):
            return self.send("organizer", text)

        self.assertIn("No guest hotel requests", desk("still searching ???"))
        self.assertIn("invitation", guest("We need a hotel"))
        guest("/start " + desk("/invite").split("?start=")[1])
        with patch("stays.extract", return_value=dict(DETAILS, intent=True, clarify=None)):
            self.assertIn("Whitefield", guest("Find a hotel for our family"))
        self.assertEqual(self.db.execute("select count(*) from stay_jobs").fetchone()[0], 0)
        with patch("stays.flag_enabled", return_value=False):
            self.assertIn("searches are paused", guest("search"))
            self.assertEqual(self.db.execute("SELECT count(*) FROM stay_jobs").fetchone()[0], 0)
        self.assertIn("#1 sent", guest("search"))
        self.assertIn("Guest hotel request #1", desk("/stays"))
        with patch("organizer.chat", side_effect=AssertionError("Status should come from SQLite")):
            self.assertIn("Booking.com: queued", desk("still searching ???"))
            self.assertIn("Expedia: queued", desk("What’s the status of hotel request #1?"))
            self.assertIn("No hotel request", desk("Status of hotel request #2"))
        with patch("tiny_browser.create", side_effect=[("br-a", "wss://a"), ("br-b", "wss://b")]), \
                patch("tiny_browser.close") as close, patch("hotels_worker.browser_search", side_effect=[
                    {"quotes": [QUOTE], "note": ""}, {"quotes": [], "note": "Provider blocked."}]):
            hotels_worker.step(self.db, {10})
            self.db.close()
            self.db = sqlite3.connect(self.path)
            hotels_worker.step(self.db, {10})
            self.assertEqual(close.call_count, 2)
        result = desk("/staystatus 1")
        self.assertIn("8,999.00", result)
        self.assertIn("Expedia", result)
        self.assertIn(QUOTE["url"], result)
        self.assertEqual(desk("still searching?"), desk("/staystatus 1"))
        self.assertNotIn(QUOTE["url"], self.send("organizer", "/staystatus 1", uid=20))
        self.assertNotIn(QUOTE["url"], guest("/staystatus 1"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM stay_reports").fetchone()[0], 1)

    @patch.dict("os.environ", SARVAM_API_KEY="test")
    def test_guest_voice_note_requires_invitation_flag_and_typed_confirmation(self):
        voice = {"duration": 10, "file_id": "clip", "file_unique_id": "unique"}
        bot = self.clients["guest"]
        with patch("sponsor_services.flag_enabled", return_value=False) as enabled, \
                patch.object(bot, "get_file") as get_file, patch.object(bot, "download_file", return_value=b"audio"), \
                patch("sponsor_services.transcribe", side_effect=["A hotel in Whitefield for our family", "Search."]), \
                patch("stays.extract", return_value=dict(DETAILS, intent=True, clarify=None)):
            self.assertIn("invitation", self.send("guest", None, voice=voice))
            invite = self.send("organizer", "/invite").split("?start=")[1]
            self.send("guest", "/start " + invite)
            self.assertIn("Voice notes are unavailable", self.send("guest", None, voice=voice))
            get_file.assert_not_called()
            enabled.return_value = True
            result = self.send("guest", None, voice=voice)
            self.assertIn("I heard: A hotel", result)
            self.assertIn("Reply “search”", result)
            self.assertIn("Please type", self.send("guest", None, voice=voice))
            self.assertEqual(self.db.execute("SELECT count(*) FROM stay_jobs").fetchone()[0], 0)

    def test_delivery_retry(self):
        self.offline = True
        with self.assertRaises(ConnectionError):
            self.send("organizer", "/event 2099-01-01 10:00 | Lunch | Hall")
        self.offline = False
        answer = self.send("organizer", "/schedule")
        self.assertIn("Saved #1", answer)
        self.assertEqual(answer.count("Lunch"), 1)
