"""发布编排：冻结、分波、回执门控、局部失败停止扩散、暂停与回滚。"""

import unittest

from app.devices import SimulatedController
from app.errors import (DigestMismatchError, DuplicateReceiptError,
                        SignatureError, WorkflowError)
from app.platform import Platform
from app.release import (DEV_APPLIED, DEV_MISMATCH, DEV_PENDING,
                         DEV_TIMEOUT, DEV_WAITING)
from tests.test_validation import base_params


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


class ReleaseScenario:
    """搭建一个已审核通过、可发布的标准场景。"""

    def __init__(self):
        self.clock = FakeClock()
        self.p = Platform(clock=self.clock)
        self.chg = self.p.recipes.create_draft(
            "u_gongyi", "RCP-LFP-01", "PRD-BAT-01", base_params())
        self.p.recipes.submit(self.chg.change_id, "u_gongyi")
        self.p.recipes.technical_review(self.chg.change_id, "u_zhang", True)
        self.p.recipes.quality_countersign(self.chg.change_id,
                                           "u_quality", True)

    def release(self, targets=None, wave_order=None):
        return self.p.releases.create_release(
            self.chg.change_id, "u_admin", targets=targets,
            wave_order=wave_order)

    def finish_wave(self, rel):
        return self.p.releases.collect_wave(rel.release_id)


class FreezeAndWaveTests(unittest.TestCase):
    def setUp(self):
        self.s = ReleaseScenario()

    def test_release_freezes_digest_and_waves(self):
        rel = self.s.release()
        self.assertEqual(rel.state, "frozen")
        self.assertEqual(rel.content_digest, self.s.chg.content_digest)
        # canary 组必须排在第一波
        self.assertEqual(rel.wave_order[0], "canary")
        self.assertTrue(self.s.p.store.contains(rel.content_digest))

    def test_wave_gating_blocks_promotion_before_all_applied(self):
        rel = self.s.release()
        self.s.p.releases.start(rel.release_id, "u_admin")
        # 首波尚未全部回执时不可推进
        with self.assertRaises(WorkflowError):
            self.s.p.releases.promote_wave(rel.release_id, "u_admin")
        self.s.finish_wave(rel)
        self.s.p.releases.promote_wave(rel.release_id, "u_admin")
        status = self.s.p.releases.wave_status(rel.release_id)
        # 第二波 broad 已下发，第三波 prep 仍等待
        self.assertEqual(status["current_wave"], 1)
        prep = [w for w in status["waves"] if w["group"] == "prep"][0]
        self.assertEqual(len(prep["waiting"]), 1)

    def test_not_yet_in_scope_device_has_no_version(self):
        rel = self.s.release(targets=["DEV-CN-01", "DEV-CB-01"])
        self.s.p.releases.start(rel.release_id, "u_admin")
        self.s.finish_wave(rel)
        # 试点组生效；推广组设备尚无适用版本
        self.assertIsNotNone(
            self.s.p.releases.active_version("DEV-CN-01", "PRD-BAT-01"))
        self.assertIsNone(
            self.s.p.releases.active_version("DEV-CB-01", "PRD-BAT-01"))

    def test_completed_release_marks_unique_version_per_device(self):
        rel = self.s.release()
        self.s.p.releases.start(rel.release_id, "u_admin")
        for _ in range(3):
            self.s.finish_wave(rel)
            self.s.p.releases.promote_wave(rel.release_id, "u_admin")
        self.assertEqual(rel.state, "completed")
        for d in ("DEV-CN-01", "DEV-CN-02", "DEV-CB-01", "DEV-CB-02",
                  "DEV-MX-01"):
            mark = self.s.p.releases.active_version(d, "PRD-BAT-01")
            self.assertIsNotNone(mark, d)
            self.assertEqual(mark.content_digest, rel.content_digest)

    def test_device_capability_preflight_blocks_release(self):
        # COATER-A 固化温度上限 200°C；目录上限 220
        chg2 = self.s.p.recipes.create_draft(
            "u_gongyi", "RCP-LFP-01", "PRD-BAT-01",
            base_params(curing_temp={"value": 205, "unit": "degC"}))
        self.s.p.recipes.submit(chg2.change_id, "u_gongyi")
        self.s.p.recipes.technical_review(chg2.change_id, "u_zhang", True)
        self.s.p.recipes.quality_countersign(chg2.change_id, "u_quality",
                                             True)
        from app.errors import ValidationError
        with self.assertRaises(ValidationError) as ctx:
            self.s.p.releases.create_release(chg2.change_id, "u_admin",
                                            targets=["DEV-CN-01"])
        self.assertTrue(any("COATER-A" in i and "curing_temp" in i
                            for i in ctx.exception.issues))

    def test_command_uses_device_native_unit(self):
        rel = self.s.release(targets=["DEV-MX-01"], wave_order=["prep"])
        self.s.p.releases.start(rel.release_id, "u_admin")
        cmd = rel.deliveries["DEV-MX-01"].command
        self.assertEqual(cmd["parameters"]["mixing_speed"]["unit"], "rpm")
        self.assertAlmostEqual(
            cmd["parameters"]["mixing_speed"]["value"], 600.0)

    def test_vacuum_pressure_converted_to_native_bar(self):
        # MIXER-X 真空压力原生单位为 bar；kPa 配方值必须换算后下发
        from app.release import ReleaseManager
        model = self.s.p.registry.model_of("DEV-MX-01")
        native = ReleaseManager._native_parameters(model, {
            "vacuum_pressure": {"value": -90, "unit": "kPa"}})
        self.assertEqual(native["vacuum_pressure"]["unit"], "bar")
        self.assertAlmostEqual(native["vacuum_pressure"]["value"], -0.9)


