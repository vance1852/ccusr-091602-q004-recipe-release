"""全链路核验脚本。

运行::

    python -m app.demo

依次演示：
1. 编辑校验 → 分级审核 → 冻结 → 分波灰度 → 开批授权（正常链路）；
2. 模拟设备回执签名篡改被拒绝、摘要不一致停止扩散、已生效设备保留版本；
3. 重复回执被拒绝；
4. 人工灰度暂停与恢复；
5. 新版本试点异常后回滚为引用旧内容的新发布。
"""

from __future__ import annotations

import copy

from .audit import build_release_record, format_record
from .crypto import tamper
from .devices import SimulatedController
from .errors import (AuthorizationDenied, DuplicateReceiptError,
                     SignatureError, ValidationError, WorkflowError)
from .platform import Platform

PRODUCT = "PRD-BAT-01"
RECIPE = "RCP-LFP-01"


def params_v1():
    return {
        "mix_mode": {"value": "vacuum", "unit": None},
        "vacuum_enabled": {"value": "on", "unit": None},
        "vacuum_pressure": {"value": -90, "unit": "kPa"},
        "coating_speed": {"value": 5000, "unit": "mm/min"},
        "coating_gap": {"value": 120, "unit": "um"},
        "drying_temp": {"value": 80, "unit": "degC"},
        "curing_temp": {"value": 150, "unit": "degC"},
        "curing_time": {"value": 10, "unit": "min"},
        "slurry_flow": {"value": 2.5, "unit": "L/min"},
        "mixing_speed": {"value": 600, "unit": "rpm"},
        "mixing_time": {"value": 30, "unit": "min"},
        "solvent_ratio": {"value": 5, "unit": "%"},
    }


def approved_change(p, version_params, engineer="u_gongyi"):
    chg = p.recipes.create_draft(engineer, RECIPE, PRODUCT, version_params)
    p.recipes.submit(chg.change_id, engineer)
    p.recipes.technical_review(chg.change_id, "u_zhang", True,
                               "技术复核通过")
    p.recipes.quality_countersign(chg.change_id, "u_quality", True,
                                  "高风险会签同意")
    return chg


def deploy_all(p, rel):
    if rel.state == "frozen":
        p.releases.start(rel.release_id, "u_admin")
    while True:
        p.releases.collect_wave(rel.release_id)
        if rel.state != "deploying":
            break
        rel = p.releases.promote_wave(rel.release_id, "u_admin")
        if rel.state == "completed":
            break
    return rel


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def scenario_1_happy_path():
    hr("场景 1：正常发布链路 + 开批授权")
    p = Platform()
    chg = approved_change(p, params_v1())
    print(f"变更 {chg.change_id} 审核通过，冻结摘要 {chg.content_digest}")

    # 编辑期校验示例：量纲错误被拦截
    bad = p.recipes.create_draft("u_gongyi", RECIPE, PRODUCT,
                                 {**params_v1(),
                                  "drying_temp": {"value": 80,
                                                  "unit": "MPa"}})
    try:
        p.recipes.submit(bad.change_id, "u_gongyi")
    except ValidationError as exc:
        print(f"编辑期校验拦截: {exc.issues[0]}")

    rel = p.releases.create_release(chg.change_id, "u_admin")
    deploy_all(p, rel)
    print(f"发布 {rel.release_id} 最终状态: {rel.state}，波次顺序 "
          f"{rel.wave_order}")

    token = p.authorizer.issue_grant("DEV-CB-01", PRODUCT, "op-chen")
    info = p.authorizer.authorize_batch(token)
    print(f"操作员开批授权成功: 设备 {info['device_id']} 适用版本 "
          f"{info['release_id']} 摘要 {info['content_digest'][:23]}…")

    # 灰度外设备
    p2 = Platform()
    chg2 = approved_change(p2, params_v1())
    rel2 = p2.releases.create_release(
        chg2.change_id, "u_admin", targets=["DEV-CN-01"],
        wave_order=["canary"])
    deploy_all(p2, rel2)
    try:
        p2.authorizer.issue_grant("DEV-CB-01", PRODUCT, "op-chen")
    except AuthorizationDenied as exc:
        print(f"灰度外设备开批被拒: {exc.reason}")


