"""Contracts for identity, offer binding and honest source coverage.

These prevent wrong-variant comparisons, alternative-condition contamination,
false delivery promises and treating blocked sources as empty assortments.
Existing search tests do not exercise product pages or cross-retailer rules.
Fixtures and mocked network boundaries need no production-only test seam.
"""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from pricecheck_app.adapters.competitors import Competitor
from pricecheck_app.adapters.http import SourceError, fetch, validate_url
from pricecheck_app.adapters.mediamarkt import parse_search
from pricecheck_app.comparison import assess, compare, resolve_product
from pricecheck_app.offers import gtin, parse_product
from test_search import FIXTURE as SEARCH_FIXTURE, page

FIXTURES = json.loads((Path(__file__).parent / "fixtures/product-offers.json").read_text())


def product_page(data):
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


def parsed(key):
    fixture = FIXTURES[key]
    return parse_product(product_page(fixture["product"]), fixture["source"], key.split("_")[0])


class OfferContract(unittest.TestCase):
    def test_galaxus_public_metadata_does_not_prove_seller_or_delivery(self):
        offer = parsed("galaxus_r20")["offers"][0]
        self.assertEqual(offer["gtin"], parsed("mediamarkt_r20")["gtin"])
        self.assertEqual(offer["price"], "229.00")
        self.assertEqual(offer["seller_kind"], "unknown")
        self.assertIsNone(offer["delivery"])
        self.assertIn("seller_unknown", assess(offer, parsed("mediamarkt_r20"))["review_reasons"])

    def test_product_group_requires_concrete_gtin_and_bound_offer(self):
        product = parsed("mediamarkt_miele")
        self.assertEqual(product["gtin"], "04002516847533")
        self.assertEqual(product["offers"][0]["condition"], "new")
        fixture = copy.deepcopy(FIXTURES["mediamarkt_miele"])
        fixture["product"].pop("gtin13")
        with self.assertRaises(SourceError):
            parse_product(product_page(fixture["product"]), fixture["source"], "mediamarkt")

    def test_two_categories_normalize_seller_price_shipping_and_identity(self):
        r20 = parsed("mediamarkt_r20")["offers"][0]
        microwave = parsed("alternate_sharp")["offers"][0]
        self.assertEqual((r20["price"], r20["shipping"], r20["seller_kind"], r20["condition"]),
                         ("263.79", "4.99", "marketplace", "new"))
        self.assertEqual((microwave["price"], microwave["shipping"], microwave["seller_kind"]),
                         ("69.90", "7.99", "direct"))
        self.assertEqual(r20["gtin"], "06973734688032")
        self.assertEqual(microwave["gtin"], "04974019174815")
        self.assertEqual(r20["delivery"]["max_days"], 8)

    def test_gtin_padding_preserves_identity_and_checks_checksum(self):
        self.assertEqual(gtin("6973734688032"), gtin("06973734688032"))
        for value in ["6973734688033", "697373468803", 6973734688032, "６９７３７３４６８８０３２"]:
            with self.subTest(value=value), self.assertRaises(SourceError):
                gtin(value)

    def test_refurbished_alternative_cannot_set_active_offer_condition(self):
        data = copy.deepcopy(SEARCH_FIXTURE)
        for node in data["apollo_state"].values():
            if node.get("id") == "Media:de:176361756" and node.get("__typename") == "CofrCoreFeature":
                node["relevantOfferId"] = "new-offer"
        result = next(p for p in parse_search(page(data)) if p["product_id"] == "176361756")
        self.assertEqual(result["condition"], "unknown")
        for node in data["apollo_state"].values():
            if node.get("__typename") == "CofrCoreFeature" and node.get("id") == "Media:de:176361756":
                node["relevantOfferId"] = "1849177405"
        result = next(p for p in parse_search(page(data)) if p["product_id"] == "176361756")
        self.assertEqual(result["condition"], "VERY_GOOD")

    def test_each_concrete_offer_keeps_its_own_price_seller_and_condition(self):
        fixture = copy.deepcopy(FIXTURES["mediamarkt_r20"])
        alternative = copy.deepcopy(fixture["product"]["offers"][0])
        alternative.update(price=99, itemCondition="RefurbishedCondition", seller={"name": "MediaMarkt"})
        fixture["product"]["offers"].append(alternative)
        offers = parse_product(product_page(fixture["product"]), fixture["source"], "mediamarkt")["offers"]
        self.assertEqual([(o["price"], o["seller_kind"], o["condition"])for o in offers],
                         [("263.79", "marketplace", "new"), ("99.00", "direct", "refurbished")])

    def test_missing_seller_shipping_or_delivery_never_become_defaults(self):
        fixture = copy.deepcopy(FIXTURES["alternate_sharp"])
        fixture["product"]["offers"].pop("seller")
        fixture["product"]["offers"].pop("shippingDetails")
        offer = parse_product(product_page(fixture["product"]), fixture["source"], "alternate")["offers"][0]
        self.assertEqual(offer["seller_kind"], "unknown")
        self.assertIsNone(offer["shipping"])
        self.assertIsNone(offer["delivery"])

    def test_wrong_product_aggregate_price_and_block_page_fail_closed(self):
        fixture = copy.deepcopy(FIXTURES["alternate_sharp"])
        with self.assertRaises(SourceError):
            parse_product(product_page(fixture["product"]), fixture["source"] + "9", "alternate")
        fixture["product"]["offers"]["@type"] = "AggregateOffer"
        with self.assertRaises(SourceError):
            parse_product(product_page(fixture["product"]), fixture["source"], "alternate")
        with self.assertRaises(SourceError):
            parse_product("Access denied", fixture["source"], "alternate")

    def test_offer_url_cannot_escape_retailer_or_product(self):
        fixture = copy.deepcopy(FIXTURES["alternate_sharp"])
        for url in ["https://evil.test/product", "https://www.alternate.de/other"]:
            fixture["product"]["offers"]["url"] = url
            # The product URL selects this node; offer URL must still be checked.
            fixture["product"]["url"] = fixture["source"]
            with self.subTest(url=url), self.assertRaises(SourceError):
                parse_product(product_page(fixture["product"]), fixture["source"], "alternate")
        for url in ["http://www.galaxus.de/", "https://user@www.galaxus.de/", "https://www.galaxus.de.evil.test/"]:
            with self.subTest(url=url), self.assertRaises(SourceError):
                validate_url(url, "www.galaxus.de")


