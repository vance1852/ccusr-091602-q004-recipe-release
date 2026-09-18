"""配方编辑期校验：量纲、范围、参数依赖。"""

import unittest

from app.errors import (
    DependencyViolationError,
    DimensionMismatchError,
    RangeViolationError,
    UnknownParameterError,
)
from app.recipe import (
    ParameterValue,
    canonical_content,
    content_digest,
    validate_draft,
)

from helpers import build_world, make_draft


class RecipeValidationTest(unittest.TestCase):
    def setUp(self):
        self.catalog, *_ = build_world()

    def test_valid_draft_passes(self):
        validate_draft(make_draft(), self.catalog)  # 不抛异常即通过

    def test_dimension_mismatch(self):
        draft = make_draft()
        draft.params["mix.temperature"] = ParameterValue(45.0, "min")  # 温度填了时间单位
        with self.assertRaises(DimensionMismatchError):
            validate_draft(draft, self.catalog)

    def test_range_violation(self):
        with self.assertRaises(RangeViolationError):
            validate_draft(make_draft(mix_temp=300.0), self.catalog)  # 上限 250 Cel

    def test_range_checked_after_unit_conversion(self):
        draft = make_draft()
        draft.params["mix.temperature"] = ParameterValue(500.0, "K")  # = 226.85 Cel，合法
        validate_draft(draft, self.catalog)
        draft.params["mix.temperature"] = ParameterValue(600.0, "K")  # = 326.85 Cel，超限
        with self.assertRaises(RangeViolationError):
            validate_draft(draft, self.catalog)

    def test_dependency_cool_below_mix(self):
        with self.assertRaises(DependencyViolationError):
            validate_draft(make_draft(mix_temp=60.0, cool_temp=70.0), self.catalog)

    def test_dependency_total_cycle(self):
        draft = make_draft()
        draft.params["mix.duration"] = ParameterValue(240.0, "min")
        draft.params["cool.duration"] = ParameterValue(400.0, "min")  # 合计 640 > 600
        with self.assertRaises(DependencyViolationError):
            validate_draft(draft, self.catalog)

    def test_dependency_heavy_dose_needs_stir(self):
        draft = make_draft()
        draft.params["dose.mass"] = ParameterValue(150.0, "kg")
        draft.params["stir.speed"] = ParameterValue(50.0, "rpm")  # 重投料但转速过低
        with self.assertRaises(DependencyViolationError):
            validate_draft(draft, self.catalog)

    def test_unknown_parameter(self):
        draft = make_draft()
        draft.params["oven.temperature"] = ParameterValue(100.0, "Cel")
        with self.assertRaises(UnknownParameterError):
            validate_draft(draft, self.catalog)

    def test_equivalent_units_yield_same_digest(self):
        """同一物理配方用不同等价单位录入，规范化内容摘要必须一致。"""
        draft_c = make_draft(mix_temp=180.0)
        draft_k = make_draft(mix_temp=180.0)
        draft_k.params["mix.temperature"] = ParameterValue(453.15, "K")
        self.assertEqual(
            content_digest(draft_c, self.catalog),
            content_digest(draft_k, self.catalog),
        )

    def test_canonical_content_uses_declared_units(self):
        draft = make_draft()
        draft.params["dose.mass"] = ParameterValue(1500.0, "g")
        content = canonical_content(draft, self.catalog)
        self.assertEqual(content["params"]["dose.mass"], {"unit": "kg", "value": 1.5})


if __name__ == "__main__":
    unittest.main()
