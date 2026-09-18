"""回滚语义：回滚创建引用旧内容的新发布，而非覆盖。"""

import unittest

from app.platform import Platform
from app.release import STATE_ROLLED_BACK
from tests.test_release import ReleaseScenario
from tests.test_validation import base_params


class RollbackTests(unittest.TestCase):
    def _deploy_full(self, scenario, targets=None, waves=None):
        rel = scenario.release(targets=targets, wave_order=waves)
        scenario.p.releases.start(rel.release_id, "u_admin")
        for _ in range(len(rel.wave_order)):
            scenario.p.releases.collect_wave(rel.release_id)
            scenario.p.releases.promote_wave(rel.release_id, "u_admin")
        return rel

    def test_rollback_creates_new_release_referencing_old_content(self):
        s = ReleaseScenario()
        r1 = self._deploy_full(s)
        v1 = r1.content_digest

        # 新版本：调整固化温度，形成 v2
        chg2 = s.p.recipes.create_draft(
            "u_gongyi", "RCP-LFP-01", "PRD-BAT-01",
            base_params(curing_temp={"value": 160, "unit": "degC"}))
        s.p.recipes.submit(chg2.change_id, "u_gongyi")
        s.p.recipes.technical_review(chg2.change_id, "u_zhang", True)
        s.p.recipes.quality_countersign(chg2.change_id, "u_quality", True)
        r2 = s.p.releases.create_release(chg2.change_id, "u_admin")
        s.p.releases.start(r2.release_id, "u_admin")
        s.p.releases.collect_wave(r2.release_id)  # canary 生效 v2
        v2 = r2.content_digest
        self.assertNotEqual(v1, v2)
        # 此时 canary 是 v2，broad/prep 仍是 v1
        self.assertEqual(
            s.p.releases.active_version("DEV-CN-01", "PRD-BAT-01")
            .content_digest, v2)
        self.assertEqual(
            s.p.releases.active_version("DEV-CB-01", "PRD-BAT-01")
            .content_digest, v1)

        # 回滚 r2 → 新发布引用 v1
        r3 = s.p.releases.rollback(r2.release_id, "u_admin",
                                  "试点批次外观异常")
        self.assertEqual(r2.state, STATE_ROLLED_BACK)
        self.assertNotEqual(r3.release_id, r2.release_id)
        self.assertEqual(r3.rollback_of, r2.release_id)
        self.assertEqual(r3.restores_digest, v1)
        self.assertEqual(r3.content_digest, v1)
        # 回滚发布默认只面向被 r2 改动过的设备
        self.assertEqual(set(r3.targets), {"DEV-CN-01", "DEV-CN-02"})

        # r2 的版本标记已失效；在回滚发布生效前 canary 暂无适用版本
        self.assertIsNone(
            s.p.releases.active_version("DEV-CN-01", "PRD-BAT-01"))
        # 未受影响的 broad/prep 继续保留 v1
        self.assertEqual(
            s.p.releases.active_version("DEV-CB-01", "PRD-BAT-01")
            .content_digest, v1)

        # 走完回滚发布
        s.p.releases.start(r3.release_id, "u_admin")
        for _ in range(len(r3.wave_order)):
            s.p.releases.collect_wave(r3.release_id)
            s.p.releases.promote_wave(r3.release_id, "u_admin")
        self.assertEqual(r3.state, "completed")
        self.assertEqual(
            s.p.releases.active_version("DEV-CN-01", "PRD-BAT-01")
            .content_digest, v1)
        self.assertEqual(
            s.p.releases.active_version("DEV-CN-01", "PRD-BAT-01")
            .release_id, r3.release_id)

    def test_rollback_to_same_digest_rejected(self):
        s = ReleaseScenario()
        r1 = self._deploy_full(s)
        with self.assertRaises(Exception):
            s.p.releases.rollback(
                r1.release_id, "u_admin", "x",
                restore_digest=r1.content_digest)

    def test_explicit_restore_digest_must_exist(self):
        s = ReleaseScenario()
        r1 = self._deploy_full(s)
        from app.errors import NotFoundError
        with self.assertRaises(NotFoundError):
            s.p.releases.rollback(
                r1.release_id, "u_admin", "x",
                restore_digest="sha256:" + "ab" * 32)


if __name__ == "__main__":
    unittest.main()
