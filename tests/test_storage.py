import unittest

from optchat.storage import normalize_origin


class NormalizeOriginTests(unittest.TestCase):
    def test_unicode_is_normalized_and_keys_are_stable(self):
        origin = {"session": "caf\u00e9", "agent": "broken \ud800 value"}

        self.assertEqual(
            normalize_origin(origin),
            {"agent": "broken \ufffd value", "session": "caf\u00e9"},
        )

    def test_empty_and_overlong_values_are_rejected(self):
        with self.assertRaises(ValueError):
            normalize_origin({"project": ""})
        with self.assertRaises(ValueError):
            normalize_origin({"project": "x" * 501})

    def test_500_character_unicode_value_is_accepted(self):
        value = "\u00e9" * 500
        self.assertEqual(normalize_origin({"machine": value}), {"machine": value})
