import math
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auditor.normalization import number  # noqa: E402


class NumberNormalizationTests(unittest.TestCase):
    def test_preserves_native_fractional_numbers(self):
        self.assertEqual(number(72.25), 72.25)
        self.assertEqual(number(Decimal("0.24")), 0.24)

    def test_accepts_brazilian_and_machine_decimal_strings(self):
        self.assertEqual(number("1.234,56"), 1234.56)
        self.assertEqual(number("72.25"), 72.25)
        self.assertEqual(number("0,06"), 0.06)

    def test_handles_parenthesized_negative_and_non_finite(self):
        self.assertEqual(number("(12,5)"), -12.5)
        self.assertIsNone(number(math.nan))
        self.assertIsNone(number(""))


if __name__ == "__main__":
    unittest.main()
