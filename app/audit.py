"""一次发布记录的全景视图。

工艺负责人可从单个发布记录看到：
* 冻结的内容摘要与配方内容；
* 完整审核链（谁、何时、决定、验签结果）；
* 每台设备所在波次、下发指令（含原生单位参数）、回执、尝试次数；
* 局部失败后的处置（暂停原因、重试、超时登记、回滚去向）；
* 各设备当前适用版本范围。
"""

from __future__ import annotations

from .release import (DEV_APPLIED, DEV_MISMATCH, DEV_PENDING,
                      DEV_REJECTED, DEV_TIMEOUT, DEV_WAITING, ReleaseManager)
from .recipe import RecipeWorkflow

_RESULT_LABELS = {
    DEV_APPLIED: "已生效",
    DEV_MISMATCH: "摘要不一致",
    DEV_TIMEOUT: "回执超时",
    DEV_REJECTED: "设备拒绝",
    DEV_PENDING: "等待回执",
    DEV_WAITING: "未轮到本波",
}


def build_release_record(releases: ReleaseManager,
                         recipes: RecipeWorkflow,
                         release_id: str) -> dict:
    r = releases.get(release_id)
    change = recipes.changes.get(r.change_id)
    content = releases.store.get(r.content_digest)

    chain = recipes.verify_chain(change.change_id) if change else []

    devices = []
    for device_id in r.targets:
        d = r.deliveries[device_id]
        devices.append({
            "device_id": device_id,
            "group": d.group,
            "wave": d.wave,
            "status": d.status,
            "status_label": _RESULT_LABELS.get(d.status, d.status),
            "command_id": d.command["command_id"] if d.command else None,
            "command_digest": (d.command["content_digest"]
                               if d.command else None),
            "native_parameters": (d.command["parameters"]
                                  if d.command else None),
            "receipt_id": d.receipt["receipt_id"] if d.receipt else None,
            "receipt_result": d.receipt["result"] if d.receipt else None,
            "receipt_digest": (d.receipt["content_digest"]
                               if d.receipt else None),
            "attempts": d.attempts,
            "error": d.error,
        })

    disposition = _local_failure_disposition(releases, r)

    current_scope = []
    for device_id in r.targets:
        mark = releases.active_version(device_id, r.product_code)
        current_scope.append({
            "device_id": device_id,
            "current_release_id": mark.release_id if mark else None,
            "current_digest": mark.content_digest if mark else None,
            "is_this_release": bool(mark and mark.release_id == r.release_id),
        })

    return {
        "release_id": r.release_id,
        "state": r.state,
        "recipe_code": r.recipe_code,
        "product_code": r.product_code,
        "change_id": r.change_id,
        "created_by": r.created_by,
        "created_at": r.created_at,
        "content_digest": r.content_digest,
        "restores_digest": r.restores_digest,
        "rollback_of": r.rollback_of,
        "frozen_content": content.payload,
        "wave_order": r.wave_order,
        "pause_reason": r.pause_reason,
        "review_chain": chain,
        "devices": devices,
        "local_failure_disposition": disposition,
        "current_applicability": current_scope,
        "timeline": r.history,
    }


def _local_failure_disposition(releases: ReleaseManager, r) -> dict:
    failed = [d.device_id for d in r.deliveries.values()
              if d.status in (DEV_MISMATCH, DEV_TIMEOUT, DEV_REJECTED)]
    applied = [d.device_id for d in r.deliveries.values()
               if d.status == DEV_APPLIED]
    rollback_release = None
    for x in releases.releases.values():
        if x.rollback_of == r.release_id:
            rollback_release = x.release_id
    return {
        "halted": r.state == "paused",
        "pause_reason": r.pause_reason,
        "failed_devices": failed,
        "already_applied_kept": applied,
        "rolled_back_to_release": rollback_release,
        "propagation_stopped": bool(failed) or r.state == "paused",
    }


def format_record(record: dict) -> str:
    """生成面向工艺负责人的中文文本报告。"""
    lines = []
    add = lines.append
    add(f"发布记录 {record['release_id']}  状态: {record['state']}")
    add(f"配方 {record['recipe_code']} / 产品 {record['product_code']} "
        f"变更 {record['change_id']}  创建人 {record['created_by']}")
    add(f"冻结摘要: {record['content_digest']}")
    if record["rollback_of"]:
        add(f"本发布为回滚发布，恢复摘要 {record['restores_digest']}，"
            f"原发布 {record['rollback_of']}")
    add("")
    add("审核链:")
    if record["review_chain"]:
        for c in record["review_chain"]:
            mark = "✓" if c["valid"] else "✗"
            add(f"  {mark} {c['type']} {c['by']} -> {c['decision']}"
                + (f"  ({c['error']})" if c["error"] else ""))
    else:
        add("  （无）")
    add("")
    add("设备指令与回执（按波次）:")
    for w, group in enumerate(record["wave_order"]):
        add(f"  波次 {w} [{group}]")
        for d in record["devices"]:
            if d["wave"] != w:
                continue
            add(f"    - {d['device_id']}: {d['status_label']}"
                + (f" 指令 {d['command_id']}" if d["command_id"] else "")
                + (f" 回执 {d['receipt_id']}" if d["receipt_id"] else "")
                + (f" 尝试 {d['attempts']} 次" if d["attempts"] > 1 else "")
                + (f"  错误: {d['error']}" if d["error"] else ""))
    disp = record["local_failure_disposition"]
    add("")
    add("局部失败处置:")
    add(f"  扩散是否停止: {'是' if disp['propagation_stopped'] else '否'}")
    add(f"  失败设备: {disp['failed_devices'] or '无'}")
    add(f"  已生效仍保留当前版本的设备: {disp['already_applied_kept'] or '无'}")
    if disp["rolled_back_to_release"]:
        add(f"  已回滚至新发布: {disp['rolled_back_to_release']}")
    add("")
    add("当前适用范围:")
    for row in record["current_applicability"]:
        if row["current_release_id"]:
            tag = "本发布" if row["is_this_release"] else "其他版本"
            add(f"  {row['device_id']}: {row['current_release_id']}"
                f" ({tag}) {row['current_digest'][:23]}…")
        else:
            add(f"  {row['device_id']}: 无适用版本")
    return "\n".join(lines)
