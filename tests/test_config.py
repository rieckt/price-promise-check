import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pricecheck_app.cli import main
from pricecheck_app.config import ConfigError, config_path, initialize, load_config
from pricecheck_app.locations import catalogue
from scripts.check_public_tree import inspect_content


class PrivateConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_fresh_checkout_has_no_personal_default_market(self):
        self.assertIsNone(load_config()["market_id"])
        shops = catalogue()
        self.assertIsNone(shops["market"])
        self.assertEqual(shops["local_catalogue_status"], "not_configured")
        self.assertEqual(shops["local_candidates"], [])
        self.assertIn("galaxus.de", shops["online_domains"])

    def test_init_is_private_and_never_overwrites_existing_settings(self):
        initialize("475")
        path = config_path()
        before = path.read_bytes()
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaises(ConfigError):
            initialize("123")
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_market_does_not_inherit_luebeck_candidates(self):
        initialize("123", {"name": "Example market", "address": "", "coordinates": [50, 8]})
        shops = catalogue()
        self.assertEqual(shops["market"]["id"], "123")
        self.assertEqual(shops["local_catalogue_status"], "not_available")
        self.assertEqual(shops["local_candidates"], [])

    def test_private_fields_never_cross_config_or_shop_output_boundary(self):
        initialize("475")
        path = config_path()
        settings = json.loads(path.read_text())
        settings["home_address"] = {"street": "Private Example Avenue 42", "name": "Example Owner"}
        settings["market"] = {"name": "Public market", "address": "", "home_address": settings["home_address"]}
        path.write_text(json.dumps(settings), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["shops", "--json"])
        self.assertEqual(code, 0)
        for payload in (json.dumps(load_config()), output.getvalue()):
            self.assertNotIn("Private Example", payload)
            self.assertNotIn("Example Owner", payload)
            self.assertNotIn("home_address", payload)

    def test_corrupt_configuration_returns_redacted_error(self):
        initialize("475")
        config_path().write_text('{"private":"Example Owner",', encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["shops", "--json"])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("Example Owner", output.getvalue())
        self.assertNotIn(self.temp.name, output.getvalue())

    def test_invalid_market_or_coordinates_are_rejected(self):
        for market_id in ("", "abc", 475):
            with self.subTest(market_id=market_id), self.assertRaises(ConfigError):
                initialize(market_id)
        for coordinates in ([91, 8], [50, float("nan")], [True, 8], [50]):
            with self.subTest(coordinates=coordinates), self.assertRaises(ConfigError):
                initialize("123", {"coordinates": coordinates})

    def test_privacy_guard_detects_canary_without_reporting_its_value(self):
        canary = "Private Example Avenue 42"
        self.assertEqual(inspect_content("README.md", canary.encode(), [canary.casefold()]),
                         ["private_configuration_value"])
        self.assertIn("private_address_object", inspect_content("example.json", json.dumps({
            "home_address": {"street": canary}}).encode(), []))
        personal_path = "/" + "Users/" + "example/" + "project"
        self.assertIn("personal_absolute_path", inspect_content("skill/SKILL.md", personal_path.encode(), []))
        token = "sk-" + "a" * 24
        self.assertIn("credential_pattern", inspect_content("data.txt", token.encode(), []))
        self.assertIn("private_file_or_unsafe_path", inspect_content("private/config.json", b"{}", []))


if __name__ == "__main__":
    unittest.main()
