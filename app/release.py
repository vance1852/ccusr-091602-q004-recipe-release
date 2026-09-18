"""发布编排：冻结摘要 → 分波灰度下发 → 回执门控 → 完成/回滚。

关键不变量：

1. 发布只引用内容摘要；内容对象在内容库中不可变。
2. 按设备组分波（默认 canary 试点组最先），当前波未全部 applied
   不允许扩散到下一波；出现 digest_mismatch / timeout / rejected
   立即进入 paused，停止扩散；已 applied 的设备继续标记当前版本。
3. 回执必须签名有效、命令匹配、设备匹配、摘要回显一致；同一
   命令重复回执被拒绝。
4. 回滚不是覆盖：旧发布置 rolled_back 并失效其版本标记，同时创建
   一项引用旧内容摘要的新发布，沿同一灰度流程下发。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .catalog import Registry
from .content import ContentStore
from .crypto import KeyRing, verify_message
from .devices import PLATFORM_KEY_ID, device_key_id
from .errors import (DigestMismatchError, DomainError, DuplicateReceiptError,
                     NotFoundError, ValidationError, WorkflowError)
from .recipe import RecipeWorkflow

STATE_FROZEN = "frozen"
STATE_DEPLOYING = "deploying"
STATE_PAUSED = "paused"
STATE_COMPLETED = "completed"
STATE_ROLLED_BACK = "rolled_back"

DEV_APPLIED = "applied"
DEV_MISMATCH = "digest_mismatch"
DEV_TIMEOUT = "timeout"
DEV_REJECTED = "rejected"
DEV_PENDING = "pending"          # 指令已发，等待回执
DEV_WAITING = "waiting_wave"     # 尚未轮到本设备所在波次

_FAIL_RESULTS = {DEV_MISMATCH, DEV_TIMEOUT, DEV_REJECTED}


@dataclass
class DeviceDelivery:
    device_id: str
    group: str
    wave: int
    status: str = DEV_WAITING
    command: dict | None = None
    receipt: dict | None = None
    error: str | None = None
    attempts: int = 0
    receipt_ids: list[str] = field(default_factory=list)
    updated_at: float | None = None


@dataclass
class Release:
    release_id: str
    change_id: str
    recipe_code: str
    product_code: str
    content_digest: str
    targets: list[str]
    wave_order: list[str]
    created_by: str
    created_at: float
    state: str = STATE_FROZEN
    current_wave: int = -1
    deliveries: dict[str, DeviceDelivery] = field(default_factory=dict)
    pause_reason: str | None = None
    rollback_of: str | None = None        # 回滚发布指向的原发布
    restores_digest: str | None = None    # 回滚发布恢复的旧摘要
    supersedes: str | None = None
    history: list[dict] = field(default_factory=list)

    def log(self, action: str, by: str, at: float, **extra) -> None:
        entry = {"action": action, "by": by, "at": at}
        entry.update(extra)
        self.history.append(entry)


@dataclass
class VersionMark:
    """某台设备当前标记生效的版本。"""

    device_id: str
    product_code: str
    release_id: str
    content_digest: str
    applied_at: float
    active: bool = True


class ReleaseManager:
    def __init__(self, registry: Registry, store: ContentStore,
                 key_ring: KeyRing, recipes: RecipeWorkflow,
                 gateway, clock: Callable[[], float] = time.time):
        self.registry = registry
        self.store = store
        self.key_ring = key_ring
        self.recipes = recipes
        self.gateway = gateway
        self.clock = clock
        self.releases: dict[str, Release] = {}
        self.marks: dict[tuple[str, str], list[VersionMark]] = {}

    # ============================================================ 创建发布
    def create_release(self, change_id: str, admin_id: str,
                       targets: list[str] | None = None,
                       wave_order: list[str] | None = None) -> Release:
        change = self.recipes.get(change_id)
        if change.state != "approved":
            raise WorkflowError(
                f"变更 {change_id} 状态为 {change.state}，审核通过后才能发布")
        if not change.content_digest or not self.store.contains(
                change.content_digest):
            raise WorkflowError("变更的内容摘要未冻结，无法发布")
        if targets is None:
            targets = sorted(self.registry.devices)
        targets = list(targets)
        if not targets:
            raise ValidationError(["发布目标设备列表为空"])
        for d in targets:
            self.registry.device(d)
        if len(set(targets)) != len(targets):
            raise ValidationError(["发布目标设备存在重复"])

        content = self.store.get(change.content_digest)
        parameters = content.payload["parameters"]
        capability = self.registry.check_targets(targets, parameters)
        issues = [f"{d}: {msg}" for d, msgs in capability.items()
                  for msg in msgs]
        if issues:
            raise ValidationError(issues, "设备能力预检未通过")

        groups = [self.registry.group_of(d) for d in targets]
        if wave_order is None:
            # 默认：canary 试点组最先，其余按组名稳定排序
            wave_order = sorted(set(groups),
                                key=lambda g: (g != "canary", g))
        unknown = set(wave_order) - set(groups)
        if unknown:
            raise ValidationError([f"波次包含无目标设备的组: {sorted(unknown)}"])
        if set(groups) - set(wave_order):
            raise ValidationError(["波次计划未覆盖全部目标设备组"])

        release = Release(
            release_id=f"REL-{uuid.uuid4().hex[:10].upper()}",
            change_id=change_id,
            recipe_code=change.recipe_code,
            product_code=change.product_code,
            content_digest=change.content_digest,
            targets=list(targets),
            wave_order=wave_order,
            created_by=admin_id,
            created_at=self.clock(),
            deliveries={d: DeviceDelivery(d, self.registry.group_of(d),
                                          wave_order.index(
                                              self.registry.group_of(d)))
                        for d in targets},
        )
        release.log("freeze", admin_id, release.created_at,
                    content_digest=release.content_digest,
                    waves=wave_order, targets=targets)
        self.releases[release.release_id] = release
        return release

    # ============================================================ 下发
    def start(self, release_id: str, admin_id: str) -> Release:
        r = self._get(release_id)
        if r.state != STATE_FROZEN:
            raise WorkflowError(f"仅 frozen 发布可启动，当前 {r.state}")
        r.state = STATE_DEPLOYING
        r.log("start", admin_id, self.clock())
        self._dispatch_wave(r, admin_id)
        return r

    def _dispatch_wave(self, r: Release, by: str) -> None:
        r.current_wave += 1
        group = r.wave_order[r.current_wave]
        content = self.store.get(r.content_digest)
        params = content.payload["parameters"]
        for device_id in r.targets:
            d = r.deliveries[device_id]
            if d.wave != r.current_wave or d.status not in (
                    DEV_WAITING, None):
                continue
            model = self.registry.model_of(device_id)
            cmd_params = self._native_parameters(model, params)
            command = {
                "type": "device_command",
                "command_id": f"CMD-{uuid.uuid4().hex[:12].upper()}",
                "release_id": r.release_id,
                "wave": r.current_wave,
                "group": group,
                "device_id": device_id,
                "recipe_code": r.recipe_code,
                "product_code": r.product_code,
                "content_digest": r.content_digest,
                "parameters": cmd_params,
                "issued_at": self.clock(),
            }
            from .crypto import sign_message
            command = sign_message(command, self.key_ring, PLATFORM_KEY_ID)
            d.command = command
            d.status = DEV_PENDING
            d.attempts += 1
            d.error = None
            d.updated_at = self.clock()
            self.gateway.issue(device_id, command)
        r.log("dispatch_wave", by, self.clock(), wave=r.current_wave,
              group=group)

    @staticmethod
    def _native_parameters(model, parameters: dict) -> dict:
        from . import units
        out = {}
        for key, item in parameters.items():
            cap = model.capabilities.get(key)
            if cap is None:
                continue
            if cap.native_unit is None:
                out[key] = {"value": item["value"], "unit": None}
            else:
                out[key] = {
                    "value": units.convert(item["value"], item.get("unit"),
                                           cap.native_unit),
                    "unit": cap.native_unit,
                }
        return out

    # ============================================================ 回执
    def receive_receipt(self, release_id: str, receipt: dict,
                        received_at: float | None = None) -> DeviceDelivery:
        r = self._get(release_id)
        at = received_at or self.clock()
        # 1) 签名校验
        verify_message(receipt, self.key_ring,
                       expected_kid=device_key_id(receipt.get("device_id", "")))
        # 2) 字段匹配
        rid = receipt.get("receipt_id")
        cmd_id = receipt.get("command_id")
        device_id = receipt.get("device_id")
        if receipt.get("release_id") != r.release_id:
            raise WorkflowError("回执不属于本发布")
        if device_id not in r.deliveries:
            raise WorkflowError(f"回执来自非目标设备: {device_id}")
        d = r.deliveries[device_id]
        if d.command is None or cmd_id != d.command["command_id"]:
            raise WorkflowError(
                f"回执命令号 {cmd_id} 与当前待回执指令不匹配（可能是过期重放）")
        if rid in d.receipt_ids:
            raise DuplicateReceiptError(
                f"设备 {device_id} 的回执 {rid} 已处理，禁止重复入账")
        for other in r.deliveries.values():
            if other is not d and rid in other.receipt_ids:
                raise DuplicateReceiptError(
                    f"回执 {rid} 已由其他设备入账，禁止重复使用")

        result = receipt.get("result")
        if result not in (DEV_APPLIED, DEV_MISMATCH, DEV_TIMEOUT,
                          DEV_REJECTED):
            raise WorkflowError(f"未知回执结果: {result!r}")

        # 3) applied 必须回显一致摘要
        if result == DEV_APPLIED and \
                receipt.get("content_digest") != r.content_digest:
            result = DEV_MISMATCH
            d.error = ("设备回报 applied 但摘要回显不一致："
                       f"{receipt.get('content_digest')} != {r.content_digest}")
        elif result != DEV_APPLIED:
            d.error = f"设备结果: {result}"

        d.receipt = receipt
        d.receipt_ids.append(rid)
        d.status = result
        d.updated_at = at
        r.log("receipt", receipt.get("kid", device_id), at,
              device=device_id, result=result, receipt_id=rid)

        if result == DEV_APPLIED:
            self._mark_version(r, device_id, at)
        else:
            self._halt(r, by=device_id, at=at,
                       reason=f"设备 {device_id} {result}，停止扩散")
        return d

    def collect_from_device(self, release_id: str,
                            device_id: str) -> DeviceDelivery:
        """从模拟网关拉取回执并入账；设备静默时不做处理。"""
        receipt = self.gateway.collect(device_id)
        if receipt is None:
            raise NotFoundError(f"设备 {device_id} 暂无回执")
        return self.receive_receipt(release_id, receipt)

    def collect_wave(self, release_id: str) -> dict[str, str]:
        """收集当前波所有设备的回执并逐台入账，返回设备→结果。"""
        r = self._get(release_id)
        outcome = {}
        for device_id, d in r.deliveries.items():
            if d.wave == r.current_wave and d.status == DEV_PENDING:
                receipt = self.gateway.collect(device_id)
                if receipt is None:
                    continue
                self.receive_receipt(release_id, receipt)
                outcome[device_id] = r.deliveries[device_id].status
        return outcome

    def mark_timeout(self, release_id: str, device_id: str,
                     admin_id: str) -> DeviceDelivery:
        """回执缺失：登记超时并停止扩散。"""
        r = self._get(release_id)
        d = r.deliveries[device_id]
        if d.status != DEV_PENDING:
            raise WorkflowError(
                f"设备 {device_id} 当前状态 {d.status}，不可登记超时")
        at = self.clock()
        d.status = DEV_TIMEOUT
        d.error = "回执超时缺失"
        d.updated_at = at
        r.log("timeout", admin_id, at, device=device_id)
        self._halt(r, by=admin_id, at=at,
                   reason=f"设备 {device_id} 回执超时，停止扩散")
        return d

    # ============================================================ 波次门控
    def _halt(self, r: Release, by: str, at: float, reason: str) -> None:
        if r.state != STATE_PAUSED:
            r.state = STATE_PAUSED
            r.pause_reason = reason
            r.log("halt", by, at, reason=reason)

    def wave_status(self, release_id: str) -> dict:
        r = self._get(release_id)
        waves: list[dict] = []
        for idx, group in enumerate(r.wave_order):
            members = [d for d in r.deliveries.values() if d.wave == idx]
            waves.append({
                "wave": idx, "group": group,
                "total": len(members),
                "applied": sum(d.status == DEV_APPLIED for d in members),
                "failed": [d.device_id for d in members
                           if d.status in _FAIL_RESULTS],
                "pending": [d.device_id for d in members
                            if d.status == DEV_PENDING],
                "waiting": [d.device_id for d in members
                            if d.status == DEV_WAITING],
            })
        return {"release_id": r.release_id, "state": r.state,
                "current_wave": r.current_wave, "waves": waves,
                "pause_reason": r.pause_reason}

    def promote_wave(self, release_id: str, admin_id: str) -> Release:
        """灰度门控：当前波全部 applied 后才允许扩散到下一波。"""
        r = self._get(release_id)
        if r.state == STATE_PAUSED:
            raise WorkflowError(f"发布已暂停（{r.pause_reason}），"
                                "请先处置失败设备或回滚")
        if r.state != STATE_DEPLOYING:
            raise WorkflowError(f"当前状态 {r.state}，不可推进波次")
        active = [d for d in r.deliveries.values()
                  if d.wave == r.current_wave]
        if any(d.status != DEV_APPLIED for d in active):
            pending = [d.device_id for d in active
                       if d.status != DEV_APPLIED]
            raise WorkflowError(f"当前波尚有设备未成功: {pending}")
        if r.current_wave + 1 >= len(r.wave_order):
            r.state = STATE_COMPLETED
            r.log("complete", admin_id, self.clock())
            return r
        self._dispatch_wave(r, admin_id)
        return r

    # ============================================================ 暂停/恢复/重试
    def pause(self, release_id: str, admin_id: str,
              reason: str = "人工灰度暂停") -> Release:
        r = self._get(release_id)
        if r.state != STATE_DEPLOYING:
            raise WorkflowError(f"仅 deploying 可暂停，当前 {r.state}")
        r.state = STATE_PAUSED
        r.pause_reason = reason
        r.log("pause", admin_id, self.clock(), reason=reason)
        return r

    def resume(self, release_id: str, admin_id: str) -> Release:
        r = self._get(release_id)
        if r.state != STATE_PAUSED:
            raise WorkflowError(f"仅 paused 可恢复，当前 {r.state}")
        failed = [d.device_id for d in r.deliveries.values()
                  if d.status in _FAIL_RESULTS]
        if failed:
            raise WorkflowError(
                f"仍有失败设备 {failed}，请先重试单台或回滚，不能直接恢复扩散")
        r.state = STATE_DEPLOYING
        r.pause_reason = None
        r.log("resume", admin_id, self.clock())
        return r

    def retry_device(self, release_id: str, device_id: str,
                     admin_id: str) -> DeviceDelivery:
        """对失败设备重新签发指令（控制器恢复后使用）。"""
        r = self._get(release_id)
        if r.state not in (STATE_PAUSED, STATE_DEPLOYING):
            raise WorkflowError(f"当前状态 {r.state}，不可重试设备")
        d = r.deliveries[device_id]
        if d.status not in _FAIL_RESULTS and d.status != DEV_PENDING:
            raise WorkflowError(f"设备 {device_id} 状态 {d.status}，无需重试")
        # 以新指令重发；旧回执保留审计
        d.status = DEV_WAITING
        d.command = None
        d.receipt = None
        d.error = None
        d.wave = r.current_wave if d.wave <= r.current_wave else d.wave
        self._dispatch_one(r, d)
        r.log("retry", admin_id, self.clock(), device=device_id,
              attempt=d.attempts)
        return d

    def _dispatch_one(self, r: Release, d: DeviceDelivery) -> None:
        content = self.store.get(r.content_digest)
        model = self.registry.model_of(d.device_id)
        import uuid as _uuid
        from .crypto import sign_message
        command = {
            "type": "device_command",
            "command_id": f"CMD-{_uuid.uuid4().hex[:12].upper()}",
            "release_id": r.release_id,
            "wave": d.wave,
            "group": d.group,
            "device_id": d.device_id,
            "recipe_code": r.recipe_code,
            "product_code": r.product_code,
            "content_digest": r.content_digest,
            "parameters": self._native_parameters(
                model, content.payload["parameters"]),
            "issued_at": self.clock(),
        }
        command = sign_message(command, self.key_ring, PLATFORM_KEY_ID)
        d.command = command
        d.status = DEV_PENDING
        d.attempts += 1
        d.updated_at = self.clock()
        self.gateway.issue(d.device_id, command)

    # ============================================================ 回滚
    def rollback(self, release_id: str, admin_id: str,
                 reason: str, restore_digest: str | None = None,
                 targets: list[str] | None = None) -> Release:
        """回滚：原发布置 rolled_back，创建引用旧内容的新发布。"""
        r = self._get(release_id)
        if r.state == STATE_ROLLED_BACK:
            raise WorkflowError("发布已回滚")
        if r.rollback_of is not None:
            raise WorkflowError("回滚发布不能再次回滚")

        target_digest = restore_digest
        if target_digest is None:
            prior = self._previous_release(r)
            target_digest = prior.content_digest if prior else None
        if target_digest is None:
            raise WorkflowError("找不到可恢复的旧版本，请显式提供摘要")
        if not self.store.contains(target_digest):
            raise NotFoundError(f"旧内容摘要不存在于内容库: {target_digest}")
        if target_digest == r.content_digest:
            raise WorkflowError("回滚目标与当前版本相同")

        at = self.clock()
        # 默认只回滚实际已应用本发布的设备；其余设备仍持有旧版本，无需重发
        if targets is None:
            affected = [d for d in r.targets
                        if (m := self._active_mark(d, r.product_code))
                        and m.release_id == r.release_id]
            rollback_targets = sorted(affected)
        else:
            rollback_targets = list(targets)
        if not rollback_targets:
            raise WorkflowError("没有设备实际应用过该发布，无需回滚")
        # 失效原发布在各设备上的版本标记
        for device_id in rollback_targets:
            self._deactivate(device_id, r.product_code, r.release_id)
        rb_groups = [g for g in r.wave_order
                     if any(self.registry.group_of(d) == g
                            for d in rollback_targets)]
        rb = Release(
            release_id=f"REL-{uuid.uuid4().hex[:10].upper()}",
            change_id=r.change_id,
            recipe_code=r.recipe_code,
            product_code=r.product_code,
            content_digest=target_digest,
            targets=list(rollback_targets),
            wave_order=rb_groups,
            created_by=admin_id,
            created_at=at,
            rollback_of=r.release_id,
            restores_digest=target_digest,
            supersedes=r.release_id,
        )
        rb.deliveries = {
            d: DeviceDelivery(d, self.registry.group_of(d),
                              rb_groups.index(self.registry.group_of(d)))
            for d in rollback_targets}
        rb.log("rollback_created", admin_id, at,
               rollback_of=r.release_id, restores_digest=target_digest,
               reason=reason)

        r.state = STATE_ROLLED_BACK
        r.pause_reason = None
        r.log("rolled_back", admin_id, at, reason=reason,
              rollback_release=rb.release_id)
        self.releases[rb.release_id] = rb
        return rb

    def _previous_release(self, r: Release) -> Release | None:
        candidates = [x for x in self.releases.values()
                      if x.product_code == r.product_code
                      and x.recipe_code == r.recipe_code
                      and x.release_id != r.release_id
                      and x.state != STATE_ROLLED_BACK
                      and x.content_digest != r.content_digest
                      and x.rollback_of is None]
        candidates.sort(key=lambda x: x.created_at, reverse=True)
        return candidates[0] if candidates else None

    # ============================================================ 版本台账
    def _mark_version(self, r: Release, device_id: str, at: float) -> None:
        key = (device_id, r.product_code)
        chain = self.marks.setdefault(key, [])
        for m in chain:
            if m.active and m.release_id != r.release_id:
                m.active = False  # 新版本覆盖旧适用版本
        chain.append(VersionMark(device_id, r.product_code, r.release_id,
                                 r.content_digest, at, True))

    def _deactivate(self, device_id: str, product_code: str,
                    release_id: str) -> None:
        for m in self.marks.get((device_id, product_code), []):
            if m.release_id == release_id:
                m.active = False

    def _active_mark(self, device_id: str, product_code: str):
        for m in reversed(self.marks.get((device_id, product_code), [])):
            if m.active:
                return m
        return None

    def active_version(self, device_id: str, product_code: str,
                       at: float | None = None) -> VersionMark | None:
        """设备×产品当前唯一适用版本；无生效版本时返回 None。"""
        active = [m for m in self.marks.get((device_id, product_code), [])
                  if m.active]
        if len(active) > 1:
            digests = {m.content_digest for m in active}
            if len(digests) > 1:
                raise DomainError(
                    f"设备 {device_id} 产品 {product_code} 存在多个冲突的"
                    f"生效版本: {sorted(digests)}")
        return active[-1] if active else None

    def applicability_map(self, product_code: str) -> list[dict]:
        rows = []
        for device_id in sorted(self.registry.devices):
            m = self.active_version(device_id, product_code)
            rows.append({
                "device_id": device_id,
                "in_scope": m is not None,
                "release_id": m.release_id if m else None,
                "content_digest": m.content_digest if m else None,
                "applied_at": m.applied_at if m else None,
            })
        return rows

    # ============================================================ 查询
    def _get(self, release_id: str) -> Release:
        if release_id not in self.releases:
            raise KeyError(f"未知发布: {release_id}")
        return self.releases[release_id]

    def get(self, release_id: str) -> Release:
        return self._get(release_id)
