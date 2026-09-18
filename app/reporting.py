"""发布记录报告：工艺负责人从一条发布记录看全链路。

报告内容：
- 审核链（每步决定的签名者、角色、时间、算法与摘要，并现场重放验签）；
- 每台设备的指令与回执（含重复回执计数、回执验签结果）；
- 局部失败后的处置（在哪一组停止扩散、哪些设备保留了版本）；
- 当前适用范围（哪些设备正运行本发布内容、授权窗口）。
"""

from __future__ import annotations

from .release import RESULT_APPLIED, ReleaseManager, ReleaseState
from .review import ReviewRecord
from .signing import verify


def build_release_report(
    manager: ReleaseManager,
    release_id: str,
    review: ReviewRecord | None = None,
) -> dict:
    release = manager.releases[release_id]

    review_chain = review.chain_view(manager.keystore) if review is not None else []

    devices = []
    for command in release.commands.values():
        receipt = release.receipts.get(command.command_id)
        entry = {
            "device_id": command.device_id,
            "group": command.group,
            "command_id": command.command_id,
            "dispatched_at": command.dispatched_at,
            "content_digest": command.content_digest,
            "receipt": None,
        }
        if receipt is not None:
            entry["receipt"] = {
                "result": receipt.result,
                "received_at": receipt.received_at,
                "received_digest": receipt.received_digest,
                "digest_match": receipt.received_digest == command.content_digest,
                "signature_valid": (
                    verify(manager.keystore, receipt.message) if receipt.message else None
                ),
            }
        devices.append(entry)

    applied = release.applied_devices()
    failed = [
        r.device_id
        for r in release.receipts.values()
        if r.result != RESULT_APPLIED
    ]
    dispatched = {c.device_id for c in release.commands.values()}
    pending = [
        d.device_id
        for d in manager.devices.values()
        if d.group in release.groups and d.device_id not in dispatched
    ]

    # 当前适用范围：本发布内容仍在哪些设备上生效
    effective_devices = sorted(
        device_id
        for (device_id, product), marking in manager.markings.items()
        if product == release.product_id and marking.release_id == release.release_id
    )

    failure_handling = None
    if release.state == ReleaseState.PAUSED:
        failure_handling = {
            "stopped_at_groups": [g for g in release.groups if g not in release.deployed_groups],
            "reason": release.pause_reason,
            "applied_devices_keep_version": applied,
            "note": "扩散已停止；已成功设备保留当前版本标记，未下发设备不受影响",
        }

    return {
        "release": {
            "release_id": release.release_id,
            "kind": release.kind,
            "state": release.state.value,
            "recipe_id": release.recipe_id,
            "recipe_version": release.recipe_version,
            "product_id": release.product_id,
            "content_digest": release.content_digest,
            "created_by": release.created_by,
            "created_at": release.created_at,
            "valid_from": release.valid_from,
            "valid_until": release.valid_until,
            "restores": release.restores,
            "rolls_back": release.rolls_back,
            "supersedes": release.supersedes,
        },
        "review_chain": review_chain,
        "devices": devices,
        "outcome": {
            "applied": sorted(applied),
            "failed": sorted(failed),
            "pending": sorted(pending),
            "deployed_groups": list(release.deployed_groups),
            "duplicate_receipts": release.duplicate_receipts,
            "failure_handling": failure_handling,
        },
        "current_scope": {
            "product_id": release.product_id,
            "groups": list(release.groups),
            "effective_devices": effective_devices,
            "authorization": {
                "valid_from": release.valid_from,
                "valid_until": release.valid_until,
            },
        },
    }
