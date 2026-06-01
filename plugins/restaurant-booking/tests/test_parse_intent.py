#!/usr/bin/env python3
"""test_parse_intent.py - Unit tests for the intent parser with mocked Haiku API.

Run:
    python3 -m pytest plugins/restaurant-booking/tests/test_parse_intent.py -v
    # or directly:
    python3 plugins/restaurant-booking/tests/test_parse_intent.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the parse-booking-intent module is importable
PLUGIN_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

# Import the module under test
# The module name has hyphens, so we use importlib
import importlib.util

spec = importlib.util.spec_from_file_location(
    "parse_booking_intent",
    str(PLUGIN_SCRIPTS / "parse-booking-intent.py"),
)
_module = importlib.util.module_from_spec(spec)  # type: ignore
spec.loader.exec_module(_module)  # type: ignore

_validate_result = _module._validate_result
_call_haiku = _module._call_haiku
parse_intent = _module.parse_intent


class TestValidateResult(unittest.TestCase):
    """Test the _validate_result schema validator - no network calls."""

    def _base_result(self) -> dict:
        return {
            "restaurant_name": "Contrabando Saldanha",
            "restaurant_url_hint": None,
            "datetime_iso": "2026-05-15T13:00:00+01:00",
            "party_size": 4,
            "notes": None,
            "intent_confidence": 0.92,
        }

    def test_valid_result_passes(self):
        result = _validate_result(self._base_result())
        self.assertEqual(result["restaurant_name"], "Contrabando Saldanha")
        self.assertEqual(result["party_size"], 4)
        self.assertAlmostEqual(result["intent_confidence"], 0.92)

    def test_missing_restaurant_raises(self):
        data = self._base_result()
        del data["restaurant_name"]
        with self.assertRaises(ValueError):
            _validate_result(data)

    def test_missing_datetime_raises(self):
        data = self._base_result()
        del data["datetime_iso"]
        with self.assertRaises(ValueError):
            _validate_result(data)

    def test_party_size_coerced_from_string(self):
        data = self._base_result()
        data["party_size"] = "6"
        result = _validate_result(data)
        self.assertEqual(result["party_size"], 6)
        self.assertIsInstance(result["party_size"], int)

    def test_confidence_clamped_above_1(self):
        data = self._base_result()
        data["intent_confidence"] = 1.5
        result = _validate_result(data)
        self.assertEqual(result["intent_confidence"], 1.0)

    def test_confidence_clamped_below_0(self):
        data = self._base_result()
        data["intent_confidence"] = -0.1
        result = _validate_result(data)
        self.assertEqual(result["intent_confidence"], 0.0)

    def test_default_fields_added(self):
        data = {
            "restaurant_name": "Tasca",
            "datetime_iso": "2026-05-15T19:00:00+01:00",
            "party_size": 2,
            "intent_confidence": 0.85,
        }
        result = _validate_result(data)
        self.assertIsNone(result["restaurant_url_hint"])
        self.assertIsNone(result["notes"])

    def test_url_hint_preserved(self):
        data = self._base_result()
        data["restaurant_url_hint"] = "https://www.thefork.com/restaurant/contrabando-saldanha-r832103"
        result = _validate_result(data)
        self.assertIn("thefork.com", result["restaurant_url_hint"])


class TestParseIntentMocked(unittest.TestCase):
    """Test parse_intent with the Haiku API mocked out."""

    def _mock_haiku_response(self, payload: dict):
        """Patch _call_haiku to return the given payload."""
        return patch.object(_module, "_call_haiku", return_value=payload)

    def test_full_request_parses_correctly(self):
        mock_response = {
            "restaurant_name": "Contrabando Saldanha",
            "restaurant_url_hint": "https://www.thefork.com/restaurant/contrabando-restaurante-e-bar-saldanha-r832103",
            "datetime_iso": "2026-05-15T13:00:00+01:00",
            "party_size": 4,
            "notes": None,
            "intent_confidence": 0.95,
        }
        with self._mock_haiku_response(mock_response):
            result = parse_intent("book Contrabando Saldanha for 4 tomorrow at 1pm")

        self.assertEqual(result["restaurant_name"], "Contrabando Saldanha")
        self.assertEqual(result["party_size"], 4)
        self.assertGreater(result["intent_confidence"], 0.7)

    def test_low_confidence_request(self):
        mock_response = {
            "restaurant_name": "some restaurant",
            "restaurant_url_hint": None,
            "datetime_iso": "2026-05-14T13:00:00+01:00",
            "party_size": 2,
            "notes": None,
            "intent_confidence": 0.45,
        }
        with self._mock_haiku_response(mock_response):
            result = parse_intent("book somewhere for lunch")

        self.assertLess(result["intent_confidence"], 0.7)

    def test_request_with_notes(self):
        mock_response = {
            "restaurant_name": "Contrabando 24 de Julho",
            "restaurant_url_hint": None,
            "datetime_iso": "2026-05-15T20:00:00+01:00",
            "party_size": 6,
            "notes": "window table please",
            "intent_confidence": 0.88,
        }
        with self._mock_haiku_response(mock_response):
            result = parse_intent(
                "book Contrabando 24 de Julho for 6 tomorrow at 8pm, window table please"
            )

        self.assertEqual(result["notes"], "window table please")
        self.assertEqual(result["party_size"], 6)

    def test_haiku_returns_prose_raises_runtime_error(self):
        with patch.object(
            _module,
            "_call_haiku",
            side_effect=RuntimeError("Haiku returned non-JSON text"),
        ):
            with self.assertRaises(RuntimeError):
                parse_intent("book something")

    def test_schema_error_from_haiku_raises_value_error(self):
        mock_response = {
            # Missing restaurant_name
            "datetime_iso": "2026-05-15T13:00:00+01:00",
            "party_size": 4,
            "intent_confidence": 0.9,
        }
        with self._mock_haiku_response(mock_response):
            with self.assertRaises(ValueError):
                parse_intent("book something for 4 at 1pm")


class TestExtractNameFromUrl(unittest.TestCase):
    """Test the URL slug parser in book-restaurant.py."""

    def setUp(self):
        br_spec = importlib.util.spec_from_file_location(
            "book_restaurant",
            str(PLUGIN_SCRIPTS / "book-restaurant.py"),
        )
        self._br = importlib.util.module_from_spec(br_spec)  # type: ignore
        br_spec.loader.exec_module(self._br)  # type: ignore

    def test_contrabando_saldanha(self):
        url = "https://www.thefork.com/restaurant/contrabando-restaurante-e-bar-saldanha-r832103"
        name = self._br._extract_name_from_url(url)
        self.assertIn("Contrabando", name)
        self.assertNotIn("832103", name)

    def test_contrabando_24_julho(self):
        url = "https://www.thefork.com/restaurant/contrabando-restaurante-e-bar-24-de-julho-r362875"
        name = self._br._extract_name_from_url(url)
        self.assertIn("Contrabando", name)
        self.assertNotIn("362875", name)

    def test_unknown_url_returns_fallback(self):
        name = self._br._extract_name_from_url("https://example.com/something")
        self.assertEqual(name, "Restaurant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
