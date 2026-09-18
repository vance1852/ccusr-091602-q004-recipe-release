"""签名消息格式与验签。

外部消息（审核决定、设备回执）一律携带 ``SignedMessage``：
算法与摘要值作为原始字段保留，验签时重新计算并比对，任何字段被篡改都会失败。

签名串：``algorithm|signer|role|signed_at|payload_digest``，
签名值 = HMAC-SHA256(签名者密钥, 签名串) 的十六进制。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .errors import SignatureMismatchError, UnknownSignerError
from .recipe import canonical_json

ALGORITHM = "HMAC-SHA256"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class SignedMessage:
    algorithm: str
    signer: str
    role: str
    signed_at: str
    payload: dict
    payload_digest: str
    signature: str

    def to_dict(self) -> dict:
        return asdict(self)


class KeyStore:
    """签名者密钥库（演示用内存实现，生产应替换为 KMS/HSM）。"""

    def __init__(self) -> None:
        self._keys: dict[str, bytes] = {}

    def register(self, signer: str, key: bytes) -> None:
        self._keys[signer] = key

    def key_for(self, signer: str) -> bytes:
        try:
            return self._keys[signer]
        except KeyError:
            raise UnknownSignerError(f"签名者未登记密钥: {signer!r}") from None

    def has(self, signer: str) -> bool:
        return signer in self._keys


def payload_digest(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _signing_string(algorithm: str, signer: str, role: str, signed_at: str, digest: str) -> bytes:
    return "|".join([algorithm, signer, role, signed_at, digest]).encode("utf-8")


def sign(keystore: KeyStore, signer: str, role: str, payload: dict, now: datetime | None = None) -> SignedMessage:
    """对载荷签名；载荷摘要与算法随消息原样保存。"""
    key = keystore.key_for(signer)
    signed_at = iso(now or utcnow())
    digest = payload_digest(payload)
    sig = hmac.new(key, _signing_string(ALGORITHM, signer, role, signed_at, digest), hashlib.sha256).hexdigest()
    return SignedMessage(
        algorithm=ALGORITHM,
        signer=signer,
        role=role,
        signed_at=signed_at,
        payload=dict(payload),
        payload_digest=digest,
        signature=sig,
    )


def verify(keystore: KeyStore, message: SignedMessage) -> bool:
    """重算摘要与签名并比对；任何字段被篡改都返回 False。"""
    if message.algorithm != ALGORITHM:
        return False
    try:
        key = keystore.key_for(message.signer)
    except UnknownSignerError:
        return False
    if payload_digest(message.payload) != message.payload_digest:
        return False
    expected = hmac.new(
        key,
        _signing_string(message.algorithm, message.signer, message.role, message.signed_at, message.payload_digest),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, message.signature)


def verify_or_raise(keystore: KeyStore, message: SignedMessage) -> None:
    if not verify(keystore, message):
        raise SignatureMismatchError(
            f"签名校验失败: signer={message.signer!r} role={message.role!r}（内容可能被篡改）"
        )
