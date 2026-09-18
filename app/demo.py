"""全链路演示：编辑 → 审核 → 冻结 → 灰度下发（故障停止扩散）→ 恢复 → 完成 → 回滚。

运行：``python -m app.demo``
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .catalog import default_catalog
from .devices import Device, SimulatedDeviceGateway
from .recipe import ParameterValue, RecipeDraft, validate_draft
from .release import ReleaseManager
from .reporting import build_release_report
from .resolution import resolve_applicable
from .review import ROLE_PROCESS_REVIEWER, ROLE_QUALITY_LEAD, ReviewRecord
from .signing import KeyStore

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)


def build_world():
    catalog = default_catalog()
    keystore = KeyStore()
    for signer in ("eng.li", "rev.wang", "qa.zhao", "release.bot"):
        keystore.register(signer, f"key-of-{signer}".encode())

    full_ranges = {
        "mix.temperature": (0.0, 300.0),
        "mix.duration": (0.0, 480.0),
        "mix.pressure": (0.0, 16.0),
        "dose.mass": (0.0, 600.0),
        "stir.speed": (0.0, 2000.0),
        "cool.temperature": (-10.0, 100.0),
        "cool.duration": (0.0, 600.0),
        "fill.ratio": (0.0, 100.0),
    }
    devices = {
        f"MX-{i}": Device(
            device_id=f"MX-{i}",
            group="line-a" if i <= 2 else "line-b",
            products=frozenset({"RESIN-01"}),
            param_ranges=full_ranges,
            key=f"device-key-{i}".encode(),
        )
        for i in range(1, 5)
    }
    for device in devices.values():
        keystore.register(f"device:{device.device_id}", device.key)
    gateway = SimulatedDeviceGateway(devices, keystore)
    manager = ReleaseManager(catalog, devices, keystore, gateway)
    return catalog, keystore, devices, gateway, manager


def make_draft(version: int, mix_temp: float) -> RecipeDraft:
    return RecipeDraft(
        recipe_id="RCP-RESIN-01",
        product_id="RESIN-01",
        version=version,
        author="eng.li",
        params={
            "mix.temperature": ParameterValue(mix_temp, "Cel"),
            "mix.duration": ParameterValue(45.0, "min"),
            "mix.pressure": ParameterValue(6.0, "bar"),
            "dose.mass": ParameterValue(120.0, "kg"),
            "stir.speed": ParameterValue(150.0, "rpm"),
            "cool.temperature": ParameterValue(40.0, "Cel"),
            "cool.duration": ParameterValue(60.0, "min"),
            "fill.ratio": ParameterValue(80.0, "%"),
        },
        created_at=NOW,
    )


def approve(catalog, keystore, draft):
    review = ReviewRecord(draft, catalog)
    review.submit("eng.li", NOW)
    review.approve(keystore, "rev.wang", ROLE_PROCESS_REVIEWER, NOW)
    review.approve(keystore, "qa.zhao", ROLE_QUALITY_LEAD, NOW)  # 高风险参数会签
    return review


def main() -> None:
    catalog, keystore, devices, gateway, manager = build_world()
    valid_from, valid_until = NOW, NOW + timedelta(days=30)

    # 第一版：全量发布到 line-a + line-b
    draft_v1 = make_draft(1, 180.0)
    validate_draft(draft_v1, catalog)
    review_v1 = approve(catalog, keystore, draft_v1)
    rel_v1 = manager.freeze(review_v1, ["line-a", "line-b"], "release.bot", valid_from, valid_until, NOW)
    manager.start(rel_v1.release_id)
    manager.deploy_next_batch(rel_v1.release_id, NOW)
    manager.deploy_next_batch(rel_v1.release_id, NOW)
    print(f"[v1] {rel_v1.release_id} state={rel_v1.state.value}")

    # 第二版：灰度到 line-a，line-b 中一台设备收到被篡改报文 → 停止扩散
    draft_v2 = make_draft(2, 200.0)
    validate_draft(draft_v2, catalog)
    review_v2 = approve(catalog, keystore, draft_v2)
    rel_v2 = manager.freeze(review_v2, ["line-a", "line-b"], "release.bot", valid_from, valid_until, NOW)
    manager.start(rel_v2.release_id)
    manager.deploy_next_batch(rel_v2.release_id, NOW)  # line-a 成功
    gateway.set_behavior("MX-3", "corrupt")            # 模拟传输篡改
    manager.deploy_next_batch(rel_v2.release_id, NOW)  # line-b 第一台即失败 → 停止扩散
    print(f"[v2] {rel_v2.release_id} state={rel_v2.state.value} reason={rel_v2.pause_reason}")

    print("\n=== 局部失败时的发布记录 ===")
    print(json.dumps(build_release_report(manager, rel_v2.release_id, review_v2), ensure_ascii=False, indent=2))

    # 故障排除后恢复灰度
    gateway.set_behavior("MX-3", "ok")
    manager.resume(rel_v2.release_id, "release.bot")
    manager.deploy_next_batch(rel_v2.release_id, NOW)
    print(f"\n[v2] resumed state={rel_v2.state.value}")

    applicable = resolve_applicable(manager, "MX-1", "RESIN-01", NOW)
    print(f"[resolve] MX-1 -> {applicable.release_id} digest={applicable.content_digest[:12]}...")

    # 回滚：创建引用 v1 内容的新发布
    rel_rb = manager.rollback(rel_v2.release_id, "release.bot", "v2 试产异常，回到 v1",
                              valid_from, valid_until, NOW)
    manager.start(rel_rb.release_id)
    manager.deploy_next_batch(rel_rb.release_id, NOW)
    manager.deploy_next_batch(rel_rb.release_id, NOW)
    print(f"[rollback] {rel_rb.release_id} restores={rel_rb.restores} state={rel_rb.state.value}; "
          f"{rel_v2.release_id} state={rel_v2.state.value}")

    applicable = resolve_applicable(manager, "MX-1", "RESIN-01", NOW)
    print(f"[resolve] MX-1 -> {applicable.release_id} digest={applicable.content_digest[:12]}... "
          f"(== v1 digest: {applicable.content_digest == rel_v1.content_digest})")


if __name__ == "__main__":
    main()
