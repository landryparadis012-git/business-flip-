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


class Chain(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.hub = []
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

    def test_parse_item_rejects_missing_price(self):
        self.assertIsNone(fb.parse_item({"posting_url": "u", "title": "x"}))

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
