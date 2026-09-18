"""消息签名与验签。

外部消息（审核签名、设备指令、设备回执）均携带 ``alg`` / ``kid`` /
``sig`` 三个原始字段，不做封装改写。默认算法 ``HS256``（HMAC-SHA256，
与仓库约定的消息格式一致）；签名内容为剔除 ``sig`` 字段后的规范
JSON 字节。另提供不依赖密钥的内容摘要校验 ``sha256`` 透传。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
from typing import Any

from .content import canonical_json, digest_of
from .errors import SignatureError

SIG_FIELD = "sig"
ALG_HS256 = "HS256"
ALG_SHA256 = "SHA256"
SUPPORTED_ALGS = (ALG_HS256, ALG_SHA256)


class KeyRing:
    """密钥标识 -> 密钥材料。审核人、设备各持独立密钥。"""

    def __init__(self):
        self._secrets: dict[str, bytes] = {}

    def register(self, key_id: str, secret: str | bytes) -> None:
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        self._secrets[key_id] = secret

    def secret(self, key_id: str) -> bytes:
        if key_id not in self._secrets:
            raise SignatureError(f"未知密钥标识: {key_id}")
        return self._secrets[key_id]

    def has(self, key_id: str) -> bool:
        return key_id in self._secrets


def _signing_bytes(message: dict) -> bytes:
    body = {k: v for k, v in message.items() if k != SIG_FIELD}
    return canonical_json(body)


def compute_hmac(message: dict, secret: bytes) -> str:
    mac = hmac.new(secret, _signing_bytes(message), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


def sign_message(message: dict, key_ring: KeyRing, key_id: str,
                 alg: str = ALG_HS256) -> dict:
    """返回带 ``alg``/``kid``/``sig`` 原始字段的新消息（不修改入参）。"""
    if alg not in SUPPORTED_ALGS:
        raise SignatureError(f"不支持的签名算法: {alg}")
    signed = copy.deepcopy(message)
    signed["alg"] = alg
    signed["kid"] = key_id
    if alg == ALG_HS256:
        signed[SIG_FIELD] = compute_hmac(signed, key_ring.secret(key_id))
    else:
        signed[SIG_FIELD] = digest_of(_signing_bytes(signed))
    return signed


def verify_message(message: dict, key_ring: KeyRing,
                   expected_kid: str | None = None) -> None:
    """校验签名；任何篡改、缺失、算法不符都抛 :class:`SignatureError`。"""
    alg = message.get("alg")
    kid = message.get("kid")
    sig = message.get(SIG_FIELD)
    if alg not in SUPPORTED_ALGS:
        raise SignatureError(f"消息缺少或带有非法 alg: {alg!r}")
    if not kid or not sig:
        raise SignatureError("消息缺少 kid 或 sig 字段")
    if expected_kid is not None and kid != expected_kid:
        raise SignatureError(f"签名主体不符: 期望 {expected_kid}，实际 {kid}")
    if not key_ring.has(kid):
        raise SignatureError(f"未知密钥标识: {kid}")
    if alg == ALG_HS256:
        expected = compute_hmac(message, key_ring.secret(kid))
    else:
        expected = digest_of(_signing_bytes(message))
    if not hmac.compare_digest(expected, sig):
        raise SignatureError("签名校验失败：内容可能被篡改")


def tamper(message: dict, **changes) -> dict:
    """测试辅助：篡改消息内容但保留旧签名。"""
    broken = copy.deepcopy(message)
    broken.update(changes)
    return broken
