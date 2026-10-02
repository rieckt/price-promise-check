"""Visible seller/date claims must bind to one priced offer, never an address.

JSON-LD omits these fields on live Galaxus pages. These regressions cover the
new optional input boundary, price ambiguity and fabricated positive delivery
guarantees; existing metadata tests cannot cover visible observations.
"""
import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError

from pricecheck_app.browser import validate_capture
from pricecheck_app.cli import main
from pricecheck_app.comparison import assess
from pricecheck_app.adapters.http import SourceError
from pricecheck_app.observations import validate_review
from pricecheck_app.retailers import product_url
from test_browser import capture
from test_compare import parsed


class ObservationContract(unittest.TestCase):
    def test_registered_product_routes_are_bounded_and_joybuy_is_unverified(self):
        urls = {"amazon": "https://www.amazon.de/dp/B0EXAMPL01", "otto": "https://www.otto.de/p/example-123/",
                "cyberport": "https://www.cyberport.de/pc/example/pdp/3306-06x/example.html",
                "notebooksbilliger": "https://www.notebooksbilliger.de/example%2B780849",
                "coolblue": "https://www.coolblue.de/produkt/913729/example.html",
                "expert": "https://www.expert.de/shop/unsere-produkte/computer/17320012214-example.html"}
        for retailer, url in urls.items():
            with self.subTest(retailer=retailer):
                self.assertEqual(product_url(url, retailer), url)
                with self.assertRaises(SourceError): product_url(url + "?session=private", retailer)
        with self.assertRaisesRegex(SourceError, "not verified"):
            product_url("https://www.joybuy.de/product/123", "joybuy")

    def data(self):
        value = capture()
        value["observed_at"] = "2026-10-02T08:00:00+02:00"
        value["observations"] = [{"offer_price": "229.00",
                                  "seller": {"name": "Galaxus", "quote": "Supplied by Galaxus"},
                                  "delivery": {"latest_date": "2026-10-06", "quote": "Delivered Tue, 6.10.",
                                               "scope": "country_DE"}}]
        return value

    def parse(self, value):
        return validate_capture(value, datetime(2026, 10, 2, 6, tzinfo=timezone.utc))["product"]["offers"][0]

    def test_visible_seller_and_late_delivery_are_bound_and_excluded(self):
        offer = self.parse(self.data())
        self.assertEqual(offer["seller_kind"], "direct")
        self.assertEqual(offer["advertised_delivery"]["calendar_days"], 4)
        self.assertFalse(offer["advertised_delivery"]["address_guarantee"])
        self.assertIn("delivery_exceeds_three_days", assess(offer, parsed("mediamarkt_r20"))["excluded_reasons"])

    def test_country_delivery_within_three_days_still_needs_address_review(self):
        data = self.data(); data["observations"][0]["delivery"]["latest_date"] = "2026-10-04"
        offer = self.parse(data)
        self.assertIn("delivery_within_three_calendar_days_unconfirmed", assess(offer, parsed("mediamarkt_r20"))["review_reasons"])

    def test_mismatched_ambiguous_private_and_conflicting_observations_fail_closed(self):
        for change in ("price", "ambiguous", "private", "quote", "scope", "conflict", "date"):
            data = self.data(); entry = data["observations"][0]
            if change == "price": entry["offer_price"] = "1.00"
            if change == "ambiguous": data["product"]["offers"] = [data["product"]["offers"], copy.deepcopy(data["product"]["offers"])]
            if change == "private": entry["home_address"] = "private-canary"
            if change == "quote": entry["seller"]["quote"] = "Another seller"
            if change == "scope": entry["delivery"]["scope"] = "home_address"
            if change == "conflict": data["product"]["offers"]["seller"] = {"name": "Another seller"}
            if change == "date": entry["delivery"]["latest_date"] = "2026-10-01"
            with self.subTest(change=change), self.assertRaises(SourceError): self.parse(data)

    def test_removed_product_reports_http_status_without_raw_url(self):
        error = HTTPError("https://private-canary.invalid", 410, "Gone", {}, None)
        output = io.StringIO()
        with patch("pricecheck_app.comparison.resolve_product", side_effect=error), redirect_stdout(output):
            self.assertEqual(main(["compare", "reference", "--json"]), 1)
        result = json.loads(output.getvalue())
        self.assertEqual((result["status"], result["error"]), ("source_gone", "HTTP 410"))
        self.assertNotIn("private-canary", output.getvalue())


class ReviewEvidenceContract(unittest.TestCase):
    def setUp(self):
        self.product = parsed("mediamarkt_r20")
        self.now = datetime(2026, 10, 2, 6, tzinfo=timezone.utc)
        self.document = {"schema_version": 1, "source": self.product["url"], "gtin": self.product["gtin"],
                         "observed_at": self.now.isoformat(),
                         "market": {"id": "475", "pickup": "available_today", "stock": "unknown",
                                    "price": None, "evidence_kind": "selected_market_ui"}, "local_offers": []}

    def test_pickup_never_becomes_local_price_or_stock_confirmation(self):
        result = validate_review(self.document, self.product, "475", self.now)
        self.assertIsNone(result["market"]["price"])
        self.assertEqual(result["market"]["stock"], "unknown")
        for key, value in [("price", "43.99"), ("stock", "confirmed_in_store"), ("id", "999")]:
            document = copy.deepcopy(self.document); document["market"][key] = value
            with self.subTest(key=key), self.assertRaises(SourceError):
                validate_review(document, self.product, "475", self.now)

    def test_local_advertisement_requires_identity_validity_and_public_proof(self):
        entry = {"shop": "Example shop", "gtin": self.product["gtin"], "price": "199.00", "currency": "EUR",
                 "valid_from": "2026-10-01", "valid_until": "2026-10-03", "evidence_kind": "flyer",
                 "source": "https://example.org/public-flyer.pdf"}
        document = copy.deepcopy(self.document); document["local_offers"] = [entry]
        catalog = {"market": {"id": "475"}, "local_candidates": [{"name": "Example shop", "distance_km": 10,
                    "within_30km_airline": True, "radius_assessment": "inside"}]}
        with patch("pricecheck_app.locations.catalogue", return_value=catalog):
            result = validate_review(document, self.product, "475", self.now)
            self.assertEqual(result["local_offers"][0]["status"], "needs_review")
            for key, value in [("gtin", "4974019174815"), ("evidence_kind", "self_taken_photo"),
                               ("valid_until", "2026-10-01"), ("source", "https://example.org/?private=x")]:
                altered = copy.deepcopy(document); altered["local_offers"][0][key] = value
                with self.subTest(key=key), self.assertRaises(SourceError):
                    validate_review(altered, self.product, "475", self.now)