class ReceiptFailureTests(unittest.TestCase):
    def setUp(self):
        self.s = ReleaseScenario()
        self.rel = self.s.release(
            targets=["DEV-CN-01", "DEV-CN-02", "DEV-CB-01"],
            wave_order=["canary", "broad"])
        self.p = self.s.p
        self.p.releases.start(self.rel.release_id, "u_admin")

    def _receipt(self, device_id):
        return self.p.gateway.collect(device_id)

    def test_digest_mismatch_halts_propagation(self):
        self.p.gateway.controller("DEV-CN-01").set_mode(
            SimulatedController.MODE_DIGEST_ALARM)
        self.p.releases.collect_wave(self.rel.release_id)
        self.assertEqual(self.rel.state, "paused")
        d = self.rel.deliveries["DEV-CN-01"]
        self.assertEqual(d.status, DEV_MISMATCH)
        # 停止扩散：推广组仍在等待
        self.assertEqual(self.rel.deliveries["DEV-CB-01"].status,
                         DEV_WAITING)
        # 已成功的试点设备继续保留当前版本
        mark = self.p.releases.active_version("DEV-CN-02", "PRD-BAT-01")
        self.assertIsNotNone(mark)
        with self.assertRaises(WorkflowError):
            self.p.releases.promote_wave(self.rel.release_id, "u_admin")

    def test_applied_but_wrong_digest_echo_counts_as_mismatch(self):
        self.p.gateway.controller("DEV-CN-01").set_mode(
            SimulatedController.MODE_WRONG_ECHO)
        self.p.releases.collect_wave(self.rel.release_id)
        self.assertEqual(self.rel.deliveries["DEV-CN-01"].status,
                         DEV_MISMATCH)
        self.assertIn("摘要回显不一致",
                      self.rel.deliveries["DEV-CN-01"].error)

    def test_missing_receipt_timeout_halts(self):
        self.p.gateway.controller("DEV-CN-01").set_mode(
            SimulatedController.MODE_SILENT)
        # CN-02 正常回执
        self.p.releases.receive_receipt(
            self.rel.release_id, self._receipt("DEV-CN-02"))
        self.p.releases.mark_timeout(self.rel.release_id, "DEV-CN-01",
                                    "u_admin")
        self.assertEqual(self.rel.state, "paused")
        self.assertEqual(self.rel.deliveries["DEV-CN-01"].status,
                         DEV_TIMEOUT)

    def test_duplicate_receipt_rejected(self):
        receipt = self._receipt("DEV-CN-01")
        self.p.releases.receive_receipt(self.rel.release_id, receipt)
        # 原样重放同一条回执
        replay = self.p.gateway.controller(
            "DEV-CN-01").replay_last_receipt(receipt)
        with self.assertRaises(DuplicateReceiptError):
            self.p.releases.receive_receipt(self.rel.release_id, replay)

    def test_stale_command_receipt_rejected(self):
        # 设备用合法密钥签发了一条引用旧命令号的回执（重放上一轮指令）
        import copy
        from app.crypto import sign_message
        from app.devices import device_key_id
        receipt = self._receipt("DEV-CN-01")
        stale = copy.deepcopy(receipt)
        stale["command_id"] = "CMD-OLDLDLDLDLD"
        stale = sign_message(stale, self.p.key_ring,
                             device_key_id("DEV-CN-01"))
        with self.assertRaises(WorkflowError):
            self.p.releases.receive_receipt(self.rel.release_id, stale)

    def test_tampered_receipt_signature_rejected(self):
        receipt = self._receipt("DEV-CN-01")
        broken = SimulatedController.tamper_receipt(
            receipt, result="digest_mismatch")
        with self.assertRaises(SignatureError):
            self.p.releases.receive_receipt(self.rel.release_id, broken)

    def test_device_rejects_forged_command(self):
        import copy as _copy
        rel = self.s.release(targets=["DEV-CN-01"], wave_order=["canary"])
        self.p.releases.start(rel.release_id, "u_admin")
        cmd = rel.deliveries["DEV-CN-01"].command
        forged = _copy.deepcopy(cmd)
        forged["parameters"]["curing_temp"]["value"] = 999  # 中间人改参数
        ctrl = self.p.gateway.controller("DEV-CN-01")
        with self.assertRaises(SignatureError):
            ctrl.deliver(forged)

    def test_recover_after_failure_then_continue(self):
        self.p.gateway.controller("DEV-CN-01").set_mode(
            SimulatedController.MODE_SILENT)
        self.p.releases.collect_wave(self.rel.release_id)
        self.p.releases.mark_timeout(self.rel.release_id, "DEV-CN-01",
                                    "u_admin")
        self.assertEqual(self.rel.state, "paused")
        # 控制器恢复 → 重试该设备
        self.p.gateway.controller("DEV-CN-01").set_mode(
            SimulatedController.MODE_HONEST)
        self.p.releases.retry_device(self.rel.release_id, "DEV-CN-01",
                                    "u_admin")
        receipt = self.p.gateway.collect("DEV-CN-01")
        self.p.releases.receive_receipt(self.rel.release_id, receipt)
        # 失败设备清零后方可恢复并扩散
        self.p.releases.resume(self.rel.release_id, "u_admin")
        self.assertEqual(self.rel.state, "deploying")
        self.p.releases.promote_wave(self.rel.release_id, "u_admin")
        self.p.releases.collect_wave(self.rel.release_id)
        final = self.p.releases.promote_wave(self.rel.release_id, "u_admin")
        self.assertEqual(final.state, "completed")


class PauseTests(unittest.TestCase):
    def test_manual_grayscale_pause_blocks_next_wave(self):
        s = ReleaseScenario()
        rel = s.release(targets=["DEV-CN-01", "DEV-CB-01"],
                        wave_order=["canary", "broad"])
        s.p.releases.start(rel.release_id, "u_admin")
        s.p.releases.collect_wave(rel.release_id)
        s.p.releases.pause(rel.release_id, "u_admin", "灰度观察 24h")
        self.assertEqual(rel.state, "paused")
        with self.assertRaises(WorkflowError):
            s.p.releases.promote_wave(rel.release_id, "u_admin")
        # 暂停期间灰度外设备依旧没有适用版本
        self.assertIsNone(
            s.p.releases.active_version("DEV-CB-01", "PRD-BAT-01"))
        s.p.releases.resume(rel.release_id, "u_admin")
        s.p.releases.promote_wave(rel.release_id, "u_admin")
        s.p.releases.collect_wave(rel.release_id)
        final = s.p.releases.promote_wave(rel.release_id, "u_admin")
        self.assertEqual(final.state, "completed")


if __name__ == "__main__":
    unittest.main()
