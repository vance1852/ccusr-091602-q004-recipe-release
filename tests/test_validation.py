"""配方内容校验：量纲、范围、必填与参数依赖。"""

import unittest

from app.catalog import default_catalog
from app.errors import ValidationError


def base_params(**overrides):
    params = {
        "mix_mode": {"value": "normal", "unit": None},
        "coating_speed": {"value": 5000, "unit": "mm/min"},
        "coating_gap": {"value": 120, "unit": "um"},
        "drying_temp": {"value": 80, "unit": "degC"},
        "curing_temp": {"value": 150, "unit": "degC"},
        "curing_time": {"value": 10, "unit": "min"},
        "slurry_flow": {"value": 2.5, "unit": "L/min"},
        "mixing_speed": {"value": 600, "unit": "rpm"},
        "mixing_time": {"value": 30, "unit": "min"},
        "solvent_ratio": {"value": 5, "unit": "%"},
    }
    params.update(overrides)
    return params


class CatalogValidationTests(unittest.TestCase):
    def setUp(self):
        self.cat = default_catalog()

    def test_valid_recipe_passes(self):
        self.assertEqual(self.cat.validate_recipe(base_params()), [])

    def test_unknown_parameter(self):
        issues = self.cat.validate_recipe(base_params(
            bogus={"value": 1, "unit": "1"}))
        self.assertTrue(any("未知参数" in i for i in issues))

    def test_wrong_dimension(self):
        issues = self.cat.validate_recipe(base_params(
            drying_temp={"value": 80, "unit": "kPa"}))
        self.assertTrue(any("量纲错误" in i for i in issues), issues)

    def test_range_violation_in_other_unit(self):
        # 450 °F ≈ 232.2 °C，超出 curing_temp 上限 220 °C（用华氏度给出）
        issues = self.cat.validate_recipe(base_params(
            curing_temp={"value": 450, "unit": "degF"}))
        self.assertTrue(any("高于上限" in i for i in issues), issues)

    def test_missing_mandatory(self):
        p = base_params()
        del p["mix_mode"]
        issues = self.cat.validate_recipe(p)
        self.assertTrue(any("缺少必填参数" in i for i in issues))

    def test_enum_choice(self):
        issues = self.cat.validate_recipe(base_params(
            mix_mode={"value": "turbo", "unit": None}))
        self.assertTrue(any("不在允许取值" in i for i in issues))

    def test_requires_dependency(self):
        p = base_params()
        del p["curing_time"]
        issues = self.cat.validate_recipe(p)
        self.assertTrue(any("curing_time" in i for i in issues), issues)

    def test_conditional_dependency_vacuum(self):
        issues = self.cat.validate_recipe(base_params(
            mix_mode={"value": "vacuum", "unit": None}))
        # vacuum 模式必须有 vacuum_pressure
        self.assertTrue(any("vacuum_pressure" in i for i in issues), issues)
        ok = base_params(
            mix_mode={"value": "vacuum", "unit": None},
            vacuum_enabled={"value": "on", "unit": None},
            vacuum_pressure={"value": -90, "unit": "kPa"})
        self.assertEqual(self.cat.validate_recipe(ok), [])

    def test_cross_param_relation(self):
        issues = self.cat.validate_recipe(base_params(
            drying_temp={"value": 170, "unit": "degC"},
            curing_temp={"value": 120, "unit": "degC"}))
        self.assertTrue(any("参数关系不满足" in i for i in issues), issues)

    def test_non_numeric_value(self):
        issues = self.cat.validate_recipe(base_params(
            mixing_speed={"value": "fast", "unit": "rpm"}))
        self.assertTrue(any("必须是数值" in i for i in issues))

    def test_submit_raises_validation_error(self):
        from app.content import ContentStore
        from app.crypto import KeyRing
        from app.identity import default_directory
        from app.recipe import RecipeWorkflow

        wf = RecipeWorkflow(self.cat, ContentStore(), default_directory(),
                            KeyRing())
        chg = wf.create_draft("u_gongyi", "R1", "P1",
                              base_params(curing_temp={"value": 999,
                                                       "unit": "degC"}))
        with self.assertRaises(ValidationError) as ctx:
            wf.submit(chg.change_id, "u_gongyi")
        self.assertTrue(any("curing_temp" in i for i in ctx.exception.issues))


if __name__ == "__main__":
    unittest.main()