class ComparisonContract(unittest.TestCase):
    def setUp(self):
        self.product = parsed("alternate_sharp")
        self.offer = self.product["offers"][0]

    def test_identity_marketplace_condition_and_delivery_exclusions_are_explicit(self):
        wrong = parsed("mediamarkt_r20")["offers"][0]
        reasons = assess(wrong, self.product)["excluded_reasons"]
        self.assertIn("different_gtin", reasons)
        self.assertIn("marketplace_seller", reasons)
        self.assertIn("delivery_exceeds_three_days", reasons)
        self.offer["condition"] = "refurbished"
        self.assertIn("condition_excluded", assess(self.offer, self.product)["excluded_reasons"])

    def test_three_day_metadata_is_not_a_calendar_delivery_guarantee(self):
        self.offer["delivery"] = {"min_days": 0, "max_days": 3, "calendar_basis": "unspecified"}
        result = assess(self.offer, self.product)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("delivery_within_three_calendar_days_unconfirmed", result["review_reasons"])
        self.assertIn("market_stock_and_price_unconfirmed", result["review_reasons"])

    def test_missing_fields_and_apple_require_review_or_exclusion(self):
        self.offer.update(seller_kind="unknown", condition="unknown", availability="unknown", price=None, brand="Apple")
        result = assess(self.offer, self.product)
        self.assertIn("apple_excluded", result["excluded_reasons"])
        self.assertTrue({"seller_unknown", "condition_unknown", "availability_unknown", "comparable_eur_price_missing"} <= set(result["review_reasons"]))
        self.product["brand"], self.offer["brand"] = "Apple", None
        self.assertIn("apple_excluded", assess(self.offer, self.product)["excluded_reasons"])

    def test_ean_ambiguity_never_picks_first_result(self):
        candidate = {"ean": "4974019174815", "url": "https://www.mediamarkt.de/de/product/_sharp-1.html"}
        with patch("pricecheck_app.comparison.MediaMarkt.search", return_value=("source", [candidate, candidate])):
            with self.assertRaisesRegex(SourceError, "ambiguous"):
                resolve_product("4974019174815")

    def test_ean_resolution_rechecks_product_identity(self):
        candidate = {"ean": "4974019174815", "url": "https://www.mediamarkt.de/de/product/_sharp-1.html"}
        with patch("pricecheck_app.comparison.MediaMarkt.search", return_value=("source", [candidate])), patch("pricecheck_app.comparison.MediaMarkt.product", return_value=parsed("mediamarkt_r20")):
            with self.assertRaisesRegex(SourceError, "disagree"):
                resolve_product("4974019174815")

    def test_blocked_source_is_not_empty_and_all_approved_shops_remain_visible(self):
        blocked = HTTPError("https://www.galaxus.de/search", 403, "Forbidden", {}, None)
        with patch("pricecheck_app.comparison.resolve_product", return_value=self.product), patch.object(Competitor, "discover", side_effect=lambda ean: ("search", [])):
            result = compare("reference", "475")
        self.assertEqual(len(result["retailer_checks"]), 9)
        self.assertEqual(sum(c["status"] == "no_search_results" for c in result["retailer_checks"]), 9)
        with patch("pricecheck_app.comparison.resolve_product", return_value=self.product), patch.object(Competitor, "discover", side_effect=blocked):
            result = compare("reference", "475")
        self.assertEqual(sum(c["status"] == "blocked" for c in result["retailer_checks"]), 9)
        self.assertIsNone(result["review_first"])

    def test_failed_offer_does_not_hide_valid_review_candidate(self):
        url = FIXTURES["alternate_sharp"]["source"]
        with patch("pricecheck_app.comparison.resolve_product", return_value=self.product), patch.object(Competitor, "discover", return_value=("search", [])), patch.object(Competitor, "product", side_effect=[self.product, SourceError("bad schema")]):
            result = compare("reference", None, [url, url + "?second"])
        check = next(c for c in result["retailer_checks"] if c["retailer"] == "alternate")
        self.assertEqual(check["status"], "partial")
        self.assertEqual(check["failures"][0]["status"], "failed")
        self.assertEqual(result["review_first"]["url"], url)
        self.assertEqual(result["market_price_comparison"], "unconfirmed")

    def test_discovery_uses_product_links_and_explicit_no_result_evidence(self):
        url = FIXTURES["alternate_sharp"]["source"]
        html = '<a href="' + url + '" class="productBox card">item</a><a href="https://evil.test">ad</a>'
        with patch("pricecheck_app.adapters.competitors.fetch", return_value=html):
            self.assertEqual(Competitor("alternate").discover("4974019174815")[1], [url])
        with patch("pricecheck_app.adapters.competitors.fetch", return_value='<div class="alert alert-info">Ihre Suche erbrachte leider keine Ergebnisse.</div>'):
            self.assertEqual(Competitor("alternate").discover("4974019174815")[1], [])
        with patch("pricecheck_app.adapters.competitors.fetch", return_value='<script>"Ihre Suche erbrachte leider keine Ergebnisse."</script>'):
            with self.assertRaises(SourceError):
                Competitor("alternate").discover("4974019174815")

    def test_transport_checks_redirect_destination_and_response_size(self):
        response = MagicMock()
        response.url = "https://www.alternate.de/product"
        response.read.return_value = b"x" * 8_000_001
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("pricecheck_app.adapters.http.build_opener", return_value=opener) as build:
            with self.assertRaisesRegex(SourceError, "size limit"):
                fetch(response.url, "www.alternate.de")
            redirect = build.call_args.args[0]
            with self.assertRaises(SourceError):
                redirect.redirect_request(None, None, 302, "Found", {}, "https://evil.test/")
        with patch("pricecheck_app.adapters.http.build_opener") as build:
            with self.assertRaises(SourceError):
                fetch("https://127.0.0.1/", "www.alternate.de")
            build.assert_not_called()
        with patch("pricecheck_app.adapters.competitors.fetch", return_value="Access denied"):
            with self.assertRaises(SourceError):
                Competitor("alternate").discover("4974019174815")


if __name__ == "__main__":
    unittest.main()
