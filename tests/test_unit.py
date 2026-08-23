"""
Basic unit tests for wolt-mcp internals — no network calls.

Run from the wolt-mcp/ directory:
    .venv/bin/python -m pytest tests/ -v

Or without pytest:
    .venv/bin/python tests/test_unit.py
"""

import asyncio
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import wolt_mcp as W


class TestJWTDecoding(unittest.TestCase):
    """Verify JWT exp claim is decoded correctly."""

    def test_decode_exp_from_real_jwt(self):
        # JWT with payload {"exp": 1777329184}
        token = ("eyJhbGciOiJFUzI1NiJ9."
                 "eyJleHAiOjE3NzczMjkxODR9."
                 "fake_signature_doesnt_matter")
        exp = W._decode_jwt_exp(token)
        self.assertEqual(exp, 1777329184)

    def test_decode_invalid_returns_none(self):
        self.assertIsNone(W._decode_jwt_exp("not.a.jwt"))
        self.assertIsNone(W._decode_jwt_exp(""))
        self.assertIsNone(W._decode_jwt_exp("only_one_segment"))


class TestCookieParsing(unittest.TestCase):
    """Verify telemetry UUIDs are extracted from cookie strings."""

    def test_extract_known_cookies(self):
        cookie = ("foo=bar; "
                  "telemetryDeviceId=56fad4b4-be1b-4271-941c-b0e50d849f3c; "
                  "telemetrySessionId=bd5876ad-0e4f-4d7a-bcd0-bc9f9673ea17; "
                  "baz=qux")
        self.assertEqual(
            W._parse_cookie_value(cookie, "telemetryDeviceId"),
            "56fad4b4-be1b-4271-941c-b0e50d849f3c",
        )
        self.assertEqual(
            W._parse_cookie_value(cookie, "telemetrySessionId"),
            "bd5876ad-0e4f-4d7a-bcd0-bc9f9673ea17",
        )

    def test_missing_cookie_returns_none(self):
        self.assertIsNone(W._parse_cookie_value("a=b; c=d", "missing"))
        self.assertIsNone(W._parse_cookie_value("", "anything"))


class TestMenuNormalization(unittest.TestCase):
    """Verify the modern assortment endpoint response is normalized correctly."""

    def test_assortment_extracts_items(self):
        fake = {
            "categories": [
                {"id": "cat1", "name": "Mains", "item_ids": ["item1"]},
            ],
            "items": [
                {
                    "id": "item1",
                    "name": "Pizza",
                    "description": "Cheesy",
                    "price": 850,
                    "options": ["opt1"],
                    "images": [{"url": "http://example/x.jpg"}],
                },
            ],
            "options": [
                {
                    "id": "opt1",
                    "name": "Size",
                    "type": "choice",
                    "values": [
                        {"id": "v1", "name": "L", "price": 0},
                        {"id": "v2", "name": "XL", "price": 200},
                    ],
                    "default_value": "v1",
                },
            ],
        }
        out = W._normalize_assortment(fake, "test-venue")
        self.assertEqual(out["item_count"], 1)
        self.assertEqual(out["items"][0]["name"], "Pizza")
        self.assertEqual(out["items"][0]["price"], 8.50)
        self.assertEqual(out["items"][0]["category_name"], "Mains")
        self.assertEqual(len(out["items"][0]["options"]), 1)
        self.assertEqual(out["items"][0]["options"][0]["values"][1]["price_delta"], 2.0)


class TestVenueNormalization(unittest.TestCase):
    """Verify the modern venue static endpoint response is normalized."""

    def test_static_venue(self):
        fake = {
            "id": "venue123",
            "name": "Test Venue",
            "currency": "EUR",
            "country": "EST",
            "city": "Tallinn",
            "address": "Some street 1",
            "active_menu": "menu-id-456",
            "rating": {"score": 8.6},
        }
        out = W._normalize_venue_static(fake, "test-venue")
        self.assertEqual(out["id"], "venue123")
        self.assertEqual(out["currency"], "EUR")
        self.assertEqual(out["country"], "est")
        self.assertEqual(out["rating"], 8.6)
        self.assertEqual(out["active_menu_id"], "menu-id-456")


class TestBasketBuilder(unittest.TestCase):
    """Verify _build_basket_items computes prices and option deltas correctly."""

    def test_simple_item(self):
        menu = {
            "i1": {
                "id": "i1",
                "name": "Burger",
                "price": 12.50,
                "options": [],
            }
        }
        items, sub = W._build_basket_items(menu, [{"item_id": "i1", "qty": 2}])
        self.assertEqual(items[0]["price"], 1250)
        self.assertEqual(sub, 25.00)

    def test_with_option_delta(self):
        menu = {
            "i1": {
                "id": "i1",
                "name": "Pizza",
                "price": 10.00,
                "options": [
                    {
                        "id": "o1",
                        "values": [
                            {"id": "v1", "name": "S", "price_delta": 0},
                            {"id": "v2", "name": "L", "price_delta": 4.0},
                        ],
                    }
                ],
            }
        }
        items, sub = W._build_basket_items(
            menu,
            [{"item_id": "i1", "qty": 1,
              "options": [{"option_id": "o1", "value_id": "v2"}]}],
        )
        self.assertEqual(items[0]["price"], 1400)  # 10 + 4 = 14.00 → 1400 cents
        self.assertEqual(sub, 14.00)

    def test_unknown_item_raises(self):
        with self.assertRaises(RuntimeError):
            W._build_basket_items({}, [{"item_id": "missing", "qty": 1}])


