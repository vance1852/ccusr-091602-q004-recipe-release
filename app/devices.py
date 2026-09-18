"""设备台账、能力校验与模拟设备网关。

- 设备按组（group）组织，发布按组分批下发；
- 能力校验：设备须支持该产品、支持配方全部参数且量程覆盖配方取值；
- 网关是可编程的模拟实现：可让指定设备超时、收到被篡改的报文或拒绝执行，
  用于演练“回执缺失 / 摘要不一致即停止扩散”的链路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .catalog import ParameterCatalog
from .errors import DeviceCapabilityError
from .recipe import RecipeDraft, canonical_content
from .signing import KeyStore, SignedMessage, sign

DEVICE_KEY_PREFIX = "device:"


def device_signer(device_id: str) -> str:
    return f"{DEVICE_KEY_PREFIX}{device_id}"


@dataclass
class Device:
    device_id: str
    group: str
    products: frozenset[str]  # 可生产的产品
    param_ranges: dict[str, tuple[float, float]]  # 支持的参数及量程（声明单位）
    key: bytes = b""  # 设备回执签名密钥


@dataclass
class DeviceCommand:
    command_id: str
    release_id: str
    device_id: str
    group: str
    content_digest: str
    content: dict  # 规范化配方内容（声明单位）
    dispatched_at: str


@dataclass
class DeviceReceipt:
    command_id: str
    release_id: str
    device_id: str
    received_digest: str  # 设备侧实际收到的内容摘要
    result: str  # applied | digest_mismatch | timeout | rejected
    received_at: str
    message: SignedMessage | None  # 超时回执由本地合成，无设备签名


def check_device_compatible(device: Device, draft: RecipeDraft, catalog: ParameterCatalog) -> None:
    """发布冻结前对每台目标设备做能力校验。"""
    if draft.product_id not in device.products:
        raise DeviceCapabilityError(
            f"设备 {device.device_id} 不支持产品 {draft.product_id}"
        )
    content = canonical_content(draft, catalog)
    for key, spec in content["params"].items():
        if key not in device.param_ranges:
            raise DeviceCapabilityError(
                f"设备 {device.device_id} 不支持参数 {key}"
            )
        lo, hi = device.param_ranges[key]
        if not (lo <= spec["value"] <= hi):
            raise DeviceCapabilityError(
                f"设备 {device.device_id} 参数 {key} 量程 [{lo}, {hi}] 不覆盖取值 {spec['value']}"
            )


# 设备行为：ok 正常应用；timeout 无回执；corrupt 传输被篡改（摘要不一致）；reject 拒绝执行
DeviceBehavior = str | Callable[[DeviceCommand], str]


class SimulatedDeviceGateway:
    """模拟下发通道：逐台派发指令并收集设备签名回执。"""

    def __init__(self, devices: dict[str, Device], keystore: KeyStore) -> None:
        self.devices = devices
        self.keystore = keystore
        self.behaviors: dict[str, DeviceBehavior] = {}
        self.sent_commands: list[DeviceCommand] = []

    def set_behavior(self, device_id: str, behavior: DeviceBehavior) -> None:
        self.behaviors[device_id] = behavior

    def dispatch(self, command: DeviceCommand, now: datetime) -> DeviceReceipt | None:
        """返回 None 表示超时（回执缺失）。"""
        from .recipe import canonical_json
        from hashlib import sha256

        self.sent_commands.append(command)
        device = self.devices[command.device_id]
        behavior = self.behaviors.get(command.device_id, "ok")
        if callable(behavior):
            behavior = behavior(command)

        if behavior == "timeout":
            return None

        received = command.content
        if behavior == "corrupt":
            # 模拟传输篡改：改动一个参数值，设备据此算出的摘要将与指令摘要不符
            received = dict(command.content)
            params = dict(received["params"])
            first_key = sorted(params)[0]
            params[first_key] = {**params[first_key], "value": params[first_key]["value"] + 1}
            received["params"] = params

        received_digest = sha256(canonical_json(received).encode("utf-8")).hexdigest()
        if behavior == "reject":
            result = "rejected"
        elif received_digest != command.content_digest:
            result = "digest_mismatch"
        else:
            result = "applied"

        payload = {
            "command_id": command.command_id,
            "release_id": command.release_id,
            "device_id": command.device_id,
            "received_digest": received_digest,
            "result": result,
        }
        message = sign(self.keystore, device_signer(command.device_id), "device", payload, now)
        return DeviceReceipt(
            command_id=command.command_id,
            release_id=command.release_id,
            device_id=command.device_id,
            received_digest=received_digest,
            result=result,
            received_at=message.signed_at,
            message=message,
        )
