import unittest
from unittest.mock import patch
import requests

import tiny_browser


class TinyBrowserTest(unittest.TestCase):
    @patch.dict("os.environ", TINYFISH_API_KEY="unit-key")
    @patch("tiny_browser.requests.post")
    def test_create_contract(self, post):
        post.return_value.status_code = 201
        post.return_value.json.return_value = {"session_id": "br-test", "cdp_url": "wss://session.example/cdp"}
        self.assertEqual(tiny_browser.create(), ("br-test", "wss://session.example/cdp"))
        args, kw = post.call_args
        self.assertEqual(args, ("https://api.browser.tinyfish.ai",))
        self.assertEqual(kw["headers"], {"X-API-Key": "unit-key"})
        self.assertTrue(5 <= kw["json"]["timeout_seconds"] <= 300)
        self.assertFalse(kw["allow_redirects"])

    @patch.dict("os.environ", TINYFISH_API_KEY="unit-key")
    @patch("tiny_browser.requests.delete")
    @patch("tiny_browser.requests.post")
    def test_invalid_endpoint_closes_session(self, post, delete):
        post.return_value.status_code = 201
        post.return_value.json.return_value = {"session_id": "br-example", "cdp_url": ""}
        delete.return_value.status_code = 204
        with self.assertRaises(ValueError):
            tiny_browser.create()
        self.assertEqual(delete.call_args.args[0], "https://api.browser.tinyfish.ai/br-example")
        with self.assertRaises(ValueError):
            tiny_browser.close("../unexpected")

    @patch.dict("os.environ", TINYFISH_API_KEY="unit-key")
    @patch("tiny_browser.requests.delete")
    def test_idempotent_close_and_server_failure(self, delete):
        delete.return_value.status_code = 204
        tiny_browser.close("br-example")
        delete.return_value.status_code = 503
        delete.return_value.raise_for_status.side_effect = requests.HTTPError("service unavailable")
        with self.assertRaises(requests.HTTPError):
            tiny_browser.close("br-example")
