import json
import os
import sqlite3
import unittest
from unittest.mock import patch

import stays


class StayIntakeTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        stays.setup(self.db)
        self.db.executescript("""
            CREATE TABLE replies(id INTEGER PRIMARY KEY, role TEXT, chat INTEGER, body TEXT);
            CREATE TABLE stay_jobs(request_id INTEGER,provider TEXT,status TEXT,created_at INTEGER,
                                   PRIMARY KEY(request_id,provider));
        """)
        self.details = dict(intent=True, destination="Whitefield, Bengaluru", check_in="2099-12-12",
                            check_out="2099-12-14", rooms=[dict(adults=2, child_ages=[4]), dict(adults=1, child_ages=[])],
                            requirements="Two bedrooms and a lift", budget="INR 12,000 total", clarify=None)
        extraction = patch.object(stays, "extract", return_value=self.details)
        self.extract = extraction.start()
        self.addCleanup(extraction.stop)
        env = patch.dict(os.environ, TINYFISH_API_KEY="fake")
        env.start()
        self.addCleanup(env.stop)

    def send(self, text, uid=20, admins=(10,)):
        with self.db:
            return stays.handle(self.db, uid, text, admins)

    def test_multiturn_and_submission(self):
        partial = dict(self.details, rooms=None)
        self.extract.side_effect = [partial, self.details]
        self.assertIn("sleep in each", self.send("We need a hotel near Whitefield"))
        preview = self.send("Two rooms: one for 2 adults and a four-year-old, the other for 1 adult")
        self.assertIn("children aged 4", preview)
        self.assertIn("Two bedrooms and a lift", preview)
        self.assertEqual(self.db.execute("SELECT count(*) FROM stay_jobs").fetchone()[0], 0)
        self.assertIn("Whitefield", str(self.extract.call_args[0][1]))
        self.assertIn("#1 sent", self.send("Yes please"))
        self.assertIn("#1", self.send("search"))
        details = json.loads(self.db.execute("SELECT details FROM stay_requests").fetchone()[0])
        self.assertEqual(details["rooms"], self.details["rooms"])
        self.assertEqual(self.db.execute("SELECT provider FROM stay_jobs ORDER BY provider").fetchall(),
                         [("booking",), ("expedia",)])
        self.assertEqual(self.db.execute("SELECT role,chat FROM replies").fetchall(), [("organizer", 10)])

    def test_cancellation_and_guest_isolation(self):
        self.send("Need a hotel")
        self.assertIsNone(self.send("yes", uid=88))
        self.assertIn("cancelled", self.send("/cancel"))
        self.assertIsNone(self.send("yes"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM stay_jobs").fetchone()[0], 0)

    def test_missing_configuration_preserves_draft(self):
        self.send("Need rooms")
        self.assertIn("unavailable", self.send("search", admins=()))
        with patch.dict(os.environ, TINYFISH_API_KEY=""):
            self.assertIn("unavailable", self.send("search"))
        self.assertIn("#1 sent", self.send("search"))

    def test_invalid_parameters(self):
        for overrides, question in [
            (dict(check_out="2099-12-12"), "later check-out"),
            (dict(check_in="2000-01-01"), "today"),
            (dict(check_out="not a date"), "including the year"),
            (dict(rooms=[dict(adults=True, child_ages=[])]), "one adult"),
            (dict(rooms=[dict(adults=1, child_ages=[18])]), "age of each child"),
            (dict(rooms=[dict(adults=1)]), "age of each child"),
            (dict(destination=None), "Which city"),
            (dict(clarify="Is that budget per night or total?"), "per night"),
        ]:
            with self.subTest(overrides=overrides):
                self.extract.return_value = dict(self.details, **overrides)
                self.assertIn(question, self.send("Need rooms"))
                self.assertEqual(self.db.execute("SELECT ready FROM stay_drafts").fetchone()[0], 0)

    def test_faq_falls_through_and_commands_do_not_modify_draft(self):
        self.extract.return_value = dict(self.details, intent=False)
        self.assertIsNone(self.send("Which room is lunch in?"))
        self.assertIsNone(self.send("Where is sangeet?"))
        self.assertIn("Tell me", self.send("/stay"))
        self.assertIsNone(self.send("/rsvp 2 0"))
        self.assertEqual(self.db.execute("SELECT ready FROM stay_drafts").fetchone()[0], 0)

    def test_extraction_failure_does_not_submit_stale_draft(self):
        self.send("Need rooms")
        self.extract.side_effect = ValueError("invalid")
        self.assertIn("try again", self.send("Actually change the dates"))
        self.send("search")
        self.assertEqual(self.db.execute("SELECT count(*) FROM stay_jobs").fetchone()[0], 0)

    def test_rollback_restores_draft_when_second_provider_write_fails(self):
        self.send("Need hotel rooms")
        self.db.execute("""CREATE TRIGGER fail_expedia BEFORE INSERT ON stay_jobs
            WHEN NEW.provider = 'expedia' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.send("search")
        self.assertEqual(self.db.execute("SELECT ready FROM stay_drafts").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM stay_requests").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM stay_jobs").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM replies").fetchone()[0], 0)
        self.db.execute("DROP TRIGGER fail_expedia")
        self.assertIn("#1 sent", self.send("search"))

    def test_large_organizer_handoff_keeps_guest_reference_out_of_provider_input(self):
        rooms = [dict(adults=4, child_ages=[12, 17, 3, 7])] * 140
        self.extract.return_value = dict(self.details, rooms=rooms, requirements="🚪" * 470)
        guest_id = 123456789
        self.send("Need rooms", uid=guest_id)
        self.send("search", uid=guest_id, admins=[11, 12])
        for admin in (11, 12):
            chunks = [body for (body,) in self.db.execute(
                "SELECT body FROM replies WHERE role='organizer' AND chat=? ORDER BY id", (admin,))]
            self.assertGreater(len(chunks), 1)
            self.assertTrue(all(len(chunk.encode('utf-16-le')) <= 4096 * 2 for chunk in chunks))
            notice = "".join(chunks)
            self.assertIn("Telegram guest ID 123456789", notice)
            self.assertIn("Room 140", notice)
            self.assertIn("🚪" * 470, notice)
        details = self.db.execute("SELECT details FROM stay_requests").fetchone()[0]
        self.assertNotIn(str(guest_id), details)
