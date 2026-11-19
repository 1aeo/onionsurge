import sys
import unittest
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from display_numbers import number, percentage


class DisplayPrecisionTests(unittest.TestCase):
    def test_subunit_zero_and_half_up_rounding_are_distinct(self):
        self.assertEqual(number(None), 'Unavailable')
        self.assertEqual(number(0), '0')
        self.assertEqual(number(.0001), '<1')
        self.assertEqual(number(-.0001), '>−1')
        self.assertEqual(number(2.5), '3')
        self.assertEqual(number(1234.5), '1,235')

    def test_percentile_rounding_does_not_cross_class_boundaries(self):
        for value, expected in [(89.9, '<90'), (94.9, '<95'), (98.9, '<99'), (99.9, '>99'), (100, '100')]:
            self.assertEqual(percentage(value, suffix=False, boundaries=(90, 95, 99, 100)), expected)
        self.assertEqual(percentage(Fraction(99999, 1000)), '>99%')
        self.assertEqual(percentage(Fraction(49999, 1000)), '<50%')

    def test_large_exact_counters_do_not_depend_on_decimal_context(self):
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(number(Decimal('123456789012345678901234567890'), grouping=False), '123456789012345678901234567890')

    def test_invalid_numbers_cannot_be_rendered_as_measurements(self):
        for value in [True, float('inf'), float('nan'), 'not numeric']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                number(value)
        for value in [-1, 101]:
            with self.assertRaises(ValueError):
                percentage(value)

    def test_huge_decimal_and_fraction_exponents_fail_fast(self):
        for z in ('0e-10000', '0e+10000', '0e1001'):
            self.assertEqual(number(Decimal(z)), '0')
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(number(Decimal('0e+10000')), '0')
            self.assertEqual(len(number(Decimal('1e1000'), grouping=False)), 1001)
            self.assertEqual(len(number(Fraction(10**1000, 1), grouping=False)), 1001)
        for over in (
            Decimal('1.1e1000'),
            Fraction(11 * 10**999, 1),
            Decimal('-1.1e1000'),
            Fraction(-11 * 10**999, 1),
            Decimal('1' + '0' * 999 + '1'),
            Fraction(10**1000 + 1, 1),
            Decimal('1e999999999'),
            Decimal('-1e999999999'),
            Fraction(10**1001, 1),
        ):
            with self.subTest(over=type(over).__name__), self.assertRaisesRegex(ValueError, 'supported range'):
                number(over)
        with self.assertRaisesRegex(ValueError, 'supported range'):
            percentage(Decimal('1e999999999'))


if __name__ == '__main__':
    unittest.main()
