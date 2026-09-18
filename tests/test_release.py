"""发布链路：冻结、分批下发、失败停止扩散、重复回执、灰度暂停、回滚。"""

import unittest

from app.errors import (
    ConflictingReceiptError,
    DeviceCapabilityError,
    ReleaseStateError,
    SignatureMismatchError,
    UnknownCommandError,
)
from app.release import ReleaseState

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


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.catalog, self.keystore, self.devices, self.gateway, self.manager = build_world()

    def _approved(self, draft):
        return approve_record(self.catalog, self.keystore, draft)

    # ---- 冻结 ----

    def test_freeze_requires_approved_review(self):
        from app.review import ReviewRecord

        review = ReviewRecord(make_draft(), self.catalog)  # 未提交审核
        with self.assertRaises(ReleaseStateError):
            self.manager.freeze(review, ["line-a"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)

    def test_freeze_rejected_when_review_chain_tampered(self):
        """模拟签名篡改：审核决定被改动后，冻结必须拒绝。"""
        review = self._approved(make_draft())
        review.decisions[0].message.payload["decision"] = "reject"  # 篡改决定内容
        with self.assertRaises(SignatureMismatchError):
            self.manager.freeze(review, ["line-a"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)

    def test_freeze_checks_device_capability(self):
        self.devices["MX-1"].param_ranges["mix.temperature"] = (0.0, 100.0)  # 量程不足
        review = self._approved(make_draft(mix_temp=180.0))
        with self.assertRaises(DeviceCapabilityError):
            self.manager.freeze(review, ["line-a"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)

    def test_freeze_rejects_unknown_group(self):
        review = self._approved(make_draft())
        with self.assertRaises(ReleaseStateError):
            self.manager.freeze(review, ["line-z"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)

    # ---- 分批下发 ----

    def test_deploy_by_groups_and_mark_devices(self):
        _, release = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft())
        self.assertEqual(release.state, ReleaseState.COMPLETED)
        self.assertEqual(release.deployed_groups, ["line-a", "line-b"])
        for device_id in ("MX-1", "MX-2", "MX-3", "MX-4"):
            marking = self.manager.device_marking(device_id, "RESIN-01")
            self.assertIsNotNone(marking)
            self.assertEqual(marking.content_digest, release.content_digest)

    def test_gray_release_only_touches_selected_groups(self):
        _, release = freeze_and_deploy(
            self.manager, self.catalog, self.keystore, make_draft(), groups=("line-a",)
        )
        self.assertEqual(release.state, ReleaseState.COMPLETED)
        self.assertIsNotNone(self.manager.device_marking("MX-1", "RESIN-01"))
        self.assertIsNone(self.manager.device_marking("MX-3", "RESIN-01"))  # 灰度外设备无标记

    # ---- 失败停止扩散 ----

    def test_digest_mismatch_halts_propagation(self):
        review = self._approved(make_draft())
        release = self.manager.freeze(review, ["line-a", "line-b"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)
        self.manager.start(release.release_id)
        self.manager.deploy_next_batch(release.release_id, NOW)  # line-a 成功

        self.gateway.set_behavior("MX-3", "corrupt")  # 模拟传输篡改
        result = self.manager.deploy_next_batch(release.release_id, NOW)

        self.assertTrue(result.halted)
        self.assertEqual(release.state, ReleaseState.PAUSED)
        self.assertIn("digest_mismatch", release.pause_reason)
        # 已成功设备保留当前版本标记
        self.assertEqual(
            self.manager.device_marking("MX-1", "RESIN-01").content_digest,
            release.content_digest,
        )
        # 未下发设备不受影响
        self.assertIsNone(self.manager.device_marking("MX-4", "RESIN-01"))
        self.assertNotIn("MX-4", [c.device_id for c in release.commands.values()])

    def test_timeout_halts_propagation(self):
        self.gateway.set_behavior("MX-1", "timeout")  # 回执缺失
        review = self._approved(make_draft())
        release = self.manager.freeze(review, ["line-a"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)
        self.manager.start(release.release_id)
        result = self.manager.deploy_next_batch(release.release_id, NOW)
        self.assertTrue(result.halted)
        self.assertEqual(release.state, ReleaseState.PAUSED)
        self.assertIn("timeout", release.pause_reason)
        self.assertIsNone(self.manager.device_marking("MX-1", "RESIN-01"))

    def test_device_rejection_halts_propagation(self):
        self.gateway.set_behavior("MX-2", "reject")
        review = self._approved(make_draft())
        release = self.manager.freeze(review, ["line-a"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)
        self.manager.start(release.release_id)
        self.manager.deploy_next_batch(release.release_id, NOW)
        self.assertEqual(release.state, ReleaseState.PAUSED)
        # MX-1 已成功并保留标记，MX-2 被拒绝
        self.assertIsNotNone(self.manager.device_marking("MX-1", "RESIN-01"))
        self.assertIsNone(self.manager.device_marking("MX-2", "RESIN-01"))

    def test_resume_after_failure_continues_remaining_devices(self):
        self.gateway.set_behavior("MX-3", "corrupt")
        review = self._approved(make_draft())
        release = self.manager.freeze(review, ["line-a", "line-b"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)
        self.manager.start(release.release_id)
        self.manager.deploy_next_batch(release.release_id, NOW)
        self.manager.deploy_next_batch(release.release_id, NOW)  # 在 MX-3 处停止
        self.assertEqual(release.state, ReleaseState.PAUSED)

        self.gateway.set_behavior("MX-3", "ok")  # 故障排除
        self.manager.resume(release.release_id, RELEASE_BOT)
        self.manager.deploy_next_batch(release.release_id, NOW)
        self.assertEqual(release.state, ReleaseState.COMPLETED)
        self.assertIsNotNone(self.manager.device_marking("MX-4", "RESIN-01"))

    # ---- 重复回执 ----

    def test_duplicate_receipt_is_idempotent(self):
        review = self._approved(make_draft())
        release = self.manager.freeze(review, ["line-a"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)
        self.manager.start(release.release_id)
        self.manager.deploy_next_batch(release.release_id, NOW)
        self.assertEqual(release.state, ReleaseState.COMPLETED)

        receipt = next(iter(release.receipts.values()))
        self.assertTrue(self.manager.record_receipt(release.release_id, receipt))  # 重复 → 忽略
        self.assertEqual(release.duplicate_receipts, 1)
        self.assertEqual(len(release.receipts), 2)  # 不新增记录
        self.assertEqual(release.state, ReleaseState.COMPLETED)

    def test_conflicting_receipt_rejected(self):
        from app.devices import DeviceReceipt

        review = self._approved(make_draft())
        release = self.manager.freeze(review, ["line-a"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)
        self.manager.start(release.release_id)
        self.manager.deploy_next_batch(release.release_id, NOW)

        receipt = next(iter(release.receipts.values()))
        conflicting = DeviceReceipt(
            command_id=receipt.command_id,
            release_id=receipt.release_id,
            device_id=receipt.device_id,
            received_digest="0" * 64,  # 与原回执矛盾
            result="digest_mismatch",
            received_at=receipt.received_at,
            message=None,
        )
        with self.assertRaises(ConflictingReceiptError):
            self.manager.record_receipt(release.release_id, conflicting)

    def test_unknown_command_receipt_rejected(self):
        from app.devices import DeviceReceipt

        review = self._approved(make_draft())
        release = self.manager.freeze(review, ["line-a"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)
        stray = DeviceReceipt("REL-9999:MX-1#1", release.release_id, "MX-1", "0" * 64, "applied", "", None)
        with self.assertRaises(UnknownCommandError):
            self.manager.record_receipt(release.release_id, stray)

    # ---- 灰度暂停 ----

    def test_manual_pause_blocks_batches_until_resume(self):
        review = self._approved(make_draft())
        release = self.manager.freeze(review, ["line-a", "line-b"], RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)
        self.manager.start(release.release_id)
        self.manager.deploy_next_batch(release.release_id, NOW)  # line-a 完成

        self.manager.pause(release.release_id, RELEASE_BOT, "灰度观察")
        self.assertEqual(release.state, ReleaseState.PAUSED)
        with self.assertRaises(ReleaseStateError):
            self.manager.deploy_next_batch(release.release_id, NOW)  # 暂停期间禁止扩散
        self.assertIsNone(self.manager.device_marking("MX-3", "RESIN-01"))

        self.manager.resume(release.release_id, RELEASE_BOT)
        self.manager.deploy_next_batch(release.release_id, NOW)
        self.assertEqual(release.state, ReleaseState.COMPLETED)

    # ---- 回滚 ----

    def test_rollback_creates_new_release_referencing_old_content(self):
        _, rel_v1 = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft(version=1))
        _, rel_v2 = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft(version=2, mix_temp=200.0))
        self.assertEqual(rel_v2.supersedes, rel_v1.release_id)

        rel_rb = self.manager.rollback(rel_v2.release_id, RELEASE_BOT, "试产异常", VALID_FROM, VALID_UNTIL, NOW)
        self.assertEqual(rel_rb.kind, "rollback")
        self.assertEqual(rel_rb.content_digest, rel_v1.content_digest)  # 引用旧内容
        self.assertEqual(rel_rb.restores, rel_v1.release_id)
        self.assertEqual(rel_rb.rolls_back, rel_v2.release_id)
        self.assertEqual(rel_v2.state, ReleaseState.COMPLETED)  # 回滚发布生效前原发布状态不变

        self.manager.start(rel_rb.release_id)
        self.manager.deploy_next_batch(rel_rb.release_id, NOW)
        self.manager.deploy_next_batch(rel_rb.release_id, NOW)
        self.assertEqual(rel_rb.state, ReleaseState.COMPLETED)
        self.assertEqual(rel_v2.state, ReleaseState.ROLLED_BACK)

        marking = self.manager.device_marking("MX-1", "RESIN-01")
        self.assertEqual(marking.release_id, rel_rb.release_id)
        self.assertEqual(marking.content_digest, rel_v1.content_digest)
        # 三条发布记录都保留，没有覆盖
        self.assertEqual(len(self.manager.releases), 3)

    def test_rollback_without_history_rejected(self):
        _, rel_v1 = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft())
        with self.assertRaises(ReleaseStateError):
            self.manager.rollback(rel_v1.release_id, RELEASE_BOT, "无历史可回滚", VALID_FROM, VALID_UNTIL, NOW)


if __name__ == "__main__":
    unittest.main()
