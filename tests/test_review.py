"""审核工作流：职责分离、高风险会签、签名防篡改。"""

import unittest

from app.errors import ReviewStateError, SelfApprovalError
from app.recipe import ParameterValue
from app.review import (
    ROLE_PROCESS_REVIEWER,
    ROLE_QUALITY_LEAD,
    ReviewRecord,
    ReviewState,
)

from helpers import AUTHOR, NOW, QUALITY_LEAD, TECH_REVIEWER, build_world, make_draft


class ReviewWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.catalog, self.keystore, *_ = build_world()

    def _submitted(self, draft=None):
        review = ReviewRecord(draft or make_draft(), self.catalog)
        review.submit(AUTHOR, NOW)
        return review

    def test_high_risk_recipe_requires_quality_cosign(self):
        review = self._submitted()  # 默认配方含高风险参数 mix.temperature / mix.pressure
        self.assertTrue(review.requires_quality)
        review.approve(self.keystore, TECH_REVIEWER, ROLE_PROCESS_REVIEWER, NOW)
        self.assertEqual(review.state, ReviewState.QUALITY_REVIEW)  # 技术批准后不能直接生效
        review.approve(self.keystore, QUALITY_LEAD, ROLE_QUALITY_LEAD, NOW)
        self.assertEqual(review.state, ReviewState.APPROVED)
        self.assertTrue(review.verify_chain(self.keystore))
        self.assertEqual(len(review.decisions), 2)

    def test_standard_recipe_skips_quality_review(self):
        draft = make_draft()
        for key in ("mix.temperature", "mix.pressure"):  # 去掉高风险参数
            del draft.params[key]
        review = ReviewRecord(draft, self.catalog)
        self.assertFalse(review.requires_quality)
        review.submit(AUTHOR, NOW)
        review.approve(self.keystore, TECH_REVIEWER, ROLE_PROCESS_REVIEWER, NOW)
        self.assertEqual(review.state, ReviewState.APPROVED)

    def test_reviewer_cannot_approve_own_change(self):
        review = self._submitted()
        with self.assertRaises(SelfApprovalError):
            review.approve(self.keystore, AUTHOR, ROLE_PROCESS_REVIEWER, NOW)

    def test_quality_cosigner_must_differ_from_technical_reviewer(self):
        review = self._submitted()
        review.approve(self.keystore, TECH_REVIEWER, ROLE_PROCESS_REVIEWER, NOW)
        with self.assertRaises(SelfApprovalError):
            review.approve(self.keystore, TECH_REVIEWER, ROLE_QUALITY_LEAD, NOW)

    def test_wrong_role_rejected(self):
        review = self._submitted()
        with self.assertRaises(ReviewStateError):
            review.approve(self.keystore, QUALITY_LEAD, ROLE_QUALITY_LEAD, NOW)  # 越级会签

    def test_reject_flow(self):
        review = self._submitted()
        review.reject(self.keystore, TECH_REVIEWER, ROLE_PROCESS_REVIEWER, NOW, "温度窗口过宽")
        self.assertEqual(review.state, ReviewState.REJECTED)
        with self.assertRaises(ReviewStateError):
            review.approve(self.keystore, TECH_REVIEWER, ROLE_PROCESS_REVIEWER, NOW)

    def test_submit_only_by_author(self):
        review = ReviewRecord(make_draft(), self.catalog)
        with self.assertRaises(ReviewStateError):
            review.submit(TECH_REVIEWER, NOW)

    def test_tampered_decision_detected(self):
        """模拟签名篡改：改动已签名决定的载荷后，审核链验签必须失败。"""
        review = self._submitted()
        review.approve(self.keystore, TECH_REVIEWER, ROLE_PROCESS_REVIEWER, NOW)
        review.approve(self.keystore, QUALITY_LEAD, ROLE_QUALITY_LEAD, NOW)
        self.assertTrue(review.verify_chain(self.keystore))

        review.decisions[0].message.payload["content_digest"] = "0" * 64  # 篡改绑定摘要
        self.assertFalse(review.verify_chain(self.keystore))

        review2 = self._submitted()
        review2.approve(self.keystore, TECH_REVIEWER, ROLE_PROCESS_REVIEWER, NOW)
        review2.decisions[0].message.signature = "ff" * 32  # 篡改签名值
        self.assertFalse(review2.verify_chain(self.keystore))


if __name__ == "__main__":
    unittest.main()
