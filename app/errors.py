"""领域错误类型。"""


class DomainError(Exception):
    """所有领域规则违反的基类。"""

    code = "domain_error"


class ValidationError(DomainError):
    """配方内容未通过量纲、范围或依赖校验。"""

    code = "validation_error"

    def __init__(self, issues, message="配方校验未通过"):
        self.issues = list(issues)
        super().__init__(f"{message}: {'; '.join(self.issues)}")


class WorkflowError(DomainError):
    """状态机不允许当前操作。"""

    code = "workflow_error"


class ReviewError(DomainError):
    """审核规则违反，例如自审或越权。"""

    code = "review_error"


class SignatureError(DomainError):
    """消息签名验证失败。"""

    code = "signature_error"


class DigestMismatchError(DomainError):
    """冻结摘要与实际内容或回执不一致。"""

    code = "digest_mismatch"


class DuplicateReceiptError(DomainError):
    """同一指令的回执被重复提交。"""

    code = "duplicate_receipt"


class AuthorizationDenied(DomainError):
    """开批授权未通过（缺失、过期、撤销或灰度外）。"""

    code = "authorization_denied"

    def __init__(self, reason, message=None):
        self.reason = reason
        super().__init__(message or f"开批被拒绝: {reason}")


class NotFoundError(DomainError):
    code = "not_found"