class TestDevice(unittest.TestCase):
    """Device UUID loading with cookie integration."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._original = (W.SESSION_FILE, W.DEVICE_FILE, W.CART_DIR)
        W.SESSION_FILE = self._tmp / "session.json"
        W.DEVICE_FILE = self._tmp / "device.json"
        W.CART_DIR = self._tmp

    def tearDown(self):
        W.SESSION_FILE, W.DEVICE_FILE, W.CART_DIR = self._original

    def test_device_uuids_from_cookie(self):
        cookie = ("telemetryDeviceId=abc-123; "
                  "telemetrySessionId=def-456")
        json.dump({"token": cookie, "kind": "cookie", "saved_at": "now"},
                  open(W.SESSION_FILE, "w"))
        d = W._load_device()
        self.assertEqual(d.wolt_client_id, "abc-123")
        self.assertEqual(d.wolt_session_id, "def-456")

    def test_device_generates_when_no_session(self):
        d = W._load_device()
        self.assertTrue(d.wolt_client_id)
        self.assertTrue(d.wolt_session_id)
        # Persisted
        self.assertTrue(W.DEVICE_FILE.exists())


class TestCookieRotation(unittest.TestCase):
    """Cookie-mode self-renewal: merging Set-Cookie rotations, and
    persisting them via WoltClient._maybe_persist_rotated_cookies."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._original = (W.SESSION_FILE, W.DEVICE_FILE, W.CART_DIR)
        W.SESSION_FILE = self._tmp / "session.json"
        W.DEVICE_FILE = self._tmp / "device.json"
        W.CART_DIR = self._tmp

    def tearDown(self):
        W.SESSION_FILE, W.DEVICE_FILE, W.CART_DIR = self._original

    def test_merge_replaces_existing_and_keeps_order(self):
        original = "a=1; __wtoken=old; __wrtoken=refresh1; z=9"
        merged = W._merge_cookie_updates(original, {"__wtoken": "new"})
        self.assertEqual(merged, "a=1; __wtoken=new; __wrtoken=refresh1; z=9")

    def test_merge_appends_unknown_cookie(self):
        merged = W._merge_cookie_updates("a=1", {"b": "2"})
        self.assertEqual(merged, "a=1; b=2")

    def test_merge_noop_when_no_updates(self):
        self.assertEqual(W._merge_cookie_updates("a=1; b=2", {}), "a=1; b=2")

    def test_persist_rotated_cookies_updates_session_file(self):
        class FakeResp:
            def __init__(self, cookies):
                self.cookies = cookies

        sess = W.Session(token="__wtoken=old; __wrtoken=r1", kind="cookie")
        W._save_session(sess)

        fresh = W._load_session()
        W.WoltClient._maybe_persist_rotated_cookies(
            fresh, FakeResp({"__wtoken": "new-value"})
        )
        self.assertEqual(fresh.token, "__wtoken=new-value; __wrtoken=r1")
        self.assertIsNotNone(fresh.last_renewed_at)

        # And the rotation was actually written to disk, not just in-memory.
        reloaded = W._load_session()
        self.assertEqual(reloaded.token, "__wtoken=new-value; __wrtoken=r1")
        self.assertEqual(reloaded.last_renewed_at, fresh.last_renewed_at)

    def test_bearer_mode_sessions_are_never_touched(self):
        class FakeResp:
            def __init__(self, cookies):
                self.cookies = cookies

        sess = W.Session(token="eyJhbGciOiJFUzI1NiJ9.fake.sig", kind="bearer")
        W._save_session(sess)
        fresh = W._load_session()
        W.WoltClient._maybe_persist_rotated_cookies(
            fresh, FakeResp({"__wtoken": "should-be-ignored"})
        )
        self.assertEqual(fresh.token, "eyJhbGciOiJFUzI1NiJ9.fake.sig")
        self.assertIsNone(fresh.last_renewed_at)

    def test_no_rotation_when_response_has_no_cookies(self):
        class FakeResp:
            def __init__(self, cookies):
                self.cookies = cookies

        sess = W.Session(token="__wtoken=old", kind="cookie")
        W._save_session(sess)
        fresh = W._load_session()
        W.WoltClient._maybe_persist_rotated_cookies(fresh, FakeResp({}))
        self.assertEqual(fresh.token, "__wtoken=old")
        self.assertIsNone(fresh.last_renewed_at)


class TestMCPToolRegistration(unittest.TestCase):
    """Verify all expected tools are registered with the MCP server."""

    def test_all_tools_present(self):
        async def get_tools():
            return await W.mcp.list_tools()

        tools = asyncio.run(get_tools())
        names = {t.name for t in tools}

        expected = {
            # v0.1
            "search_venues", "get_venue", "get_venue_menu", "find_items",
            "add_to_cart", "view_cart", "remove_from_cart", "clear_cart",
            "get_deeplink",
            # v0.2-v0.4
            "set_session", "get_session_status", "get_my_orders",
            "get_wolt_baskets", "get_payment_methods",
            # v0.3
            "set_delivery_address", "get_delivery_address",
            "list_delivery_addresses", "set_default_payment_method",
            # v0.5
            "sync_basket_to_wolt", "prepare_wolt_checkout", "get_checkout_link",
            # v0.7
            "get_favorites", "sync_multi_venue_baskets",
            "search_items_global", "get_wolt_plus_status",
            "refresh_session", "get_audit_log",
        }
        missing = expected - names
        self.assertFalse(missing, f"Missing tools: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
