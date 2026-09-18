"""不可变内容寻址存储与规范序列化。

配方内容以 JSON 规范形式序列化后计算 SHA-256 摘要；内容对象一经
存入不可修改。发布记录只持有内容摘要，天然支持"回滚=引用旧内容
创建新发布"。
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """规范序列化：键排序、无空白、UTF-8、不转义非 ASCII。"""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_of(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj)).hexdigest()


class ContentObject:
    """不可变内容包装。"""

    __slots__ = ("digest", "payload", "kind")

    def __init__(self, kind: str, payload: Any):
        self.kind = kind
        self.payload = copy.deepcopy(payload)
        self.digest = digest_of({"kind": kind, "payload": self.payload})

    def to_envelope(self) -> dict:
        return {"kind": self.kind, "digest": self.digest,
                "payload": copy.deepcopy(self.payload)}

    def __eq__(self, other) -> bool:
        return isinstance(other, ContentObject) and other.digest == self.digest

    def __hash__(self) -> int:
        return hash(self.digest)


class ContentStore:
    """按摘要存取不可变内容。"""

    def __init__(self):
        self._objects: dict[str, ContentObject] = {}

    def put(self, kind: str, payload: Any) -> ContentObject:
        obj = ContentObject(kind, payload)
        if obj.digest not in self._objects:
            self._objects[obj.digest] = obj
        return self._objects[obj.digest]

    def get(self, digest: str) -> ContentObject:
        if digest not in self._objects:
            raise KeyError(f"内容不存在或已被清除: {digest}")
        return self._objects[digest]

    def contains(self, digest: str) -> bool:
        return digest in self._objects

    def all_digests(self) -> list[str]:
        return sorted(self._objects)

    def export_snapshot(self) -> dict:
        return {"version": 1,
                "objects": [o.to_envelope()
                            for o in sorted(self._objects.values(),
                                            key=lambda x: x.digest)]}

    def load_snapshot(self, data: dict) -> None:
        for env in data.get("objects", []):
            obj = ContentObject(env["kind"], env["payload"])
            if obj.digest != env["digest"]:
                raise ValueError(f"快照内容摘要不一致: {env['digest']}")
            self._objects[obj.digest] = obj
