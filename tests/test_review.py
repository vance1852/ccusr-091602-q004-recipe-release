"""审核链：禁止自审、高风险质量会签、驳回、签名篡改。"""

import copy
import unittest

from app.errors import ReviewError, SignatureError, WorkflowError
from app.platform import Platform
from tests.test_validation import base_params


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.p = Platform()

    def _draft(self, high_risk=True, **kw):
        params = base_params(**kw)
        chg = self.p.recipes.create_draft(
            "u_gongyi", "RCP-LFP-01", "PRD-BAT-01", params)
        return chg

    def test_low_risk_skips_quality(self):
        # curing_temp 与 solvent_ratio 均为高风险参数，低风险配方需同时去除
        params = base_params()
        del params["solvent_ratio"]
        del params["curing_temp"]
        del params["curing_time"]
        chg = self.p.recipes.create_draft(
            "u_gongyi", "RCP-LFP-01", "PRD-BAT-01", params)
        self.p.recipes.submit(chg.change_id, "u_gongyi")
        self.p.recipes.technical_review(chg.change_id, "u_zhang", True)
        self.assertEqual(self.p.recipes.get(chg.change_id).state, "approved")

    def test_high_risk_requires_quality_countersign(self):
        chg = self._draft()
        self.p.recipes.submit(chg.change_id, "u_gongyi")
        self.p.recipes.technical_review(chg.change_id, "u_zhang", True)
        self.assertEqual(self.p.recipes.get(chg.change_id).state,
                         "quality_review")
        self.p.recipes.quality_countersign(chg.change_id, "u_quality", True)
        self.assertEqual(self.p.recipes.get(chg.change_id).state, "approved")

    def test_reviewer_cannot_approve_own_change(self):
        # u_zhang 同时具备 engineer 与 reviewer 角色
        chg = self.p.recipes.create_draft(
            "u_zhang", "RCP-LFP-01", "PRD-BAT-01", base_params())
        self.p.recipes.submit(chg.change_id, "u_zhang")
        with self.assertRaises(ReviewError):
            self.p.recipes.technical_review(chg.change_id, "u_zhang", True)

    def test_quality_cannot_countersign_own_change(self):
        # 给质量负责人临时加上工程师身份由其提交
        from app.identity import User, ROLE_ENGINEER
        self.p.directory.add(User("u_q2", "钱工",
                                  (ROLE_ENGINEER, "quality")))
        self.p.key_ring.register("key:u_q2", b"secret-u_q2")
        chg = self.p.recipes.create_draft(
            "u_q2", "RCP-LFP-01", "PRD-BAT-01", base_params())
        self.p.recipes.submit(chg.change_id, "u_q2")
        self.p.recipes.technical_review(chg.change_id, "u_zhang", True)
        with self.assertRaises(ReviewError):
            self.p.recipes.quality_countersign(chg.change_id, "u_q2", True)

    def test_non_reviewer_role_rejected(self):
        chg = self._draft()
        self.p.recipes.submit(chg.change_id, "u_gongyi")
        with self.assertRaises(ReviewError):
            self.p.recipes.technical_review(chg.change_id, "u_gongyi", True)
        with self.assertRaises(ReviewError):
            self.p.recipes.technical_review(chg.change_id, "u_admin", True)

    def test_technical_reject(self):
        chg = self._draft()
        self.p.recipes.submit(chg.change_id, "u_gongyi")
        self.p.recipes.technical_review(chg.change_id, "u_zhang", False,
                                        "参数存疑")
        self.assertEqual(self.p.recipes.get(chg.change_id).state, "rejected")

    def test_quality_reject(self):
        chg = self._draft()
        self.p.recipes.submit(chg.change_id, "u_gongyi")
        self.p.recipes.technical_review(chg.change_id, "u_zhang", True)
        self.p.recipes.quality_countersign(chg.change_id, "u_quality",
                                           False, "高风险论证不足")
        self.assertEqual(self.p.recipes.get(chg.change_id).state, "rejected")

    def test_wrong_state_transitions(self):
        chg = self._draft()
        with self.assertRaises(WorkflowError):
            self.p.recipes.technical_review(chg.change_id, "u_zhang", True)
        self.p.recipes.submit(chg.change_id, "u_gongyi")
        with self.assertRaises(WorkflowError):
            self.p.recipes.quality_countersign(chg.change_id, "u_quality",
                                               True)

    def test_signed_review_tampering_detected(self):
        chg = self._draft()
        self.p.recipes.submit(chg.change_id, "u_gongyi")
        self.p.recipes.technical_review(chg.change_id, "u_zhang", True,
                                        "同意")
        # 篡改已落账的审核消息
        change = self.p.recipes.get(chg.change_id)
        original = change.signatures[0]
        change.signatures[0] = copy.deepcopy(original)
        change.signatures[0]["decision"] = "rejected"
        results = self.p.recipes.verify_chain(chg.change_id)
        self.assertFalse(results[0]["valid"])
        self.assertTrue(any(not r["valid"] for r in results))

    def test_signed_review_chain_intact(self):
        chg = self._draft()
        self.p.recipes.submit(chg.change_id, "u_gongyi")
        self.p.recipes.technical_review(chg.change_id, "u_zhang", True)
        self.p.recipes.quality_countersign(chg.change_id, "u_quality", True)
        results = self.p.recipes.verify_chain(chg.change_id)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["valid"] for r in results))


if __name__ == "__main__":
    unittest.main()
