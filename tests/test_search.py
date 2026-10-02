import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pricecheck_app.adapters.mediamarkt import SourceError, matches_query, parse_search, validate_url
from pricecheck_app.cli import main
from pricecheck_app.locations import distance_km

FIXTURE = json.loads((Path(__file__).parent / "fixtures/mediamarkt-search.json").read_text())


def page(data, include_list=True):
    state = json.dumps({"apolloState": data["apollo_state"], "unused": "undefined; text"})
    state = state[:-1] + ', "optional": undefined}'
    result = '<script>window.__PRELOADED_STATE__ = ' + state + ';</script>'
    if include_list:
        result += '<script type="application/ld+json">' + json.dumps({
            "@type": "ItemList", "itemListElement": data["item_list"]}) + '</script>'
    return result


class SearchContract(unittest.TestCase):
    def setUp(self):
        self.data = copy.deepcopy(FIXTURE)

    def products(self):
        return {p["product_id"]: p for p in parse_search(page(self.data))}

    def test_variants_keep_identity_and_search_order(self):
        products = parse_search(page(self.data))
        self.assertEqual([p["product_id"] for p in products],
                         ["175219236", "3059512", "107780003", "176361756"])
        self.assertEqual(products[2]["ean"], "6973734688032")
        self.assertEqual(len({p["ean"] for p in products}), 4)

    def test_marketplace_and_unavailable_direct_offer_are_distinct(self):
        products = self.products()
        self.assertEqual(products["107780003"]["seller_kind"], "marketplace")
        self.assertEqual(products["3059512"]["seller_kind"], "direct")
        self.assertEqual(products["3059512"]["online_availability"], "unavailable")
        self.assertEqual(products["107780003"]["market_availability"], "unknown")
        self.assertEqual(products["107780003"]["price_promise"], "not_checked")

    def test_price_shipping_and_member_prices_remain_separate(self):
        products = self.products()
        r20 = products["107780003"]
        self.assertEqual((r20["price"], r20["shipping"]), ("263.79", "4.99"))
        pro = products["3059512"]
        self.assertEqual(pro["price"], "349.00")
        member = next(p for p in pro["promotions"] if p["audience"] == "member")
        self.assertEqual(member["price"], "293.28")
        self.assertEqual(member["eligibility"], "not_checked")

    def test_seller_name_does_not_prove_direct_sale(self):
        self.assertEqual(self.products()["176361756"]["seller_kind"], "marketplace")

    def test_missing_seller_flag_or_null_availability_stays_unknown(self):
        for value in self.data["apollo_state"].values():
            if value.get("id") == "Media:de:3059512":
                value.pop("isProductOfTypeMarketplace", None)
                if value["__typename"] == "CofrOnlineStatusFeature":
                    value["isAvailableAndBuyable"] = None
        product = self.products()["3059512"]
        self.assertEqual(product["seller_kind"], "unknown")
        self.assertEqual(product["online_availability"], "unknown")

    def test_missing_list_fails_while_explicit_empty_list_is_valid(self):
        with self.assertRaises(SourceError):
            parse_search(page(self.data, include_list=False))
        self.data["item_list"] = []
        self.data["apollo_state"]["ROOT_QUERY"]["searchV4:fixture"]["totalProducts"] = 0
        self.assertEqual(parse_search(page(self.data)), [])

    def test_zero_search_hits_do_not_return_fallback_recommendations(self):
        self.data["apollo_state"]["ROOT_QUERY"]["searchV4:fixture"]["totalProducts"] = 0
        self.assertEqual(parse_search(page(self.data)), [])

    def test_missing_or_inconsistent_counts_fail_closed(self):
        root = self.data["apollo_state"]["ROOT_QUERY"]["searchV4:fixture"]
        for count in (None, True, -1, 1):
            root["totalProducts"] = count
            with self.subTest(count=count), self.assertRaises(SourceError):
                self.products()

    def test_blocked_page_or_executable_state_is_rejected(self):
        with self.assertRaises(SourceError):
            parse_search('<html>Access denied</html>')
        with self.assertRaises(ValueError):
            parse_search('<script>window.__PRELOADED_STATE__ = evil();</script>')

    def test_urls_cannot_target_other_hosts_or_protocols(self):
        for url in ['http://www.mediamarkt.de/de/', 'https://www.mediamarkt.de.evil.test/',
                    'https://user@www.mediamarkt.de/', 'https://www.mediamarkt.de:444/']:
            with self.subTest(url=url), self.assertRaises(SourceError):
                validate_url(url)

    def test_invalid_query_fails_with_machine_readable_status(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["search", "  ", "--json"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")

    def test_radius_uses_air_distance_and_not_display_rounding(self):
        self.assertEqual(distance_km((53, 10), (53, 10)), 0)
        self.assertAlmostEqual(distance_km((0, 0), (0, 1)), 111.195, places=3)
        self.assertLess(distance_km((0, 0), (0, 0.2697)), 30)
        self.assertGreater(distance_km((0, 0), (0, 0.2699)), 30)

    def test_candidate_filter_requires_model_tokens_and_keeps_variants_unverified(self):
        products = self.products()
        self.assertTrue(matches_query(products["3059512"], "dreame r20 pro"))
        self.assertFalse(matches_query(products["107780003"], "dreame r20 pro"))
        self.assertFalse(matches_query({"name": "DREAME R200 PRO"}, "dreame r20"))
        self.assertFalse(matches_query(products["107780003"], "zzzzqqqxyz123456789"))


if __name__ == "__main__":
    unittest.main()
