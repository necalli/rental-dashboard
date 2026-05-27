import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class _DummyResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body.encode("utf-8")
        self.status = int(status)

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_DummyResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _http_error(url: str, code: int, body: str = "") -> HTTPError:
    return HTTPError(url, code, f"HTTP {code}", hdrs=None, fp=io.BytesIO(body.encode("utf-8")))


class FxRatesTests(unittest.TestCase):
    def _tmpdir(self) -> str:
        path = tempfile.mkdtemp(prefix="fx-tests-")
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
        return path

    def _reload_fx(self, tmpdir: str, **env_overrides):
        env = {
            "RENTAL_DB_PATH": str(Path(tmpdir) / "rental_dashboard.db"),
            "RENTAL_FX_CACHE_PATH": str(Path(tmpdir) / "fx_cache.json"),
            "RENTAL_FX_PERSIST": "1",
            "RENTAL_FX_DB_PERSIST": "1",
            "RENTAL_FX_ALLOW_STALE": "1",
            "RENTAL_FX_ENABLE_FALLBACK_PROVIDERS": "0",
            "RENTAL_FX_TIMEOUT": "2",
            "RENTAL_FX_PER_ATTEMPT_TIMEOUT": "1",
            "RENTAL_FX_RETRIES": "1",
            "RENTAL_FX_BACKOFF_BASE_MS": "1",
            "RENTAL_FX_BACKOFF_MAX_MS": "4",
            "RENTAL_FX_TTL_SECONDS": "1",
            "RENTAL_FX_MAX_STALE_SECONDS": str(14 * 24 * 60 * 60),
        }
        env.update({k: str(v) for k, v in env_overrides.items()})
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        import services.fx_rates as fx_rates

        return importlib.reload(fx_rates)

    def test_403_uses_stale_cache_fallback(self) -> None:
        tmpdir = self._tmpdir()
        fx_rates = self._reload_fx(
            tmpdir,
            RENTAL_FX_BASE_URLS="https://primary.example",
        )
        stale_payload = {
            "rate": 0.742,
            "base": "SGD",
            "target": "USD",
            "as_of": "2026-02-10",
            "source": "seed",
            "stale": False,
        }
        fx_rates._CACHE[("SGD", "USD")] = {
            "fetched_at": time.time() - 6 * 60 * 60,
            "payload": stale_payload,
        }

        with patch("services.fx_rates.urllib.request.urlopen") as mocked_urlopen:
            mocked_urlopen.side_effect = _http_error(
                "https://primary.example/latest?from=SGD&to=USD",
                403,
                "blocked",
            )
            out = fx_rates.get_fx_rate("SGD", "USD", _force_refresh=True)

        self.assertIsNotNone(out)
        self.assertAlmostEqual(float(out["rate"]), 0.742, places=6)
        self.assertTrue(bool(out.get("stale")))

    def test_timeout_retries_then_success(self) -> None:
        tmpdir = self._tmpdir()
        fx_rates = self._reload_fx(
            tmpdir,
            RENTAL_FX_BASE_URLS="https://primary.example",
        )
        success_body = json.dumps({"rates": {"USD": 0.7395}, "date": "2026-02-11"})

        with patch("services.fx_rates.urllib.request.urlopen") as mocked_urlopen:
            mocked_urlopen.side_effect = [
                TimeoutError("timed out"),
                _DummyResponse(success_body, status=200),
            ]
            out = fx_rates.get_fx_rate("SGD", "USD")

        self.assertIsNotNone(out)
        self.assertAlmostEqual(float(out["rate"]), 0.7395, places=6)
        self.assertEqual(mocked_urlopen.call_count, 2)

    def test_malformed_json_fails_over_to_next_provider(self) -> None:
        tmpdir = self._tmpdir()
        fx_rates = self._reload_fx(
            tmpdir,
            RENTAL_FX_BASE_URLS="https://one.example,https://two.example",
        )

        def _side_effect(req, timeout=None):
            url = req.full_url
            if "one.example" in url:
                return _DummyResponse("{not-json", status=200)
            if "two.example" in url:
                return _DummyResponse(
                    json.dumps({"rates": {"USD": 0.74}, "date": "2026-02-11"}),
                    status=200,
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with patch("services.fx_rates.urllib.request.urlopen", side_effect=_side_effect):
            out = fx_rates.get_fx_rate("SGD", "USD")

        self.assertIsNotNone(out)
        self.assertEqual(out.get("source"), "https://two.example")
        self.assertAlmostEqual(float(out["rate"]), 0.74, places=6)

    def test_provider_failover_primary_500_secondary_success(self) -> None:
        tmpdir = self._tmpdir()
        fx_rates = self._reload_fx(
            tmpdir,
            RENTAL_FX_BASE_URLS="https://primary.example,https://secondary.example",
        )

        def _side_effect(req, timeout=None):
            url = req.full_url
            if "primary.example" in url:
                raise _http_error(url, 500, "server error")
            if "secondary.example" in url:
                return _DummyResponse(
                    json.dumps({"rates": {"USD": 0.7412}, "date": "2026-02-11"}),
                    status=200,
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with patch("services.fx_rates.urllib.request.urlopen", side_effect=_side_effect):
            out = fx_rates.get_fx_rate("SGD", "USD")

        self.assertIsNotNone(out)
        self.assertEqual(out.get("source"), "https://secondary.example")
        self.assertAlmostEqual(float(out["rate"]), 0.7412, places=6)

    def test_db_persistence_survives_reload(self) -> None:
        tmpdir = self._tmpdir()
        fx_rates = self._reload_fx(
            tmpdir,
            RENTAL_FX_PERSIST="0",
            RENTAL_FX_DB_PERSIST="1",
            RENTAL_FX_BASE_URLS="https://primary.example",
            RENTAL_FX_TTL_SECONDS="3600",
        )
        with patch("services.fx_rates.urllib.request.urlopen") as mocked_urlopen:
            mocked_urlopen.return_value = _DummyResponse(
                json.dumps({"rates": {"USD": 0.7888}, "date": "2026-02-11"}),
                status=200,
            )
            first = fx_rates.get_fx_rate("SGD", "USD")
        self.assertIsNotNone(first)
        self.assertAlmostEqual(float(first["rate"]), 0.7888, places=6)

        fx_rates = self._reload_fx(
            tmpdir,
            RENTAL_FX_PERSIST="0",
            RENTAL_FX_DB_PERSIST="1",
            RENTAL_FX_BASE_URLS="https://primary.example",
            RENTAL_FX_TTL_SECONDS="3600",
        )
        with patch("services.fx_rates.urllib.request.urlopen") as mocked_urlopen:
            mocked_urlopen.side_effect = TimeoutError("offline")
            second = fx_rates.get_fx_rate("SGD", "USD")
        self.assertIsNotNone(second)
        self.assertAlmostEqual(float(second["rate"]), 0.7888, places=6)
        self.assertEqual(mocked_urlopen.call_count, 0)


if __name__ == "__main__":
    unittest.main()
