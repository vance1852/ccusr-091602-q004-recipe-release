"""审核工作流：草稿 → 技术审核 →（高风险时）质量会签 → 批准。

规则：
- 状态机取值与 domain_contract.json 的 review_states 一致；
- 审核人不能批准/驳回自己提交的变更（职责分离）；
- 配方含高风险参数时，技术审核通过后必须再由质量负责人会签，
  且会签人不得与技术审核人为同一人；
- 每一步决定都生成签名消息，载荷绑定配方内容摘要 —— 内容或决定被篡改
  都会在验签时暴露。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from .catalog import RISK_HIGH, ParameterCatalog
from .errors import ReviewStateError, SelfApprovalError
from .recipe import RecipeDraft, content_digest
from .signing import KeyStore, SignedMessage, sign, verify

ROLE_PROCESS_REVIEWER = "process_reviewer"
ROLE_QUALITY_LEAD = "quality_lead"
ROLE_AUTHOR = "process_engineer"


class ReviewState(str, Enum):
    DRAFT = "draft"
    TECHNICAL_REVIEW = "technical_review"
    QUALITY_REVIEW = "quality_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewDecision:
    """一次带签名的审核决定。"""

    def __init__(self, step: ReviewState, decision: str, message: SignedMessage) -> None:
        self.step = step
        self.decision = decision  # "approve" | "reject"
        self.message = message

    @property
    def signer(self) -> str:
        return self.message.signer


class ReviewRecord:
    """一个配方版本（recipe_id, version）的完整审核链。"""

    def __init__(self, draft: RecipeDraft, catalog: ParameterCatalog) -> None:
        self.draft = draft
        self.catalog = catalog
        self.state = ReviewState.DRAFT
        self.decisions: list[ReviewDecision] = []
        # 含高风险参数即要求质量会签
        self.requires_quality = any(
            catalog.get(key).risk == RISK_HIGH for key in draft.params
        )
        self.digest = content_digest(draft, catalog)

    # ---- 内部 ----

    def _ensure_not_author(self, signer: str) -> None:
        if signer == self.draft.author:
            raise SelfApprovalError(f"审核人 {signer!r} 不能批准/驳回自己提交的变更")

    def _record(self, keystore: KeyStore, step: ReviewState, decision: str,
                signer: str, role: str, now: datetime, note: str = "") -> None:
        payload = {
            "recipe_id": self.draft.recipe_id,
            "product_id": self.draft.product_id,
            "version": self.draft.version,
            "content_digest": self.digest,
            "step": step.value,
            "decision": decision,
            "note": note,
        }
        message = sign(keystore, signer, role, payload, now)
        self.decisions.append(ReviewDecision(step, decision, message))

    # ---- 状态机 ----

    def submit(self, by: str, now: datetime) -> None:
        if self.state != ReviewState.DRAFT:
            raise ReviewStateError(f"当前状态 {self.state.value} 不允许提交")
        if by != self.draft.author:
            raise ReviewStateError("只有起草人本人可以提交审核")
        self.state = ReviewState.TECHNICAL_REVIEW

    def approve(self, keystore: KeyStore, signer: str, role: str, now: datetime) -> None:
        self._ensure_not_author(signer)
        if self.state == ReviewState.TECHNICAL_REVIEW:
            if role != ROLE_PROCESS_REVIEWER:
                raise ReviewStateError("技术审核必须由工艺审核人执行")
            self._record(keystore, self.state, "approve", signer, role, now)
            self.state = ReviewState.QUALITY_REVIEW if self.requires_quality else ReviewState.APPROVED
        elif self.state == ReviewState.QUALITY_REVIEW:
            if role != ROLE_QUALITY_LEAD:
                raise ReviewStateError("质量会签必须由质量负责人执行")
            technical_signer = self.decisions[-1].signer
            if signer == technical_signer:
                raise SelfApprovalError("质量会签人必须不同于技术审核人")
            self._record(keystore, self.state, "approve", signer, role, now)
            self.state = ReviewState.APPROVED
        else:
            raise ReviewStateError(f"当前状态 {self.state.value} 不允许批准")

    def reject(self, keystore: KeyStore, signer: str, role: str, now: datetime, reason: str = "") -> None:
        self._ensure_not_author(signer)
        if self.state == ReviewState.TECHNICAL_REVIEW and role != ROLE_PROCESS_REVIEWER:
            raise ReviewStateError("技术审核阶段只能由工艺审核人驳回")
        if self.state == ReviewState.QUALITY_REVIEW and role != ROLE_QUALITY_LEAD:
            raise ReviewStateError("质量会签阶段只能由质量负责人驳回")
        if self.state not in (ReviewState.TECHNICAL_REVIEW, ReviewState.QUALITY_REVIEW):
            raise ReviewStateError(f"当前状态 {self.state.value} 不允许驳回")
        self._record(keystore, self.state, "reject", signer, role, now, note=reason)
        self.state = ReviewState.REJECTED

    # ---- 审计 ----

    def verify_chain(self, keystore: KeyStore) -> bool:
        """重放验签整条审核链（发布冻结前与出报告时都会调用）。"""
        return all(verify(keystore, d.message) for d in self.decisions)

    def chain_view(self, keystore: KeyStore) -> list[dict]:
        return [
            {
                "step": d.step.value,
                "decision": d.decision,
                "signer": d.message.signer,
                "role": d.message.role,
                "signed_at": d.message.signed_at,
                "algorithm": d.message.algorithm,
                "payload_digest": d.message.payload_digest,
                "signature_valid": verify(keystore, d.message),
            }
            for d in self.decisions
        ]
