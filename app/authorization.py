"""开批授权：操作员开批前必须取得设备×产品的唯一适用版本。

拒绝情形：
* 设备尚无生效版本（尚未进入灰度范围 / 未成功应用）；
* 授权令牌缺失、签名被篡改、已过期或已撤销；
* 令牌引用的发布/摘要与设备当前标记版本不一致（发布已回滚或被新版本取代）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable

from .crypto import KeyRing, sign_message, verify_message
from .errors import AuthorizationDenied
from .release import ReleaseManager

AUTH_KID = "key:auth-service"


@dataclass
class BatchGrant:
    token: dict
    revoked: bool = False


class Authorizer:
    def __init__(self, releases: ReleaseManager, key_ring: KeyRing,
                 clock: Callable[[], float] = time.time,
                 default_ttl_seconds: float = 3600.0):
        self.releases = releases
        self.key_ring = key_ring
        self.clock = clock
        self.default_ttl = default_ttl_seconds
        self._grants: dict[str, BatchGrant] = {}

    def issue_grant(self, device_id: str, product_code: str,
                    operator_id: str, ttl_seconds: float | None = None
                    ) -> dict:
        """为一次开批签发授权令牌；设备在灰度外则直接拒签。"""
        mark = self.releases.active_version(device_id, product_code)
        if mark is None:
            raise AuthorizationDenied(
                f"设备 {device_id} 不在产品 {product_code} 当前灰度/生效范围内，"
                "拒绝签发开批授权")
        now = self.clock()
        token = {
            "type": "batch_grant",
            "grant_id": f"GRT-{uuid.uuid4().hex[:10].upper()}",
            "device_id": device_id,
            "product_code": product_code,
            "operator_id": operator_id,
            "release_id": mark.release_id,
            "content_digest": mark.content_digest,
            "iat": now,
            "exp": now + (self.default_ttl if ttl_seconds is None
                          else ttl_seconds),
        }
        token = sign_message(token, self.key_ring, AUTH_KID)
        self._grants[token["grant_id"]] = BatchGrant(token)
        return token

    def revoke(self, grant_id: str) -> None:
        if grant_id not in self._grants:
            raise KeyError(f"未知授权: {grant_id}")
        self._grants[grant_id].revoked = True

    def authorize_batch(self, token: dict,
                        at: float | None = None) -> dict:
        """开批闸门：返回适用版本信息，否则抛 :class:`AuthorizationDenied`。"""
        now = at if at is not None else self.clock()
        device_id = token.get("device_id", "?")
        try:
            verify_message(token, self.key_ring, expected_kid=AUTH_KID)
        except Exception as exc:  # noqa: BLE001
            raise AuthorizationDenied(f"授权令牌无效（{exc}）") from exc

        grant_id = token.get("grant_id")
        grant = self._grants.get(grant_id)
        if grant is None:
            raise AuthorizationDenied("授权令牌未登记")
        if grant.revoked:
            raise AuthorizationDenied("授权已被撤销")
        if now >= token["exp"]:
            raise AuthorizationDenied(
                f"授权已过期（到期时间 {token['exp']}，当前 {now}）", )
        if now < token["iat"]:
            raise AuthorizationDenied("授权尚未生效")

        mark = self.releases.active_version(token["device_id"],
                                            token["product_code"])
        if mark is None:
            raise AuthorizationDenied(
                f"设备 {device_id} 当前不在适用范围内（可能已退出灰度）")
        if mark.release_id != token["release_id"]:
            raise AuthorizationDenied(
                f"授权引用发布 {token['release_id']}，但设备当前适用版本为 "
                f"{mark.release_id}（版本已变更，请重新领取授权）")
        if mark.content_digest != token["content_digest"]:
            raise AuthorizationDenied(
                "授权摘要与设备当前生效摘要不一致（版本已回滚或更新）")
        return {
            "device_id": mark.device_id,
            "product_code": token["product_code"],
            "release_id": mark.release_id,
            "content_digest": mark.content_digest,
            "operator_id": token["operator_id"],
            "grant_id": grant_id,
        }
