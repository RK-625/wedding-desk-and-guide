"""Provider contract, quota-safe restart and evidence checks without live hotel traffic."""
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch

import requests
import hotels_worker as worker


DETAILS = {"destination": "Whitefield, Bengaluru", "check_in": "2099-12-12", "check_out": "2099-12-14",
           "rooms": [{"adults": 2, "child_ages": [4]}, {"adults": 1, "child_ages": []}],
           "budget": "INR 12,000 total", "requirements": "step-free access"}
QUOTE = {"hotel": "Example Whitefield", "url": "https://www.booking.com/hotel/in/example.en-gb.html",
         "check_in": DETAILS["check_in"], "check_out": DETAILS["check_out"], "rooms": DETAILS["rooms"],
         "currency": "INR", "room_configuration": "Two double rooms", "taxes_included": True,
         "taxes": "All taxes and fees included", "availability_evidence": "2 double rooms available",
         "requirements_evidence": "Step-free access in both rooms", "distance": "2 km from the venue",
         "requirements_met": True, "total": 8999, "full_stay_total": True,
         "total_evidence": "INR 8,999 total for all rooms and nights", "gaps": "", "locality": "Whitefield, Bengaluru"}


class QuoteTest(unittest.TestCase):
    def test_complete_and_mismatches(self):
        self.assertEqual(worker.validate_quote(QUOTE, "booking", DETAILS)["gaps"], [])
        for changes, gap in [
            ({"total": 12001, "total_evidence": "INR 12,001 total"}, "over budget"),
            ({"taxes": "Taxes excluded"}, "mandatory taxes and fees"),
            ({"check_in": "2099-12-13"}, "dates"),
            ({"total_evidence": "From INR 8,999 per night"}, "full-stay total"),
            ({"currency": "USD", "total_evidence": "INR 8,999 full stay"}, "full-stay total"),
            ({"total_evidence": "INR 998 total"}, "full-stay total"),
            ({"total_evidence": "INR 8,999 total", "total": True}, "full-stay total"),
            ({"availability_evidence": "No rooms available"}, "availability evidence"),
            ({"requirements_evidence": "No step-free access"}, "must-have evidence"),
            ({"locality": "Mumbai"}, "destination evidence"),
            ({"locality": "Bengaluru airport"}, "destination evidence"),
            ({"rooms": [{"adults": 3, "child_ages": [4]}]}, "room-by-room occupancy"),
            ({"rooms": [{"adults": True, "child_ages": []}]}, "room-by-room occupancy"),
        ]:
            with self.subTest(changes=changes):
                candidate = worker.validate_quote(dict(QUOTE, **changes), "booking", DETAILS)
                self.assertIn(gap, candidate["gaps"])

    def test_legacy_empty_requirements_and_bedrooms(self):
        for empty in ("None specified", "No special requirements", "no special requests"):
            without = dict(DETAILS, requirements=empty)
            self.assertEqual(worker.validate_quote(dict(QUOTE, requirements_evidence=""), "booking", without)["gaps"], [])

        request = dict(DETAILS, requirements="3 bedrooms")
        actual = dict(QUOTE, room_configuration="Two twin bedrooms", requirements_evidence="Only 2 bedrooms")
        self.assertIn("requested bedroom count", worker.validate_quote(actual, "booking", request)["gaps"])
        request["requirements"] = "3-bedroom suite"
        actual["requirements_evidence"] = "Three-bedroom suite"
        self.assertNotIn("requested bedroom count", worker.validate_quote(actual, "booking", request)["gaps"])

    def test_source_links(self):
        for url in ("https://www.booking.com.evil.com/hotel/in/a.html", "https://www.booking.com@evil.com/a",
                    "https://www.booking.com/searchresults.html?x=1", "http://booking.com/hotel/in/a.html",
                    "https://www.expedia.com/Whitefield.h123.Hotel-Information", "https://[broken"):
            self.assertIsNone(worker.validate_quote(dict(QUOTE, url=url), "booking", DETAILS))
        self.assertTrue(worker.property_url("https://www.expedia.com/Place.h123.Hotel-Information", "expedia"))

    def test_budget_and_distance(self):
        for budget, price, expected in [
            ("INR 3000 per room per night", 12000, ""),
            ("INR5000 total", 5001, "over budget"),
            ("INR 3000 per room per night", 12001, "over budget"),
            ("USD 400 total", 20, "budget or currency requires comparison"),
            ("INR 12,000", 20, "budget basis needs confirmation"),
        ]:
            self.assertEqual(worker.budget_issue(dict(DETAILS, budget=budget), price, "INR"), expected)
        details = dict(DETAILS, requirements="within 500 m")
        self.assertIn("distance constraint", worker.validate_quote(QUOTE, "booking", details)["gaps"])


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
          CREATE TABLE stay_requests(id INTEGER PRIMARY KEY,guest INTEGER,details TEXT);
          CREATE TABLE replies(id INTEGER PRIMARY KEY,role TEXT,chat INTEGER,body TEXT);
        """)
        worker.setup(self.db)
        with self.db:
            self.db.execute("INSERT INTO stay_requests VALUES(1,777,?)", (json.dumps(DETAILS),))
            self.db.executemany("INSERT INTO stay_jobs(request_id,provider,created_at) VALUES(1,?,?)",
                                [(p, int(time.time())) for p in worker.PROVIDERS])
        key = patch.dict(os.environ, TINYFISH_API_KEY="test")
        key.start()
        self.addCleanup(key.stop)
        http = patch.object(worker, "api")
        self.http = http.start()
        self.addCleanup(http.stop)

    def due(self):
        with self.db:
            self.db.execute("UPDATE stay_jobs SET next_at=0")

    def run_step(self):
        return worker.step(self.db, {101})

    def test_partial_provider_and_delivery_once(self):
        exp = dict(QUOTE, hotel="Second Example", url="https://www.expedia.com/Place.h123.Hotel-Information",
                   full_stay_total=False)
        self.http.side_effect = [{"run_id": "book-1"}, {"run_id": "exp-2"},
             {"status": "COMPLETED", "result": {"quotes": [QUOTE]}},
             {"status": "COMPLETED", "result": {"quotes": [exp]}}]
        self.run_step()
        self.run_step()
        post = self.http.call_args_list[0]
        self.assertEqual(post.args, ("POST", "/automation/run-async"))
        payload = post.kwargs["json"]
        self.assertFalse(payload["use_vault"])
        self.assertNotIn("777", payload["goal"])
        self.assertIn("group_children=1", payload["url"])
        self.due()
        self.run_step()
        self.assertIsNone(worker.report(self.db, 1))
        self.run_step()
        report = worker.report(self.db, 1)
        self.assertLess(report.index("Source-reported match"), report.index("Partial match"))
        self.assertIn("INR 8,999.00", report)
        self.assertIn("full-stay total", report)
        self.assertEqual(self.db.execute("SELECT DISTINCT role,chat FROM replies").fetchall(), [("organizer", 101)])
        delivered = list(self.db.execute("SELECT body FROM replies"))
        self.assertFalse(self.run_step())
        self.assertEqual(delivered, self.db.execute("SELECT body FROM replies").fetchall())

    def test_interrupted_post_reconciles_before_poll(self):
        self.http.side_effect = requests.Timeout()
        self.run_step()
        self.assertEqual(self.db.execute("SELECT status FROM stay_jobs WHERE provider='booking'").fetchone()[0],
                         "starting")
        marker = self.db.execute("SELECT marker FROM stay_jobs WHERE provider='booking'").fetchone()[0]
        self.due()
        self.http.side_effect = None
        self.http.return_value = {"data": [{"run_id": "original", "goal": "Task " + marker}]}
        self.run_step()
        self.assertEqual(self.http.call_args.args, ("GET", "/runs"))
        self.assertEqual(self.db.execute("SELECT run_id FROM stay_jobs WHERE provider='booking'").fetchone()[0],
                         "original")

    def test_missing_key_and_expired_search(self):
        with patch.dict(os.environ, TINYFISH_API_KEY=""):
            self.run_step()
        with self.db:
            self.db.execute("UPDATE stay_jobs SET started=? WHERE provider='expedia'", (int(time.time())-3600,))
        self.run_step()
        self.http.assert_not_called()
        report = worker.report(self.db, 1)
        self.assertIn("key is missing", report)
        self.assertIn("timed out", report)
        self.assertIn("No source-backed", report)
        self.assertIn("Manual Booking.com search", report)
        self.assertIn("checkin=2099-12-12", report)
        self.assertIn("group_children=1", report)
        self.assertIn("Manual Expedia search", report)
        self.assertIn("endDate=2099-12-14", report)
        self.assertIn("UTC", report)

    def test_transient_poll_does_not_restart_submit(self):
        with self.db:
            self.db.execute("UPDATE stay_jobs SET status='running',run_id='run_abc' WHERE provider='booking'")
        error = requests.Response()
        error.status_code = 503
        self.http.side_effect = requests.HTTPError(response=error)
        self.run_step()
        self.assertEqual(self.http.call_args.args, ("GET", "/runs/run_abc"))
        self.assertEqual(self.db.execute("SELECT status,run_id FROM stay_jobs WHERE provider='booking'").fetchone(),
                         ("running", "run_abc"))
