"""量纲与单位换算测试。"""

import math
import unittest

from app import units
from app.units import UnitError, compatible, convert


class UnitTests(unittest.TestCase):
    def test_temperature_with_offset(self):
        self.assertAlmostEqual(convert(0, "degC", "K"), 273.15)
        self.assertAlmostEqual(convert(100, "degC", "K"), 373.15)
        self.assertAlmostEqual(convert(32, "degF", "degC"), 0.0, places=6)
        self.assertAlmostEqual(convert(212, "degF", "degC"), 100.0, places=6)

    def test_pressure_family(self):
        self.assertAlmostEqual(convert(1, "MPa", "kPa"), 1000.0)
        self.assertAlmostEqual(convert(1, "bar", "kPa"), 100.0)
        self.assertAlmostEqual(convert(14.6959488, "psi", "bar"), 1.01325,
                               places=4)
        self.assertAlmostEqual(convert(-0.9, "bar", "kPa"), -90.0)

    def test_time_and_speed(self):
        self.assertEqual(convert(1, "h", "s"), 3600.0)
        self.assertAlmostEqual(convert(6000, "mm/min", "m/s"), 0.1)
        self.assertEqual(convert(1200, "rpm", "rps"), 20.0)

    def test_dimensionless(self):
        self.assertEqual(convert(5, "%", "1"), 0.05)
        self.assertEqual(convert(10000, "ppm", "%"), 1.0)

    def test_dimension_mismatch_rejected(self):
        with self.assertRaises(UnitError):
            convert(1, "degC", "kPa")
        with self.assertRaises(UnitError):
            convert(1, "rpm", "L/min")
        self.assertFalse(compatible("kg", "L"))
        self.assertTrue(compatible("g", "kg"))

    def test_unknown_unit(self):
        with self.assertRaises(UnitError):
            units.get_unit("furlong")


if __name__ == "__main__":
    unittest.main()
