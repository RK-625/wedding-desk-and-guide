"""Opt-in: real TinyBrowser and DeepSeek, using controlled HTML instead of vendor inventory."""
import os
import unittest
from unittest.mock import patch
from playwright.sync_api import Page

import hotel_browser_agent
import hotels_worker
import tiny_browser
from test_hotel_browser_agent import BODY, PROPERTY, SEARCH, SPEC


@unittest.skipUnless(os.getenv("LIVE_CDP_FIXTURE") == "1", "Opt-in billed remote CDP integration")
class LiveCDPTest(unittest.TestCase):
    def test_remote_agent_opens_property_and_extracts_stay_total(self):
        from dotenv import load_dotenv
        load_dotenv(os.environ.get("WEDDING_ENV_FILE", ".env"))
        session, endpoint = tiny_browser.create()
        visited = []
        original_route = Page.route

        def route_fixture(page, pattern, production_guard):
            def serve(route):
                url = route.request.url
                if url not in (SEARCH, PROPERTY):
                    return route.abort()
                visited.append(url)
                html = BODY if url == PROPERTY else f'<a href="{PROPERTY}">Maple Lodge in Whitefield</a>'
                route.fulfill(status=200, content_type="text/html", body=f"<html><body>{html}</body></html>")
            return original_route(page, pattern, serve)

        try:
            with patch.object(Page, "route", route_fixture):
                result = hotel_browser_agent.search(endpoint, "booking", SPEC, SEARCH, hotels_worker.SCHEMA)
            self.assertIn(PROPERTY, visited, result["note"])
            self.assertTrue(result["quotes"], result["note"])
            quote = hotels_worker.validate_quote(result["quotes"][0], "booking", SPEC)
            self.assertEqual(quote["price"], 8000)
            self.assertEqual(quote["url"], PROPERTY)
        finally:
            tiny_browser.close(session)
