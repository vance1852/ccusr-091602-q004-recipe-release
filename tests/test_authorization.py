"""开批授权：唯一适用版本、灰度外拒绝、过期/篡改/撤销拒绝。"""

import copy
import unittest

from app.crypto import tamper
from app.errors import AuthorizationDenied
from tests.test_release import FakeClock, ReleaseScenario


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.s = ReleaseScenario()
        self.p = self.s.p
        self.clock: FakeClock = self.s.clock
        self.rel = self.s.release(targets=["DEV-CN-01", "DEV-CB-01"],
                                  wave_order=["canary", "broad"])
        self.p.releases.start(self.rel.release_id, "u_admin")
        self.p.releases.collect_wave(self.rel.release_id)  # canary 生效

    def test_grant_for_in_scope_device(self):
        token = self.p.authorizer.issue_grant(
            "DEV-CN-01", "PRD-BAT-01", "op-001")
        info = self.p.authorizer.authorize_batch(token)
        self.assertEqual(info["device_id"], "DEV-CN-01")
        self.assertEqual(info["release_id"], self.rel.release_id)

    def test_out_of_grayscale_device_denied_at_issue(self):
        with self.assertRaises(AuthorizationDenied):
            self.p.authorizer.issue_grant(
                "DEV-CB-01", "PRD-BAT-01", "op-001")

    def test_expired_grant_denied(self):
        token = self.p.authorizer.issue_grant(
            "DEV-CN-01", "PRD-BAT-01", "op-001", ttl_seconds=600)
        self.clock.advance(601)
        with self.assertRaises(AuthorizationDenied) as ctx:
            self.p.authorizer.authorize_batch(token)
        self.assertIn("过期", str(ctx.exception))

    def test_revoked_grant_denied(self):
        token = self.p.authorizer.issue_grant(
            "DEV-CN-01", "PRD-BAT-01", "op-001")
        self.p.authorizer.revoke(token["grant_id"])
        with self.assertRaises(AuthorizationDenied):
            self.p.authorizer.authorize_batch(token)

    def test_tampered_grant_denied(self):
        token = self.p.authorizer.issue_grant(
            "DEV-CN-01", "PRD-BAT-01", "op-001")
        broken = tamper(token, device_id="DEV-CN-02")
        with self.assertRaises(AuthorizationDenied):
            self.p.authorizer.authorize_batch(broken)

    def test_grant_invalidated_by_rollback(self):
        token = self.p.authorizer.issue_grant(
            "DEV-CN-01", "PRD-BAT-01", "op-001")
        # 回滚目标：研发库登记的已知良好基线内容摘要
        from tests.test_validation import base_params
        baseline = self.p.store.put("recipe_content", {
            "recipe_code": "RCP-LFP-01", "product_code": "PRD-BAT-01",
            "parameters": base_params(
                curing_temp={"value": 140, "unit": "degC"}),
        })
        rb = self.p.releases.rollback(
            self.rel.release_id, "u_admin", "异常撤回",
            restore_digest=baseline.digest)
        with self.assertRaises(AuthorizationDenied):
            self.p.authorizer.authorize_batch(token)
        # 回滚版本生效后必须重新领取授权
        self.p.releases.start(rb.release_id, "u_admin")
        self.p.releases.collect_wave(rb.release_id)
        self.p.releases.promote_wave(rb.release_id, "u_admin")
        new_token = self.p.authorizer.issue_grant(
            "DEV-CN-01", "PRD-BAT-01", "op-001")
        info = self.p.authorizer.authorize_batch(new_token)
        self.assertEqual(info["release_id"], rb.release_id)

    def test_grant_invalidated_when_new_version_supersedes(self):
        token = self.p.authorizer.issue_grant(
            "DEV-CN-01", "PRD-BAT-01", "op-001")
        from tests.test_validation import base_params
        chg2 = self.p.recipes.create_draft(
            "u_gongyi", "RCP-LFP-01", "PRD-BAT-01",
            base_params(curing_temp={"value": 145, "unit": "degC"}))
        self.p.recipes.submit(chg2.change_id, "u_gongyi")
        self.p.recipes.technical_review(chg2.change_id, "u_zhang", True)
        self.p.recipes.quality_countersign(chg2.change_id, "u_quality", True)
        r2 = self.p.releases.create_release(
            chg2.change_id, "u_admin", targets=["DEV-CN-01"],
            wave_order=["canary"])
        self.p.releases.start(r2.release_id, "u_admin")
        self.p.releases.collect_wave(r2.release_id)
        self.p.releases.promote_wave(r2.release_id, "u_admin")
        # 旧授权引用的 release 已不是当前适用版本
        with self.assertRaises(AuthorizationDenied):
            self.p.authorizer.authorize_batch(token)

    def test_unknown_device_at_issue(self):
        with self.assertRaises(Exception):
            self.p.authorizer.issue_grant("DEV-NOPE", "PRD-BAT-01",
                                          "op-001")


if __name__ == "__main__":
    unittest.main()