def scenario_2_tamper_and_digest():
    hr("场景 2：签名篡改被拒 + 摘要不一致停止扩散")
    p = Platform()
    chg = approved_change(p, params_v1())
    rel = p.releases.create_release(
        chg.change_id, "u_admin",
        targets=["DEV-CN-01", "DEV-CN-02", "DEV-CB-01"],
        wave_order=["canary", "broad"])
    p.releases.start(rel.release_id, "u_admin")

    # 2a. 中间人篡改回执内容
    receipt = p.gateway.collect("DEV-CN-01")
    forged = tamper(receipt, result="digest_mismatch")
    try:
        p.releases.receive_receipt(rel.release_id, forged)
    except SignatureError as exc:
        print(f"篡改回执被拒: {exc}")
    # 原始合法回执仍可入账
    p.releases.receive_receipt(rel.release_id, receipt)

    # 2b. CN-02 设备侧复算摘要不一致
    p.gateway.controller("DEV-CN-02").set_mode(
        SimulatedController.MODE_DIGEST_ALARM)
    p.releases.collect_wave(rel.release_id)
    print(f"发布状态: {rel.state}，原因: {rel.pause_reason}")
    print("已成功设备继续标记当前版本:")
    mark = p.releases.active_version("DEV-CN-01", PRODUCT)
    print(f"  DEV-CN-01 -> {mark.release_id}（保持当前版本）")
    print("停止扩散：DEV-CB-01 状态 =",
          rel.deliveries["DEV-CB-01"].status)

    print("\n工艺负责人看到的发布记录:")
    print(format_record(build_release_record(
        p.releases, p.recipes, rel.release_id)))


def scenario_3_duplicate_receipt():
    hr("场景 3：重复回执被拒")
    p = Platform()
    chg = approved_change(p, params_v1())
    rel = p.releases.create_release(
        chg.change_id, "u_admin", targets=["DEV-CN-01"],
        wave_order=["canary"])
    p.releases.start(rel.release_id, "u_admin")
    receipt = p.gateway.collect("DEV-CN-01")
    p.releases.receive_receipt(rel.release_id, receipt)
    try:
        p.releases.receive_receipt(
            rel.release_id, copy.deepcopy(receipt))
    except DuplicateReceiptError as exc:
        print(f"重复回执被拒: {exc}")


def scenario_4_grayscale_pause():
    hr("场景 4：人工灰度暂停")
    p = Platform()
    chg = approved_change(p, params_v1())
    rel = p.releases.create_release(
        chg.change_id, "u_admin",
        targets=["DEV-CN-01", "DEV-CB-01"],
        wave_order=["canary", "broad"])
    p.releases.start(rel.release_id, "u_admin")
    p.releases.collect_wave(rel.release_id)
    p.releases.pause(rel.release_id, "u_admin", "首批试点观察 24 小时")
    print(f"暂停: {rel.state} - {rel.pause_reason}")
    try:
        p.releases.promote_wave(rel.release_id, "u_admin")
    except WorkflowError as exc:
        print(f"暂停期间禁止扩散: {exc}")
    p.releases.resume(rel.release_id, "u_admin")
    deploy_all(p, rel)
    print(f"观察无异常后恢复，发布完成: {rel.state}")


def scenario_5_rollback():
    hr("场景 5：试点异常回滚（创建引用旧内容的新发布）")
    p = Platform()
    # v1 全量生效
    chg1 = approved_change(p, params_v1())
    r1 = p.releases.create_release(chg1.change_id, "u_admin")
    deploy_all(p, r1)
    v1 = r1.content_digest
    print(f"v1 全量生效: {v1[:23]}…")

    # v2 只在 canary 生效
    v2_params = {**params_v1(),
                 "curing_temp": {"value": 165, "unit": "degC"}}
    chg2 = approved_change(p, v2_params)
    r2 = p.releases.create_release(chg2.change_id, "u_admin")
    p.releases.start(r2.release_id, "u_admin")
    p.releases.collect_wave(r2.release_id)  # canary v2
    print(f"v2 仅试点组生效: {r2.content_digest[:23]}…；推广组仍为 v1")

    # 回滚：r2 置 rolled_back，新建发布引用 v1 内容，只面向已受影响设备
    r3 = p.releases.rollback(r2.release_id, "u_admin",
                             "试点批次外观异常，紧急回滚")
    print(f"原发布 {r2.release_id} -> {r2.state}；"
          f"回滚发布 {r3.release_id} 恢复摘要 {r3.restores_digest[:23]}…，"
          f"目标 {r3.targets}")
    deploy_all(p, r3)
    print("回滚发布完成后适用范围:")
    for row in p.releases.applicability_map(PRODUCT):
        if row["in_scope"]:
            print(f"  {row['device_id']} -> {row['release_id']} "
                  f"{row['content_digest'][:23]}…")
        else:
            print(f"  {row['device_id']} -> 无适用版本")

    # 回滚完成后新开批授权必须指向回滚发布
    token = p.authorizer.issue_grant("DEV-CN-01", PRODUCT, "op-chen")
    info = p.authorizer.authorize_batch(token)
    print(f"新开批授权指向回滚发布: {info['release_id'] == r3.release_id}")


def main():
    scenario_1_happy_path()
    scenario_2_tamper_and_digest()
    scenario_3_duplicate_receipt()
    scenario_4_grayscale_pause()
    scenario_5_rollback()
    hr("全部核验场景结束")


if __name__ == "__main__":
    main()
