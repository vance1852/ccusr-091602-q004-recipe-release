"""UCUM 单位注册表与量纲换算测试。"""

import unittest

from app.errors import DimensionMismatchError, UnknownUnitError
from app.units import are_compatible, convert, dimension_of


class UnitConversionTest(unittest.TestCase):
    def test_mass_conversion(self):
        self.assertAlmostEqual(convert(1.5, "kg", "g"), 1500.0)
        self.assertAlmostEqual(convert(250, "mg", "g"), 0.25)

    def test_temperature_conversion_with_offset(self):
        self.assertAlmostEqual(convert(180.0, "Cel", "K"), 453.15)
        self.assertAlmostEqual(convert(453.15, "K", "Cel"), 180.0)
        self.assertAlmostEqual(convert(32.0, "[degF]", "Cel"), 0.0, places=6)

    def test_pressure_and_ratio(self):
        self.assertAlmostEqual(convert(6.0, "bar", "kPa"), 600.0)
        self.assertAlmostEqual(convert(80.0, "%", "1"), 0.8)

    def test_speed_conversion(self):
        self.assertAlmostEqual(convert(1500.0, "rpm", "Hz"), 25.0)

    def test_dimension_mismatch_rejected(self):
        with self.assertRaises(DimensionMismatchError):
            convert(1.0, "min", "Cel")

    def test_unknown_unit_rejected(self):
        with self.assertRaises(UnknownUnitError):
            dimension_of("furlong")
        with self.assertRaises(UnknownUnitError):
            convert(1.0, "g", "furlong")

    def test_compatibility(self):
        self.assertTrue(are_compatible("kg", "g"))
        self.assertFalse(are_compatible("kg", "L"))


if __name__ == "__main__":
    unittest.main()
