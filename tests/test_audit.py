"""一次发布记录全景视图测试。"""

import unittest

from app.audit import build_release_record, format_record
from app.devices import SimulatedController
from tests.test_release import ReleaseScenario


class AuditRecordTests(unittest.TestCase):
    def test_record_shows_chain_devices_disposition_scope(self):
        s = ReleaseScenario()
        rel = s.release(targets=["DEV-CN-01", "DEV-CN-02", "DEV-CB-01"],
                        wave_order=["canary", "broad"])
        s.p.releases.start(rel.release_id, "u_admin")
        # CN-02 成功，CN-01 摘要不一致 → 局部失败
        s.p.gateway.controller("DEV-CN-01").set_mode(
            SimulatedController.MODE_DIGEST_ALARM)
        s.p.releases.collect_wave(rel.release_id)

        rec = build_release_record(s.p.releases, s.p.recipes, rel.release_id)
        self.assertEqual(rec["content_digest"], rel.content_digest)
        # 审核链完整可验
        self.assertEqual({c["by"] for c in rec["review_chain"]},
                         {"u_zhang", "u_quality"})
        self.assertTrue(all(c["valid"] for c in rec["review_chain"]))
        # 设备指令/回执齐全
        by_dev = {d["device_id"]: d for d in rec["devices"]}
        self.assertEqual(by_dev["DEV-CN-01"]["status"], "digest_mismatch")
        self.assertIsNotNone(by_dev["DEV-CN-01"]["command_id"])
        self.assertIsNotNone(by_dev["DEV-CN-01"]["receipt_id"])
        self.assertEqual(by_dev["DEV-CN-02"]["status"], "applied")
        self.assertIsNone(by_dev["DEV-CB-01"]["command_id"])
        # 处置信息
        disp = rec["local_failure_disposition"]
        self.assertTrue(disp["halted"])
        self.assertEqual(disp["failed_devices"], ["DEV-CN-01"])
        self.assertEqual(disp["already_applied_kept"], ["DEV-CN-02"])
        # 当前适用范围
        scope = {r["device_id"]: r for r in rec["current_applicability"]}
        self.assertTrue(scope["DEV-CN-02"]["is_this_release"])
        self.assertIsNone(scope["DEV-CB-01"]["current_release_id"])
        # 文本报告可读
        text = format_record(rec)
        self.assertIn("审核链", text)
        self.assertIn("局部失败处置", text)
        self.assertIn("当前适用范围", text)

    def test_record_links_rollback_release(self):
        s = ReleaseScenario()
        rel = s.release(targets=["DEV-CN-01", "DEV-CN-02"],
                        wave_order=["canary"])
        s.p.releases.start(rel.release_id, "u_admin")
        s.p.releases.collect_wave(rel.release_id)
        s.p.releases.promote_wave(rel.release_id, "u_admin")
        # 研发库中的已知良好基线内容（首版发布之前存在）
        from tests.test_validation import base_params
        baseline = s.p.store.put("recipe_content", {
            "recipe_code": "RCP-LFP-01", "product_code": "PRD-BAT-01",
            "parameters": base_params(
                curing_temp={"value": 140, "unit": "degC"}),
        })
        rb = s.p.releases.rollback(rel.release_id, "u_admin", "异常",
                                  restore_digest=baseline.digest)
        rec = build_release_record(s.p.releases, s.p.recipes, rel.release_id)
        self.assertEqual(
            rec["local_failure_disposition"]["rolled_back_to_release"],
            rb.release_id)
        rec_rb = build_release_record(s.p.releases, s.p.recipes,
                                      rb.release_id)
        self.assertEqual(rec_rb["rollback_of"], rel.release_id)
        self.assertEqual(rec_rb["restores_digest"], baseline.digest)


if __name__ == "__main__":
    unittest.main()
