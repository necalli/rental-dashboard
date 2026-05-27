import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    import app as app_module
except Exception:
    app_module = None


@unittest.skipIf(app_module is None, "Flask app dependencies are unavailable")
class AppConfigTests(unittest.TestCase):
    def test_env_bool_defaults_and_truthy_values(self) -> None:
        self.assertFalse(app_module._env_bool("__MISSING_TEST_ENV__", False))
        self.assertTrue(app_module._env_bool("__MISSING_TEST_ENV__", True))

    def test_parse_cors_origins_defaults_to_list(self) -> None:
        self.assertEqual(
            app_module._parse_cors_origins("http://localhost:3000, http://127.0.0.1:3000"),
            ["http://localhost:3000", "http://127.0.0.1:3000"],
        )

    def test_parse_cors_origins_allows_explicit_wildcard(self) -> None:
        self.assertEqual(app_module._parse_cors_origins("*"), "*")

    def test_parse_cors_origins_ignores_empty_items(self) -> None:
        self.assertEqual(
            app_module._parse_cors_origins("http://localhost:3000,, "),
            ["http://localhost:3000"],
        )


if __name__ == "__main__":
    unittest.main()
