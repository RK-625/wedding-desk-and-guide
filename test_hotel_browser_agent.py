import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from playwright.sync_api import TimeoutError as NavigationTimeout, Error as BrowserError

import hotel_browser_agent as agent
from hotels_worker import SCHEMA, validate_quote

SEARCH = "https://www.booking.com/searchresults.html?ss=Whitefield"
PROPERTY = "https://www.booking.com/hotel/in/maple-lodge.html"
SPEC = dict(destination="Whitefield", check_in="2099-12-12", check_out="2099-12-14",
            rooms=[dict(adults=2, child_ages=[])], budget="", requirements="")
BODY = ("Maple Lodge in Whitefield. December 12, 2099 - December 14, 2099. "
        "Room 1: 2 adults, no children. Double room available. INR 8,000 total for full stay. "
        "All taxes and fees included.")
QUOTE = dict(hotel="Maple Lodge", check_in="2099-12-12", check_out="2099-12-14",
             rooms=SPEC["rooms"], room_configuration="Double room", currency="INR",
             total=8000, taxes="All taxes and fees included", taxes_included=True,
             availability_evidence="Double room available", total_evidence="INR 8,000 total for full stay",
             date_evidence="December 12, 2099 - December 14, 2099",
             occupancy_evidence="Room 1: 2 adults, no children",
             requirements_met=True, requirements_evidence="", distance="", locality="Whitefield",
             gaps="", full_stay_total=True)


