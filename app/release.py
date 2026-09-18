"""发布管理：冻结 → 分批下发 → 完成 / 暂停 / 回滚。

核心语义：
- 冻结时把规范化内容存入内容库，发布记录只引用内容摘要；
- 按设备组分批下发，每台设备一条指令、一条（签名）回执；
- 回执缺失（超时）或摘要不一致时立即停止扩散：发布转入 paused，
  已成功的设备保留当前版本标记，未下发的设备不受影响；
- 重复回执幂等处理，矛盾回执报错；
- 回滚不是覆盖：创建一条引用旧内容摘要的新发布，走完同样的下发流程，
  全部生效后原发布才标记 rolled_back。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .catalog import ParameterCatalog
from .devices import (
    Device,
    DeviceCommand,
    DeviceReceipt,
    SimulatedDeviceGateway,
    check_device_compatible,
)
from .errors import (
    ConflictingReceiptError,
    ReleaseStateError,
    SignatureMismatchError,
    UnknownCommandError,
)
from .recipe import RecipeDraft, canonical_content, content_digest
from .review import ReviewRecord, ReviewState
from .signing import KeyStore, iso, verify


class ReleaseState(str, Enum):
    FROZEN = "frozen"
    DEPLOYING = "deploying"
    PAUSED = "paused"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


RESULT_APPLIED = "applied"
RESULT_DIGEST_MISMATCH = "digest_mismatch"
RESULT_TIMEOUT = "timeout"
RESULT_REJECTED = "rejected"
_FAILURE_RESULTS = (RESULT_DIGEST_MISMATCH, RESULT_TIMEOUT, RESULT_REJECTED)


@dataclass
class AppliedMarking:
    """设备当前版本标记：只有收到 applied 回执才会写入。"""

    device_id: str
    product_id: str
    release_id: str
    content_digest: str
    applied_at: str
    valid_from: str
    valid_until: str


@dataclass
class Release:
    release_id: str
    recipe_id: str
    product_id: str
    recipe_version: int
    content_digest: str
    kind: str  # "normal" | "rollback"
    groups: list[str]
    created_by: str
    created_at: str
    valid_from: str
    valid_until: str
    state: ReleaseState = ReleaseState.FROZEN
    restores: str | None = None  # 回滚发布引用的旧发布
    rolls_back: str | None = None  # 回滚发布要取代的发布
    supersedes: str | None = None  # 常规发布取代的前一发布
    pause_reason: str | None = None
    commands: dict[str, DeviceCommand] = field(default_factory=dict)
    receipts: dict[str, DeviceReceipt] = field(default_factory=dict)
    duplicate_receipts: int = 0
    deployed_groups: list[str] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)  # device_id -> 已派发次数

    def device_succeeded(self, device_id: str) -> bool:
        return any(
            c.device_id == device_id
            and (r := self.receipts.get(c.command_id)) is not None
            and r.result == RESULT_APPLIED
            for c in self.commands.values()
        )

    def applied_devices(self) -> list[str]:
        return sorted({c.device_id for c in self.commands.values() if self.device_succeeded(c.device_id)})


@dataclass
class BatchResult:
    group: str
    dispatched: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    halted: bool = False


class ReleaseManager:
    def __init__(
        self,
        catalog: ParameterCatalog,
        devices: dict[str, Device],
        keystore: KeyStore,
        gateway: SimulatedDeviceGateway,
    ) -> None:
        self.catalog = catalog
        self.devices = devices
        self.keystore = keystore
        self.gateway = gateway
        self.content_store: dict[str, dict] = {}  # digest -> 规范化内容
        self.releases: dict[str, Release] = {}
        self.markings: dict[tuple[str, str], AppliedMarking] = {}  # (device, product) -> 当前版本
        self._counter = itertools.count(1)

    # ---- 冻结 ----

    def freeze(
        self,
        review: ReviewRecord,
        groups: list[str],
        by: str,
        valid_from: datetime,
        valid_until: datetime,
        now: datetime,
    ) -> Release:
        """冻结审核通过的配方：先验签审核链，再做设备能力校验，最后落内容摘要。"""
        if review.state != ReviewState.APPROVED:
            raise ReleaseStateError(f"审核状态 {review.state.value}，不能冻结发布")
        if not review.verify_chain(self.keystore):
            raise SignatureMismatchError("审核链签名验签失败，拒绝冻结（内容可能被篡改）")
        if valid_until <= valid_from:
            raise ReleaseStateError("授权窗口无效：valid_until 必须晚于 valid_from")

        draft = review.draft
        digest = content_digest(draft, self.catalog)
        if digest != review.digest:
            raise SignatureMismatchError("配方内容摘要与审核时摘要不一致")

        targets = self._targets(groups)
        for device in targets:
            check_device_compatible(device, draft, self.catalog)

        release = Release(
            release_id=f"REL-{next(self._counter):04d}",
            recipe_id=draft.recipe_id,
            product_id=draft.product_id,
            recipe_version=draft.version,
            content_digest=digest,
            kind="normal",
            groups=list(groups),
            created_by=by,
            created_at=iso(now),
            valid_from=iso(valid_from),
            valid_until=iso(valid_until),
            supersedes=self._current_release_id(draft.product_id),
        )
        self.content_store[digest] = canonical_content(draft, self.catalog)
        self.releases[release.release_id] = release
        return release

    def _targets(self, groups: list[str]) -> list[Device]:
        targets = [d for d in self.devices.values() if d.group in groups]
        if not targets:
            raise ReleaseStateError(f"设备组 {groups} 内没有可用设备")
        return targets

    def _current_release_id(self, product_id: str) -> str | None:
        candidates = [
            r for r in self.releases.values()
            if r.product_id == product_id and r.kind == "normal"
            and r.state in (ReleaseState.DEPLOYING, ReleaseState.PAUSED, ReleaseState.COMPLETED)
        ]
        if not candidates:
            return None
        return candidates[-1].release_id  # 字典按插入序，取最新一条

    # ---- 下发 ----

    def start(self, release_id: str) -> None:
        release = self._get(release_id)
        if release.state != ReleaseState.FROZEN:
            raise ReleaseStateError(f"发布 {release_id} 状态 {release.state.value}，不能启动下发")
        release.state = ReleaseState.DEPLOYING

    def deploy_next_batch(self, release_id: str, now: datetime) -> BatchResult:
        """下发下一个未完成的设备组；任一设备失败即停止扩散并暂停。"""
        release = self._get(release_id)
        if release.state != ReleaseState.DEPLOYING:
            raise ReleaseStateError(f"发布 {release_id} 状态 {release.state.value}，不能继续下发")

        group = next((g for g in release.groups if g not in release.deployed_groups), None)
        if group is None:
            self._complete(release)
            return BatchResult(group="", halted=False)

        result = BatchResult(group=group)
        for device in self.devices.values():
            if device.group != group:
                continue
            if release.device_succeeded(device.device_id):
                continue  # 该设备此前已成功（暂停恢复后重入）
            attempt = release.attempts.get(device.device_id, 0) + 1
            release.attempts[device.device_id] = attempt
            command_id = f"{release.release_id}:{device.device_id}#{attempt}"
            command = DeviceCommand(
                command_id=command_id,
                release_id=release.release_id,
                device_id=device.device_id,
                group=group,
                content_digest=release.content_digest,
                content=self.content_store[release.content_digest],
                dispatched_at=iso(now),
            )
            release.commands[command_id] = command
            result.dispatched.append(device.device_id)
            receipt = self.gateway.dispatch(command, now)
            if receipt is None:
                receipt = DeviceReceipt(
                    command_id=command_id,
                    release_id=release.release_id,
                    device_id=device.device_id,
                    received_digest="",
                    result=RESULT_TIMEOUT,
                    received_at=iso(now),
                    message=None,
                )
            self.record_receipt(release_id, receipt)
            if receipt.result == RESULT_APPLIED:
                result.applied.append(device.device_id)
            else:
                result.failed.append(device.device_id)
            if release.state != ReleaseState.DEPLOYING:
                result.halted = True
                break  # 停止扩散：同组剩余设备与后续组都不再下发

        if not result.halted:
            release.deployed_groups.append(group)
            if len(release.deployed_groups) == len(release.groups):
                self._complete(release)
        return result

    def record_receipt(self, release_id: str, receipt: DeviceReceipt) -> bool:
        """登记回执；返回 True 表示这是重复回执（幂等忽略）。

        - 未知指令 → UnknownCommandError
        - 重复且内容一致 → 计数并忽略
        - 重复但内容矛盾 → ConflictingReceiptError
        - 设备签名无效 → SignatureMismatchError
        - 失败类回执 → 立即停止扩散（paused）
        """
        release = self._get(release_id)
        command = release.commands.get(receipt.command_id)
        if command is None:
            raise UnknownCommandError(f"回执指向未知指令 {receipt.command_id}")

        if receipt.command_id in release.receipts:
            prev = release.receipts[receipt.command_id]
            if prev.result == receipt.result and prev.received_digest == receipt.received_digest:
                release.duplicate_receipts += 1
                return True
            raise ConflictingReceiptError(
                f"指令 {receipt.command_id} 的回执矛盾: {prev.result}/{prev.received_digest} "
                f"vs {receipt.result}/{receipt.received_digest}"
            )

        if receipt.message is not None and not verify(self.keystore, receipt.message):
            raise SignatureMismatchError(f"设备 {receipt.device_id} 回执签名验签失败")

        release.receipts[receipt.command_id] = receipt

        digest_ok = receipt.received_digest == command.content_digest
        if receipt.result == RESULT_APPLIED and digest_ok:
            self.markings[(receipt.device_id, release.product_id)] = AppliedMarking(
                device_id=receipt.device_id,
                product_id=release.product_id,
                release_id=release.release_id,
                content_digest=release.content_digest,
                applied_at=receipt.received_at,
                valid_from=release.valid_from,
                valid_until=release.valid_until,
            )
        elif receipt.result in _FAILURE_RESULTS or not digest_ok:
            release.state = ReleaseState.PAUSED
            release.pause_reason = (
                f"halted: 设备 {receipt.device_id} 回执 {receipt.result}"
                + ("" if digest_ok else "（摘要与指令不符）")
            )
        return False

    # ---- 灰度暂停 / 恢复 ----

    def pause(self, release_id: str, by: str, reason: str = "manual") -> None:
        release = self._get(release_id)
        if release.state != ReleaseState.DEPLOYING:
            raise ReleaseStateError(f"发布 {release_id} 状态 {release.state.value}，不能暂停")
        release.state = ReleaseState.PAUSED
        release.pause_reason = f"manual: {reason} (by {by})"

    def resume(self, release_id: str, by: str) -> None:
        release = self._get(release_id)
        if release.state != ReleaseState.PAUSED:
            raise ReleaseStateError(f"发布 {release_id} 状态 {release.state.value}，不能恢复")
        release.state = ReleaseState.DEPLOYING
        release.pause_reason = None

    def _complete(self, release: Release) -> None:
        release.state = ReleaseState.COMPLETED
        if release.kind == "rollback" and release.rolls_back:
            self.releases[release.rolls_back].state = ReleaseState.ROLLED_BACK

    # ---- 回滚 ----

    def rollback(
        self,
        release_id: str,
        by: str,
        reason: str,
        valid_from: datetime,
        valid_until: datetime,
        now: datetime,
        restore_release_id: str | None = None,
    ) -> Release:
        """创建一条引用旧内容的新发布（不覆盖任何记录）。"""
        target = self._get(release_id)
        if target.state not in (ReleaseState.COMPLETED, ReleaseState.PAUSED):
            raise ReleaseStateError(f"发布 {release_id} 状态 {target.state.value}，不能回滚")

        restore_id = restore_release_id or target.supersedes
        if restore_id is None or restore_id not in self.releases:
            raise ReleaseStateError(f"发布 {release_id} 没有可回滚到的历史发布")
        restore = self.releases[restore_id]
        if restore.content_digest not in self.content_store:
            raise ReleaseStateError(f"旧发布 {restore_id} 的内容已不在内容库中")

        release = Release(
            release_id=f"REL-{next(self._counter):04d}",
            recipe_id=restore.recipe_id,
            product_id=restore.product_id,
            recipe_version=restore.recipe_version,
            content_digest=restore.content_digest,
            kind="rollback",
            groups=list(target.groups),
            created_by=by,
            created_at=iso(now),
            valid_from=iso(valid_from),
            valid_until=iso(valid_until),
            restores=restore.release_id,
            rolls_back=target.release_id,
            supersedes=target.supersedes,
        )
        release.pause_reason = f"rollback of {target.release_id}: {reason}"
        self.releases[release.release_id] = release
        return release

    # ---- 查询 ----

    def _get(self, release_id: str) -> Release:
        try:
            return self.releases[release_id]
        except KeyError:
            raise ReleaseStateError(f"发布不存在: {release_id}") from None

    def device_marking(self, device_id: str, product_id: str) -> AppliedMarking | None:
        return self.markings.get((device_id, product_id))
