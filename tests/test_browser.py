"""Browser handoff contracts: fresh evidence, source scope and honest fallback.

These prevent stale/fabricated-source imports, private envelope disclosure and
HTTP retries silently replacing browser evidence. No existing test covers the
handoff. Public fixtures and normal file/network boundaries are sufficient.
"""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from pricecheck_app.adapters.competitors import Competitor
from pricecheck_app.adapters.http import SourceError
from pricecheck_app.browser import MAX_BYTES, load_browser_evidence, validate_capture
from pricecheck_app.cli import main
from pricecheck_app.comparison import compare
from test_compare import FIXTURES, parsed


def capture():
    fixture = copy.deepcopy(FIXTURES["galaxus_r20"])
    return {"source": fixture["source"], "observed_at": datetime.now(timezone.utc).isoformat(),
            "product": fixture["product"]}


class BrowserContract(unittest.TestCase):
    def test_english_locale_on_german_retailer_keeps_exact_identity(self):
        data = capture()
        url = "https://www.galaxus.de/en/s2/product/dreame-r20-vacuum-cleaners-36108749"
        data["source"] = url
        data["product"]["offers"]["url"] = url
        result = validate_capture(data)
        self.assertEqual(result["product"]["gtin"], "06973734688032")
        self.assertEqual(result["product"]["offers"][0]["url"], url)

    def test_browser_capture_preserves_identity_price_provenance_and_unknowns(self):
        result = validate_capture(capture())
        offer = result["product"]["offers"][0]
        self.assertEqual((offer["price"], offer["gtin"], offer["seller_kind"]),
                         ("229.00", "06973734688032", "unknown"))
        self.assertEqual(offer["evidence"], "browser_jsonld")
        self.assertEqual(len(offer["evidence_sha256"]), 64)
        self.assertEqual(offer["capture_trust"], "user_or_agent_supplied_not_authenticated")
        self.assertIsNone(offer["delivery"])

    def test_stale_future_and_timezone_free_captures_are_rejected(self):
        now = datetime(2026, 10, 2, tzinfo=timezone.utc)
        for stamp in [(now - timedelta(seconds=901)).isoformat(),
                      (now + timedelta(seconds=61)).isoformat(), "2026-10-02T00:00:00", None]:
            data = capture(); data["observed_at"] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(SourceError):
                validate_capture(data, now)
        data["observed_at"] = (now - timedelta(seconds=900)).isoformat()
        self.assertEqual(validate_capture(data, now)["retailer"], "galaxus")

    def test_other_hosts_accounts_queries_and_missing_identity_are_rejected(self):
        data = capture()
        for source in ["https://evil.test/product", "https://www.galaxus.de/de/account",
                       data["source"] + "?session=private", data["source"] + "#private"]:
            data["source"] = source
            with self.subTest(source=source), self.assertRaises(SourceError):
                validate_capture(data)
        data = capture();data["product"].pop("gtin")
        with self.assertRaises(SourceError):
            validate_capture(data)

    def test_raw_page_or_private_fields_cannot_enter_public_result(self):
        for field in ["html", "cookies", "home_address", "profile"]:
            data = capture();data[field] = "private-canary"
            with self.subTest(field=field), self.assertRaises(SourceError):
                validate_capture(data)
        data = capture();data["product"]["description"] = "private-canary"
        with self.assertRaises(SourceError):
            validate_capture(data)

    def test_nonfinite_prices_and_oversized_input_are_rejected(self):
        data = capture();data["product"]["offers"]["price"] = float("nan")
        with self.assertRaises(SourceError):
            validate_capture(data)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "oversize.json";path.write_bytes(b" " * (MAX_BYTES + 1))
            with self.assertRaises(SourceError):
                load_browser_evidence([str(path)])

    def test_duplicate_sources_keys_and_bad_envelope_fail_closed(self):
        data = capture()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "capture.json"
            for value in [{"schema_version": True, "captures": [data]},
                          {"schema_version": 1, "captures": [data, data]},
                          {"schema_version": 1, "captures": []}]:
                path.write_text(json.dumps(value))
                with self.subTest(value=value["schema_version"]), self.assertRaises(SourceError):
                    load_browser_evidence([str(path)])
            path.write_text('{"schema_version":1,"schema_version":1,"captures":[]}')
            with self.assertRaises(SourceError):
                load_browser_evidence([str(path)])

    def test_blocked_http_generates_concrete_browser_request(self):
        blocked = HTTPError("https://www.galaxus.de/search", 403, "Forbidden", {}, None)
        with patch("pricecheck_app.comparison.resolve_product", return_value=parsed("mediamarkt_r20")), patch.object(Competitor, "discover", side_effect=blocked):
            result = compare("reference", None)
        request = next(r for r in result["fallback_requests"] if r["retailer"] == "galaxus")
        self.assertEqual(request["ean"], "6973734688032")
        self.assertEqual(request["search_url"], "https://www.galaxus.de/search?q=6973734688032")
        self.assertEqual(request["reason"], "blocked")

    def test_browser_fallback_skips_only_covered_retailer_and_keeps_open_rules(self):
        browser = validate_capture(capture())
        with patch("pricecheck_app.comparison.resolve_product", return_value=parsed("mediamarkt_r20")), patch.object(Competitor, "discover", return_value=("source", [])) as discover:
            result = compare("reference", "475", browser_captures=[browser])
        self.assertEqual(discover.call_count, 8)
        check = next(c for c in result["retailer_checks"] if c["retailer"] == "galaxus")
        self.assertEqual(check["status"], "browser_checked")
        self.assertEqual(result["offers"][0]["assessment"]["identity_match"], "exact_gtin")
        self.assertIn("seller_unknown", result["offers"][0]["assessment"]["review_reasons"])
        self.assertEqual(result["coverage"], "partial")

    def test_wrong_variant_remains_excluded_and_conflicting_url_is_rejected(self):
        browser = validate_capture(capture())
        with patch("pricecheck_app.comparison.resolve_product", return_value=parsed("alternate_sharp")), patch.object(Competitor, "discover", return_value=("source", [])):
            result = compare("reference", None, browser_captures=[browser])
            self.assertIsNone(result["review_first"])
            self.assertIn("different_gtin", result["offers"][0]["assessment"]["excluded_reasons"])
            with self.assertRaises(SourceError):
                compare("reference", None, [browser["source"] + "?other"], [browser])

    def test_stdin_cli_handoff_and_redacted_failure(self):
        envelope = {"schema_version": 1, "captures": [capture()]}
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(envelope).encode()))
        output = io.StringIO()
        with patch("sys.stdin", stdin), patch("pricecheck_app.comparison.resolve_product", return_value=parsed("mediamarkt_r20")), patch.object(Competitor, "discover", return_value=("source", [])), redirect_stdout(output):
            self.assertEqual(main(["compare", "reference", "--browser-evidence", "-", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["offers"][0]["evidence"], "browser_jsonld")
        output = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(b'{"home_address":"private-canary"}'))
        with patch("sys.stdin", stdin), redirect_stdout(output):
            self.assertEqual(main(["compare", "reference", "--browser-evidence", "-", "--json"]), 1)
        self.assertNotIn("private-canary", output.getvalue())


if __name__ == "__main__":
    unittest.main()
