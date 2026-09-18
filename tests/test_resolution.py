"""操作员开批前的适用版本解析：唯一版本、过期授权、灰度外设备。"""

import unittest
from datetime import timedelta

from app.errors import (
    AuthorizationExpiredError,
    DeviceOutOfScopeError,
    NoApplicableVersionError,
)
from app.resolution import resolve_applicable

from helpers import NOW, VALID_UNTIL, build_world, freeze_and_deploy, make_draft


class ResolutionTest(unittest.TestCase):
    def setUp(self):
        self.catalog, self.keystore, self.devices, self.gateway, self.manager = build_world()

    def test_resolve_unique_applicable_version(self):
        _, release = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft())
        applicable = resolve_applicable(self.manager, "MX-1", "RESIN-01", NOW)
        self.assertEqual(applicable.release_id, release.release_id)
        self.assertEqual(applicable.content_digest, release.content_digest)
        self.assertEqual(applicable.recipe_version, 1)
        self.assertEqual(applicable.params["mix.temperature"], {"unit": "Cel", "value": 180.0})

    def test_new_release_supersedes_old_marking(self):
        freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft(version=1))
        _, rel_v2 = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft(version=2, mix_temp=200.0))
        applicable = resolve_applicable(self.manager, "MX-1", "RESIN-01", NOW)
        self.assertEqual(applicable.release_id, rel_v2.release_id)  # 唯一适用版本即最新生效版

    def test_no_version_for_untouched_device(self):
        freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft(), groups=("line-a",))
        with self.assertRaises(NoApplicableVersionError):
            resolve_applicable(self.manager, "MX-3", "RESIN-01", NOW)  # 灰度外设备

    def test_expired_authorization_rejected(self):
        freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft())
        with self.assertRaises(AuthorizationExpiredError):
            resolve_applicable(self.manager, "MX-1", "RESIN-01", VALID_UNTIL + timedelta(seconds=1))

    def test_not_yet_valid_authorization_rejected(self):
        freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft())
        with self.assertRaises(AuthorizationExpiredError):
            resolve_applicable(self.manager, "MX-1", "RESIN-01", NOW - timedelta(days=1))

    def test_device_outside_gray_scope_rejected(self):
        """设备调组后不再处于发布灰度范围内 → 拒绝。"""
        freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft(), groups=("line-a",))
        self.devices["MX-1"].group = "line-c"  # 设备被调出灰度组
        with self.assertRaises(DeviceOutOfScopeError):
            resolve_applicable(self.manager, "MX-1", "RESIN-01", NOW)

    def test_unknown_device_rejected(self):
        with self.assertRaises(DeviceOutOfScopeError):
            resolve_applicable(self.manager, "MX-99", "RESIN-01", NOW)

    def test_resolve_after_rollback_returns_old_content(self):
        from helpers import RELEASE_BOT, VALID_FROM, VALID_UNTIL

        _, rel_v1 = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft(version=1))
        _, rel_v2 = freeze_and_deploy(self.manager, self.catalog, self.keystore, make_draft(version=2, mix_temp=200.0))
        rel_rb = self.manager.rollback(rel_v2.release_id, RELEASE_BOT, "回滚", VALID_FROM, VALID_UNTIL, NOW)
        self.manager.start(rel_rb.release_id)
        self.manager.deploy_next_batch(rel_rb.release_id, NOW)
        self.manager.deploy_next_batch(rel_rb.release_id, NOW)

        applicable = resolve_applicable(self.manager, "MX-1", "RESIN-01", NOW)
        self.assertEqual(applicable.release_id, rel_rb.release_id)
        self.assertEqual(applicable.content_digest, rel_v1.content_digest)  # 回到旧内容


if __name__ == "__main__":
    unittest.main()
