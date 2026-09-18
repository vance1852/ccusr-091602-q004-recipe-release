"""模拟机器人控制器/设备网关。

每台设备持有独立密钥，对收到的平台指令验签，并对回执签名。
通过切换控制器的行为模式，可以复现：

* ``honest``        —— 正常 applied 回执；
* ``digest_alarm``  —— 设备复算摘要不一致，回执 digest_mismatch；
* ``wrong_echo``    —— 回 applied 但回执里的摘要是另一个值；
* ``silent``        —— 不回回执（超时）；
* 另外提供篡改签名/重放旧回执的测试辅助。
"""

from __future__ import annotations

import copy
import uuid
from typing import Callable

from .crypto import (ALG_HS256, KeyRing, sign_message, tamper, verify_message)

PLATFORM_KEY_ID = "key:platform"


def device_key_id(device_id: str) -> str:
    return f"key:device:{device_id}"


class SimulatedController:
    MODE_HONEST = "honest"
    MODE_DIGEST_ALARM = "digest_alarm"
    MODE_WRONG_ECHO = "wrong_echo"
    MODE_SILENT = "silent"

    def __init__(self, device_id: str, key_ring: KeyRing,
                 clock: Callable[[], float],
                 mode: str = MODE_HONEST):
        self.device_id = device_id
        self.key_ring = key_ring
        self.clock = clock
        self.mode = mode
        self.last_command: dict | None = None
        self.applied: list[dict] = []  # 已生效指令

    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def deliver(self, command: dict) -> None:
        # 控制器先验平台签名
        verify_message(command, self.key_ring, expected_kid=PLATFORM_KEY_ID)
        self.last_command = copy.deepcopy(command)

    def respond(self) -> dict | None:
        """根据当前行为模式产生回执；silent 模式返回 None。"""
        if self.last_command is None or self.mode == self.MODE_SILENT:
            return None
        cmd = self.last_command
        if self.mode == self.MODE_DIGEST_ALARM:
            claimed = "sha256:" + "0" * 64
            result = "digest_mismatch"
        elif self.mode == self.MODE_WRONG_ECHO:
            claimed = "sha256:" + "f" * 64
            result = "applied"
        else:
            claimed = cmd["content_digest"]
            result = "applied"

        receipt = {
            "type": "receipt",
            "receipt_id": f"RCP-{uuid.uuid4().hex[:10].upper()}",
            "command_id": cmd["command_id"],
            "release_id": cmd["release_id"],
            "device_id": self.device_id,
            "content_digest": claimed,
            "result": result,
            "applied_version": cmd["release_id"] if result == "applied" else None,
            "ts": self.clock(),
        }
        signed = sign_message(receipt, self.key_ring,
                              device_key_id(self.device_id))
        if result == "applied" and self.mode == self.MODE_HONEST:
            self.applied.append(copy.deepcopy(cmd))
        return signed

    def replay_last_receipt(self, previous: dict) -> dict:
        """重放：原样再次提交一份旧回执（重复回执测试用）。"""
        return copy.deepcopy(previous)

    @staticmethod
    def tamper_receipt(receipt: dict, **changes) -> dict:
        return tamper(receipt, **changes)


class DeviceGateway:
    """平台侧网关：保存全部设备控制器，转发指令、接收回执。"""

    def __init__(self, key_ring: KeyRing, clock: Callable[[], float]):
        self.key_ring = key_ring
        self.clock = clock
        self.controllers: dict[str, SimulatedController] = {}

    def register_device(self, device_id: str,
                        mode: str = SimulatedController.MODE_HONEST
                        ) -> SimulatedController:
        ctrl = SimulatedController(device_id, self.key_ring, self.clock, mode)
        self.controllers[device_id] = ctrl
        return ctrl

    def controller(self, device_id: str) -> SimulatedController:
        return self.controllers[device_id]

    def issue(self, device_id: str, command: dict) -> None:
        self.controllers[device_id].deliver(command)

    def collect(self, device_id: str) -> dict | None:
        return self.controllers[device_id].respond()
