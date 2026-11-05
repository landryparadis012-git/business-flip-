"""Regression tests for the Flip Broker discovery -> pricing -> card chain.

These run with no network and no secrets: Telegram and hub emission are
monkeypatched, and Apify payloads are fed in as realistic actor items.
"""
import importlib.util, json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location("flip_broker", os.path.join(ROOT, "flip_broker.py"))
fb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fb)

COMPS = json.load(open(os.path.join(ROOT, "comps.json")))["comps"]
WATCHES = json.load(open(os.path.join(ROOT, "watches.json")))["watches"]
REAL_LOAD_COMPS = fb.load_comps


class Chain(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.hub = []
        # Freeze pricing fixtures: production correctly rejects old comps,
        # while unit tests should not expire as the calendar advances.
        fb.load_comps = lambda watch_id: [
            (float(c["price"]), c.get("source", "?"))
            for c in COMPS.get(watch_id, {}).get("comps", [])
        ]
        fb.tg_send = lambda text, buttons=None: self.sent.append((text, buttons))
        fb.hub_emit = lambda kind, did, payload: self.hub.append((kind, did, payload))

    # --- config integrity: the defect that silently killed carding ---
    def test_every_watch_has_comps(self):
        for w in WATCHES:
            self.assertIn(w["id"], COMPS, f"watch {w['id']} has no comps entry")
            self.assertGreaterEqual(len(COMPS[w["id"]]["comps"]), fb.MIN_COMPS)

    def test_watch_ids_are_unique(self):
        ids = [w["id"] for w in WATCHES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_budget_fits_monthly_cap(self):
        per_day = sum(w.get("results_cap", fb.HUB_RESULTS_CAP) for w in WATCHES)
        self.assertLessEqual(per_day * 30, fb.MONTHLY_CAP,
                             "daily scan x 30 exceeds the Apify monthly cap")

    # --- parsing real actor output ---
    def test_parse_item_real_actor_shape(self):
        item = {"posting_url": "https://www.facebook.com/marketplace/item/123/?ref=x",
                "listing_price": {"amount": "300", "currency": "USD"},
                "marketplace_listing_title": "PlayStation 5 Disc Edition",
                "listing_id": "123", "location": {"city": "Miami"}}
        p = fb.parse_item(item)
        self.assertIsNotNone(p)
        self.assertEqual(p["price"], 300.0)
        self.assertEqual(p["id"], "123")
        self.assertEqual(p["url"], "https://www.facebook.com/marketplace/item/123/")

    def test_parse_item_actor_listingUrl_shape(self):
        # Real shape returned by apify~facebook-marketplace-scraper
        item = {"listingUrl": "https://www.facebook.com/marketplace/item/1416601660461163/?ref=search",
                "id": "1416601660461163",
                "listing_price": {"amount": "325.00", "currency": "USD"},
                "marketplace_listing_title": "PlayStation 5 Slim Disc",
                "location": {"reverse_geocode": {"city": "Miami"}},
                "is_sold": False, "is_pending": False, "is_hidden": False}
        p = fb.parse_item(item)
        self.assertIsNotNone(p, "real actor item shape was rejected")
        self.assertEqual(p["price"], 325.0)
        self.assertEqual(p["id"], "1416601660461163")
        self.assertEqual(p["title"], "PlayStation 5 Slim Disc")
        self.assertEqual(p["url"], "https://www.facebook.com/marketplace/item/1416601660461163/")

    def test_parse_item_skips_sold_and_pending(self):
        base = {"listingUrl": "https://www.facebook.com/marketplace/item/9",
                "id": "9", "listing_price": {"amount": "100"},
                "marketplace_listing_title": "PS5"}
        self.assertIsNone(fb.parse_item(dict(base, is_sold=True)))
        self.assertIsNone(fb.parse_item(dict(base, is_pending=True)))

    def test_parse_item_rejects_missing_price(self):
        self.assertIsNone(fb.parse_item({"posting_url": "u", "title": "x"}))

    def test_ebay_active_context_is_display_only(self):
        w = next(x for x in WATCHES if x["id"] == "playstation-5-miami")
        old_id, old_secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
        os.environ.pop("EBAY_CLIENT_ID", None)
        os.environ.pop("EBAY_CLIENT_SECRET", None)
        try:
            self.assertIsNone(fb.ebay_active_asking(w))
        finally:
            if old_id is not None: os.environ["EBAY_CLIENT_ID"] = old_id
            if old_secret is not None: os.environ["EBAY_CLIENT_SECRET"] = old_secret
        comps = fb.load_comps
        fb.load_comps = lambda _: [(100.0, "verified_sold"), (110.0, "verified_sold"), (120.0, "verified_sold")]
        try:
            result = fb.price_deal(w, 50.0, "PlayStation 5 Disc Console")
            self.assertEqual(result["low"], 100.0)
            self.assertNotIn("ebay_active", result)
        finally:
            fb.load_comps = comps

    def test_title_identity_rejects_adjacent_console_and_game_bundle(self):
        w = next(x for x in WATCHES if x["id"] == "playstation-5-miami")
        self.assertFalse(fb.title_matches_watch(w, "PlayStation 3 with controllers"))
        self.assertFalse(fb.title_matches_watch(w, "PlayStation 5 Game Bundle (8 Games)"))
        self.assertTrue(fb.title_matches_watch(w, "PlayStation 5 Disc Console"))

    # --- pricing honesty ---
    def test_underpriced_listing_becomes_deal(self):
        w = next(x for x in WATCHES if x["id"] == "playstation-5-miami")
        low = min(c["price"] for c in COMPS["playstation-5-miami"]["comps"])
        pr = fb.price_deal(w, round(low * 0.55, 2), "PlayStation 5 Disc")
        self.assertIsNotNone(pr)
        self.assertEqual(pr["tier"], "DEAL")
        self.assertLess(pr["max_offer"], low)
        self.assertGreater(pr["spread"], 0)

    def test_fair_priced_listing_is_not_a_deal(self):
        w = next(x for x in WATCHES if x["id"] == "playstation-5-miami")
        low = min(c["price"] for c in COMPS["playstation-5-miami"]["comps"])
        pr = fb.price_deal(w, round(low * 0.99, 2), "PlayStation 5 Disc")
        self.assertIsNotNone(pr)
        self.assertEqual(pr["tier"], "PASS")

    def test_risk_words_downgrade_to_pass(self):
        w = next(x for x in WATCHES if x["id"] == "playstation-5-miami")
        low = min(c["price"] for c in COMPS["playstation-5-miami"]["comps"])
        pr = fb.price_deal(w, round(low * 0.55, 2), "PlayStation 5 for parts only")
        self.assertEqual(pr["tier"], "PASS")
        self.assertIn("parts only", pr["flags"])

    def test_real_loader_excludes_non_sale_prices(self):
        old_stale = fb.STALE_DAYS
        fb.STALE_DAYS = 1000
        try:
            self.assertEqual(REAL_LOAD_COMPS("playstation-5-miami"), [])
            self.assertEqual(len(REAL_LOAD_COMPS("macbook-air-m1-miami")), 3)
        finally:
            fb.STALE_DAYS = old_stale

    def test_watch_without_comps_never_cards(self):
        pr = fb.price_deal({"id": "no-such-watch"}, 50.0, "thing")
        self.assertIsNone(pr)

    # --- duplicate protection + card emission ---
    def test_full_flow_cards_once_then_dedupes(self):
        w = next(x for x in WATCHES if x["id"] == "playstation-5-miami")
        low = min(c["price"] for c in COMPS["playstation-5-miami"]["comps"])
        state = {"seen": {}, "deals": {}, "usage": {"month": "2026-10", "fetched": 0}}
        p = {"id": "listing-abc", "title": "PS5 Disc", "price": round(low * 0.55, 2),
             "url": "https://www.facebook.com/marketplace/item/abc", "city": "Miami"}
        fb.process_listing(state, w, p)
        self.assertEqual(len(self.sent), 1, "expected exactly one card")
        self.assertEqual(len(self.hub), 1)
        self.assertEqual(self.hub[0][0], "flip.deal_found")
        self.assertIn("listing-abc", state["deals"])
        self.assertIn("$", self.sent[0][0])
        self.assertTrue(self.sent[0][1], "card must carry action buttons")
        # duplicate: same listing again must not re-card or reset status
        state["deals"]["listing-abc"]["status"] = "contacted"
        fb.process_listing(state, w, p)
        self.assertEqual(len(self.sent), 1, "duplicate listing re-carded")
        self.assertEqual(state["deals"]["listing-abc"]["status"], "contacted")

    def test_previously_unpriced_listing_is_rechecked_when_comps_return(self):
        w = next(x for x in WATCHES if x["id"] == "playstation-5-miami")
        low = min(c["price"] for c in COMPS["playstation-5-miami"]["comps"])
        state = {"seen": {"recover": {"w": w["id"], "p": low * .55,
                                      "t": 1, "card": False, "nocomp": True}},
                 "deals": {}, "usage": {"month": "2026-10", "fetched": 0}}
        p = {"id": "recover", "title": "PlayStation 5 Disc Console",
             "price": round(low * .55, 2), "url": "u", "city": "Miami"}
        fb.process_listing(state, w, p)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("recover", state["deals"])
        self.assertFalse(state["seen"]["recover"].get("nocomp", True))

    def test_telegram_offset_is_sent_to_get_updates(self):
        old_token = os.environ.get("FLIP_TG_TOKEN")
        os.environ["FLIP_TG_TOKEN"] = "test-token"
        calls = []
        original = fb.http_json
        fb.http_json = lambda url, **kwargs: (calls.append(url) or {"result": []})
        try:
            fb.tg_get_updates(12345)
        finally:
            fb.http_json = original
            if old_token is None:
                os.environ.pop("FLIP_TG_TOKEN", None)
            else:
                os.environ["FLIP_TG_TOKEN"] = old_token
        self.assertIn("offset=12345", calls[0])

    def test_failed_scan_does_not_close_daily_retry_gate(self):
        state = {"last_discovery_date": None,
                 "usage": {"month": None, "fetched": 0, "warned": False, "blocked": False},
                 "seen": {}, "deals": {}}
        original = fb.apify_search
        fb.apify_search = lambda watch, cap: None
        try:
            fb.discovery(state, [{"id": "w", "query": "item", "city": "miami",
                                  "results_cap": 8}], force=True)
        finally:
            fb.apify_search = original
        self.assertIsNone(state["last_discovery_date"])

    def test_scan_clamps_actor_result_request_to_remaining_budget(self):
        state = {"last_discovery_date": None,
                 "usage": {"month": "2026-11", "fetched": 798, "warned": False, "blocked": False},
                 "seen": {}, "deals": {}}
        calls = []
        original = fb.apify_search
        fb.apify_search = lambda watch, cap: (calls.append(cap) or [])
        try:
            fb.discovery(state, [{"id": "w", "query": "item", "city": "miami",
                                  "results_cap": 8}], force=True)
        finally:
            fb.apify_search = original
        self.assertEqual(calls, [2])

    def test_over_budget_listing_is_ignored(self):
        w = {"id": "playstation-5-miami", "max_price": 100}
        state = {"seen": {}, "deals": {}, "usage": {"month": "2026-10", "fetched": 0}}
        fb.process_listing(state, w, {"id": "x", "title": "PS5", "price": 400.0,
                                      "url": "u", "city": "Miami"})
        self.assertEqual(self.sent, [])

    def test_max_active_deals_respected(self):
        w = next(x for x in WATCHES if x["id"] == "playstation-5-miami")
        low = min(c["price"] for c in COMPS["playstation-5-miami"]["comps"])
        state = {"seen": {}, "deals": {}, "usage": {"month": "2026-10", "fetched": 0}}
        for i in range(fb.MAX_ACTIVE):
            state["deals"][f"seed{i}"] = {"status": "found", "title": "t", "asking": 1}
        fb.process_listing(state, w, {"id": "new", "title": "PS5", "price": round(low*0.55, 2),
                                      "url": "u", "city": "Miami"})
        self.assertEqual(self.sent, [], "carded past MAX_ACTIVE")
        self.assertNotIn("new", state["deals"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
