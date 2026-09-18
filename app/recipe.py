"""配方草稿与审核工作流。

状态机（见 domain_contract.json）::

    draft --submit--> technical_review --技术复核通过--> approved
                                  \\                      ^
                                   \\--含高风险参数--> quality_review --会签--> approved
                                    \\---reject---> rejected

规则：
* 提交时执行量纲、范围、参数依赖与跨参数关系校验，并冻结内容摘要；
* 审核人不能批准/会签自己创建的变更；
* 任一参数标记为高风险时，必须经质量负责人会签。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .catalog import Catalog
from .content import ContentStore
from .crypto import KeyRing, sign_message, verify_message
from .errors import (ReviewError, ValidationError, WorkflowError)
from .identity import Directory, ROLE_QUALITY, ROLE_REVIEWER

STATE_DRAFT = "draft"
STATE_TECH = "technical_review"
STATE_QUALITY = "quality_review"
STATE_APPROVED = "approved"
STATE_REJECTED = "rejected"


def user_key_id(user_id: str) -> str:
    return f"key:{user_id}"


@dataclass
class RecipeChange:
    change_id: str
    recipe_code: str
    product_code: str
    parameters: dict
    created_by: str
    created_at: float
    state: str = STATE_DRAFT
    content_digest: str | None = None
    submitted_at: float | None = None
    signatures: list[dict] = field(default_factory=list)
    reject_reason: str | None = None
    history: list[dict] = field(default_factory=list)

    def high_risk_keys(self, catalog: Catalog) -> list[str]:
        return sorted(k for k, item in self.parameters.items()
                      if k in catalog.params and catalog.params[k].high_risk)

    def _log(self, action: str, user_id: str, at: float, **extra) -> None:
        entry = {"action": action, "by": user_id, "at": at}
        entry.update(extra)
        self.history.append(entry)


class RecipeWorkflow:
    def __init__(self, catalog: Catalog, store: ContentStore,
                 directory: Directory, key_ring: KeyRing,
                 clock: Callable[[], float] = time.time):
        self.catalog = catalog
        self.store = store
        self.directory = directory
        self.key_ring = key_ring
        self.clock = clock
        self.changes: dict[str, RecipeChange] = {}

    # ------------------------------------------------------------- 编辑
    def create_draft(self, user_id: str, recipe_code: str,
                     product_code: str, parameters: dict) -> RecipeChange:
        user = self.directory.get(user_id)
        change = RecipeChange(
            change_id=f"CHG-{uuid.uuid4().hex[:10].upper()}",
            recipe_code=recipe_code, product_code=product_code,
            parameters={k: dict(v) for k, v in parameters.items()},
            created_by=user.user_id, created_at=self.clock(),
        )
        change._log("create_draft", user_id, change.created_at)
        self.changes[change.change_id] = change
        return change

    def update_draft(self, change_id: str, user_id: str,
                     parameters: dict) -> RecipeChange:
        change = self._get(change_id)
        self.directory.get(user_id)
        if change.state != STATE_DRAFT:
            raise WorkflowError(
                f"变更 {change_id} 当前状态为 {change.state}，不可再编辑")
        change.parameters = {k: dict(v) for k, v in parameters.items()}
        change._log("update_draft", user_id, self.clock())
        return change

    # ------------------------------------------------------------- 提交
    def submit(self, change_id: str, user_id: str) -> RecipeChange:
        change = self._get(change_id)
        if change.state != STATE_DRAFT:
            raise WorkflowError(f"仅 draft 状态可提交，当前 {change.state}")
        if change.created_by != user_id:
            raise ReviewError("只有变更创建人可以提交审核")

        issues = self.catalog.validate_recipe(change.parameters)
        if issues:
            raise ValidationError(issues)

        obj = self.store.put("recipe_content", {
            "recipe_code": change.recipe_code,
            "product_code": change.product_code,
            "parameters": change.parameters,
        })
        change.content_digest = obj.digest
        change.submitted_at = self.clock()
        change.state = STATE_TECH
        change._log("submit", user_id, change.submitted_at,
                    content_digest=obj.digest)
        return change

    # ------------------------------------------------------------- 审核
    def technical_review(self, change_id: str, reviewer_id: str,
                         approved: bool, comment: str = "") -> RecipeChange:
        change = self._get(change_id)
        user = self.directory.get(reviewer_id)
        if change.state != STATE_TECH:
            raise WorkflowError(
                f"变更 {change_id} 不在待技术复核状态（当前 {change.state}）")
        if not user.has(ROLE_REVIEWER):
            raise ReviewError(f"{user.name} 不具备审核人角色")
        if reviewer_id == change.created_by:
            raise ReviewError("审核人不能批准自己提交的变更")

        at = self.clock()
        if not approved:
            self._sign_and_attach(change, "technical_review", reviewer_id,
                                  "rejected", comment, at)
            change.state = STATE_REJECTED
            change.reject_reason = comment or "技术复核驳回"
            change._log("reject", reviewer_id, at, reason=change.reject_reason)
            return change

        self._sign_and_attach(change, "technical_review", reviewer_id,
                              "approved", comment, at)
        high = change.high_risk_keys(self.catalog)
        if high:
            change.state = STATE_QUALITY
            change._log("technical_approved_pending_quality", reviewer_id, at,
                        high_risk=high)
        else:
            change.state = STATE_APPROVED
            change._log("approved", reviewer_id, at)
        return change

    def quality_countersign(self, change_id: str, quality_id: str,
                            approved: bool, comment: str = "") -> RecipeChange:
        change = self._get(change_id)
        user = self.directory.get(quality_id)
        if change.state != STATE_QUALITY:
            raise WorkflowError(
                f"变更 {change_id} 不在待会签状态（当前 {change.state}）")
        if not user.has(ROLE_QUALITY):
            raise ReviewError(f"{user.name} 不具备质量负责人角色")
        if quality_id == change.created_by:
            raise ReviewError("质量负责人不能会签自己提交的变更")

        at = self.clock()
        if not approved:
            self._sign_and_attach(change, "quality_countersign", quality_id,
                                  "rejected", comment, at)
            change.state = STATE_REJECTED
            change.reject_reason = comment or "质量会签驳回"
            change._log("reject", quality_id, at, reason=change.reject_reason)
            return change

        self._sign_and_attach(change, "quality_countersign", quality_id,
                              "approved", comment, at)
        change.state = STATE_APPROVED
        change._log("approved", quality_id, at)
        return change

    # ------------------------------------------------------------- 查询
    def _get(self, change_id: str) -> RecipeChange:
        if change_id not in self.changes:
            raise KeyError(f"未知变更: {change_id}")
        return self.changes[change_id]

    def get(self, change_id: str) -> RecipeChange:
        return self._get(change_id)

    def verify_chain(self, change_id: str) -> list[dict]:
        """重放并验证审核链上所有签名；返回逐条验签结果。"""
        change = self._get(change_id)
        results = []
        for msg in change.signatures:
            try:
                verify_message(msg, self.key_ring)
                # 内容摘要必须与变更冻结摘要一致
                if msg.get("content_digest") != change.content_digest:
                    raise ReviewError("审核消息引用的内容摘要与变更不一致")
                ok, err = True, None
            except Exception as exc:  # noqa: BLE001 - 收集为结果
                ok, err = False, str(exc)
            results.append({"type": msg.get("type"),
                            "by": msg.get("reviewer_id"),
                            "decision": msg.get("decision"),
                            "valid": ok, "error": err})
        return results

    # ------------------------------------------------------------- 内部
    def _sign_and_attach(self, change: RecipeChange, msg_type: str,
                         reviewer_id: str, decision: str,
                         comment: str, at: float) -> dict:
        message = {
            "type": msg_type,
            "change_id": change.change_id,
            "recipe_code": change.recipe_code,
            "product_code": change.product_code,
            "content_digest": change.content_digest,
            "reviewer_id": reviewer_id,
            "decision": decision,
            "comment": comment,
            "ts": at,
        }
        signed = sign_message(message, self.key_ring,
                              user_key_id(reviewer_id))
        verify_message(signed, self.key_ring)
        change.signatures.append(signed)
        change._log(f"{msg_type}_{decision}", reviewer_id, at)
        return signed
