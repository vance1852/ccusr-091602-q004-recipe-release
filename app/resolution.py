"""操作员开批前的适用版本解析。

规则：
- 按（设备, 产品）解析出唯一当前版本 —— 标记只由 applied 回执写入，天然唯一；
- 设备不在发布的灰度范围内 → 拒绝；
- 授权窗口过期（或尚未生效）→ 拒绝；
- 没有任何已生效版本 → 拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import (
    AuthorizationExpiredError,
    DeviceOutOfScopeError,
    NoApplicableVersionError,
)
from .release import ReleaseManager, ReleaseState
from .signing import iso


@dataclass(frozen=True)
class ApplicableVersion:
    device_id: str
    product_id: str
    release_id: str
    recipe_id: str
    recipe_version: int
    content_digest: str
    params: dict  # 声明单位的规范化参数，可直接下发设备执行
    valid_from: str
    valid_until: str


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def resolve_applicable(
    manager: ReleaseManager,
    device_id: str,
    product_id: str,
    now: datetime,
) -> ApplicableVersion:
    device = manager.devices.get(device_id)
    if device is None:
        raise DeviceOutOfScopeError(f"设备 {device_id} 未登记在设备台账中")

    marking = manager.device_marking(device_id, product_id)
    if marking is None:
        raise NoApplicableVersionError(f"设备 {device_id} 对产品 {product_id} 没有已生效版本")

    release = manager.releases[marking.release_id]
    if device.group not in release.groups:
        raise DeviceOutOfScopeError(
            f"设备 {device_id}（组 {device.group}）不在发布 {release.release_id} 的灰度范围 {release.groups} 内"
        )
    if release.state == ReleaseState.ROLLED_BACK:
        raise NoApplicableVersionError(
            f"发布 {release.release_id} 已回滚，等待回滚发布在设备 {device_id} 上生效"
        )

    now_utc = now.astimezone(timezone.utc)
    if not (_parse(marking.valid_from) <= now_utc <= _parse(marking.valid_until)):
        raise AuthorizationExpiredError(
            f"设备 {device_id} 的授权窗口 [{marking.valid_from}, {marking.valid_until}] 不含当前时刻 {iso(now_utc)}"
        )

    content = manager.content_store[marking.content_digest]
    return ApplicableVersion(
        device_id=device_id,
        product_id=product_id,
        release_id=release.release_id,
        recipe_id=release.recipe_id,
        recipe_version=release.recipe_version,
        content_digest=marking.content_digest,
        params={k: dict(v) for k, v in content["params"].items()},
        valid_from=marking.valid_from,
        valid_until=marking.valid_until,
    )