def turn(name, args):
    call = SimpleNamespace(id=f"id-{name}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
    msg = SimpleNamespace(tool_calls=[call], model_dump=lambda **_: {"role":"assistant","tool_calls":[
        {"id":call.id,"type":"function","function":{"name":name,"arguments":json.dumps(args)}}]})
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class BrowserAgentCase(unittest.TestCase):
    def run_search(self, body=BODY, results="Hotel results", status=200, script=None, navigation_error=None, reached=True):
        client = MagicMock()
        client.chat.completions.create.side_effect = script or [
            turn("inspect_property", {"index":0}), turn("record_quote", QUOTE), turn("finish", {})]
        page = MagicMock()
        bodies = {SEARCH:results, PROPERTY:body}
        def go(url, **kwargs):
            page.url = url if reached else "about:blank"
            if navigation_error:
                raise navigation_error
            return SimpleNamespace(status=status)
        page.goto.side_effect = go
        def locator(selector):
            item = MagicMock()
            item.evaluate.side_effect = lambda *args, **kwargs: bodies[page.url]
            item.evaluate_all.return_value = [[PROPERTY, "Maple Lodge"], ["https://elsewhere.test/","Bogus"]]
            return item
        page.locator.side_effect = locator
        engine = MagicMock()
        ctx = MagicMock()
        ctx.new_page.return_value = page
        engine.chromium.connect_over_cdp.return_value.contexts = [ctx]
        with patch("playwright.sync_api.sync_playwright") as pw, patch("hotel_browser_agent.OpenAI") as sdk, \
                patch.dict(os.environ, DEEPSEEK_API_KEY="sensitive-test-secret"):
            pw.return_value.__enter__.return_value=engine
            sdk.return_value.__enter__.return_value=client
            out=agent.search("wss://cdp.test/private-capability","booking",SPEC,SEARCH,SCHEMA)
        self.assertGreater(page.close.call_count,0)
        return out,client,page

    def test_end_to_end_agent_sees_observations_and_selects_only_property(self):
        out,client,page = self.run_search()
        self.assertEqual(out["quotes"][0]["url"], PROPERTY)
        normalized = validate_quote(out["quotes"][0],"booking",SPEC)
        self.assertEqual(normalized["price"],8000)
        self.assertEqual(normalized["gaps"], [])
        self.assertEqual([x.args[0] for x in page.goto.call_args_list], [SEARCH,PROPERTY])
        payload = str(client.chat.completions.create.call_args_list)
        for secret in ("private-capability","sensitive-test-secret","elsewhere.test"):
            self.assertNotIn(secret, payload)

    def test_fabricated_evidence_downgrades(self):
        quote=agent.verify(dict(QUOTE, taxes="made-up taxes", total_evidence="INR 12,000 total",
                                date_evidence="2099-12-12 2099-12-14", occupancy_evidence="4 adults"),PROPERTY,BODY)
        self.assertFalse(quote["taxes_included"])
        self.assertFalse(quote["full_stay_total"])
        self.assertIsNone(quote["total"])
        self.assertEqual(quote["check_in"], "")
        self.assertEqual(quote["rooms"], [])
        self.assertIsNone(agent.verify(dict(QUOTE,hotel="Different Lodge"),PROPERTY,BODY))
        self.assertGreater(len(validate_quote(quote,"booking",SPEC)["gaps"]), 0)

    def test_rejects_unsupported_occupancy(self):
        requested=[{"adults":2,"child_ages":[3,7]},{"adults":1,"child_ages":[]}]
        self.assertFalse(agent.supports_rooms(requested,"3 adults, children ages 3 and 7"))
        self.assertTrue(agent.supports_rooms(requested,"Room 1: 2 adults ages 3 and 7; Room 2: 1 adult"))
        self.assertFalse(agent.supports_rooms(requested,"Room 1: 2 adults ages 3 and 6; Room 2: 1 adult"))
        self.assertFalse(agent.supports_rooms(requested,"Room 1: 1 adult ages 3 and 7; Room 2: 2 adults"))

    def test_exact_room_count_and_no_extra_infants(self):
        rooms=[{"adults":2,"child_ages":[]}]
        for snippet in ("2 adults", "2 rooms, 2 adults", "Room 1: 2 adults. Room 2: 2 adults.",
                        "1 room for 2 adults and 1 infant", "Room 1: 2 adults with baby",
                        "Room 2: 2 adults", "Room 1: 2 adults and 1 child aged 4"):
            with self.subTest(snippet=snippet):
                self.assertFalse(agent.supports_rooms(rooms,snippet))
        for snippet in ("1 room, 2 adults", "Room 1: 2 adults, 0 children"):
            self.assertTrue(agent.supports_rooms(rooms,snippet))
        self.assertFalse(agent.supports_rooms(rooms*2,"Room 1: 2 adults Room 2: 2 adults Room 3: 2 adults"))
        self.assertFalse(agent.supports_rooms([dict(adults=2,child_ages=[2])],
            "Room 1: 2 adults, 1 child aged 2 and 1 infant"))

    def test_blocks_captcha_and_403_before_model(self):
        for args in ({"status":403},{"results":"Verify that you are human"}):
            with self.subTest(args=args):
                out,client,page=self.run_search(**args)
                self.assertFalse(out["quotes"])
                self.assertTrue(out["note"])
                self.assertEqual(client.chat.completions.create.call_count,0)

    def test_redirects_and_generated_destinations_rejected(self):
        for url in ("https://booking.com.evil.test/hotel/in/x.html", "https://www.booking.com/reserve",
                    "https://me@www.booking.com/hotel/in/x.html", "https://www.booking.com:444/hotel/in/x.html",
                    "file:///hotel/in/test.html","https://www.booking.com/confirm"):
            self.assertFalse(agent.navigation_allowed(url,"booking"))
        out,client,page=self.run_search(script=[turn("inspect_property",{"index":67,"url":"file:///x"}),
                                               turn("finish",{})])
        self.assertEqual([x.args[0] for x in page.goto.call_args_list],[SEARCH])
        self.assertEqual(out["quotes"],[])

    def test_rendered_documents_recovered_on_navigation_timeout(self):
        out, client, page = self.run_search(navigation_error=NavigationTimeout("wss://secret"))
        self.assertEqual(len(out["quotes"]),1)
        self.assertGreater(client.chat.completions.create.call_count,0)

    def test_timeout_before_destination_and_diagnostics_do_not_disclose_capability(self):
        out, client, page = self.run_search(navigation_error=NavigationTimeout("wss://secret"), reached=False)
        self.assertIn("before the destination loaded", out["note"])
        self.assertNotIn("secret", out["note"])
        self.assertEqual(client.chat.completions.create.call_count,0)
        out, _, _ = self.run_search(navigation_error=BrowserError("wss://secret"))
        self.assertIn("results navigation", out["note"])
        self.assertNotIn("secret", out["note"])

    def test_short_snippets_cannot_remove_material_qualifiers(self):
        raw = dict(QUOTE, availability_evidence="available", occupancy_evidence="Room 1: 2 adults",
                   total_evidence="INR 8,000 total")
        body = ("Maple Lodge. These rooms are not available. Room 1: 2 adults and 1 infant. "
                "From INR 8,000 total per night.")
        verified = agent.verify(raw, PROPERTY, body)
        self.assertEqual(verified["rooms"], [])
        self.assertEqual(verified["availability_evidence"], "")
        self.assertIsNone(verified["total"])
        self.assertEqual(agent.literal("available", "unavailable", "availability_evidence"), "")
