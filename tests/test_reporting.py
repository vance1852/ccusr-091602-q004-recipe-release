"""发布记录报告：审核链、设备指令与回执、失败处置、当前适用范围。"""

import unittest

from app.release import ReleaseState
from app.reporting import build_release_report

from helpers import (
    NOW,
    RELEASE_BOT,
    VALID_FROM,
    VALID_UNTIL,
    approve_record,
    build_world,
    freeze_and_deploy,
    make_draft,
)


class ReleaseReportTest(unittest.TestCase):
    def setUp(self):
        self.catalog, self.keystore, self.devices, self.gateway, self.manager = build_world()

    def test_report_shows_full_chain_and_scope(self):
        review, release = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft())
        report = build_release_report(self.manager, release.release_id, review)

        # 审核链：技术审核 + 质量会签，全部验签通过
        self.assertEqual([d["step"] for d in report["review_chain"]], ["technical_review", "quality_review"])
        self.assertTrue(all(d["signature_valid"] for d in report["review_chain"]))
        self.assertEqual(report["review_chain"][0]["algorithm"], "HMAC-SHA256")

        # 每台设备的指令与回执
        self.assertEqual(len(report["devices"]), 4)
        for entry in report["devices"]:
            self.assertEqual(entry["receipt"]["result"], "applied")
            self.assertTrue(entry["receipt"]["digest_match"])
            self.assertTrue(entry["receipt"]["signature_valid"])

        # 当前适用范围
        self.assertEqual(report["release"]["state"], "completed")
        self.assertEqual(
            report["current_scope"]["effective_devices"],
            ["MX-1", "MX-2", "MX-3", "MX-4"],
        )
        self.assertEqual(report["outcome"]["pending"], [])
        self.assertIsNone(report["outcome"]["failure_handling"])

    def test_report_shows_failure_handling_after_halt(self):
        review = approve_record(self.catalog, self.keystore, make_draft())
        release = self.manager.freeze(review, ["line-a", "line-b"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)
        self.manager.start(release.release_id)
        self.manager.deploy_next_batch(release.release_id, NOW)
        self.gateway.set_behavior("MX-3", "timeout")
        self.manager.deploy_next_batch(release.release_id, NOW)

        report = build_release_report(self.manager, release.release_id, review)
        handling = report["outcome"]["failure_handling"]
        self.assertEqual(report["release"]["state"], "paused")
        self.assertEqual(handling["stopped_at_groups"], ["line-b"])
        self.assertIn("timeout", handling["reason"])
        self.assertEqual(handling["applied_devices_keep_version"], ["MX-1", "MX-2"])
        self.assertEqual(report["outcome"]["pending"], ["MX-4"])  # 未扩散到的设备
        self.assertEqual(report["outcome"]["failed"], ["MX-3"])

    def test_report_exposes_tampered_review_signature(self):
        """模拟签名篡改：报告中的审核链验签结果必须暴露问题。"""
        review, release = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft())
        review.decisions[1].message.payload["decision"] = "reject"  # 篡改会签决定
        report = build_release_report(self.manager, release.release_id, review)
        self.assertFalse(report["review_chain"][1]["signature_valid"])

    def test_report_exposes_tampered_device_receipt(self):
        review, release = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft())
        receipt = next(iter(release.receipts.values()))
        receipt.message.payload["result"] = "rejected"  # 篡改已入库回执
        report = build_release_report(self.manager, release.release_id, review)
        tampered = next(d for d in report["devices"] if d["command_id"] == receipt.command_id)
        self.assertFalse(tampered["receipt"]["signature_valid"])

    def test_report_counts_duplicate_receipts(self):
        review, release = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft())
        receipt = next(iter(release.receipts.values()))
        self.manager.record_receipt(release.release_id, receipt)
        report = build_release_report(self.manager, release.release_id, review)
        self.assertEqual(report["outcome"]["duplicate_receipts"], 1)


if __name__ == "__main__":
    unittest.main()
