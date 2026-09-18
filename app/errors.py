"""领域错误类型。

所有业务规则违反都抛出这里的异常，便于上游（界面/接口层）统一捕获。
"""


class DomainError(Exception):
    """领域错误基类。"""


# ---- 单位与参数校验 ----


class UnknownUnitError(DomainError):
    """使用了未注册的 UCUM 单位代码。"""


class DimensionMismatchError(DomainError):
    """录入单位的量纲与参数声明的量纲不一致。"""


class UnknownParameterError(DomainError):
    """配方中出现了参数目录未定义的参数。"""


class RangeViolationError(DomainError):
    """参数取值超出允许范围。"""


class DependencyViolationError(DomainError):
    """参数之间不满足目录中声明的依赖关系。"""


class DeviceCapabilityError(DomainError):
    """目标设备不具备执行该配方的能力（产品、参数或量程不符）。"""


# ---- 审核 ----


class ReviewStateError(DomainError):
    """审核状态机不允许的操作。"""


class SelfApprovalError(ReviewStateError):
    """审核人试图批准自己的变更，或会签人与前序审核人重复。"""


# ---- 签名 ----


class SignatureError(DomainError):
    """签名相关错误基类。"""


class UnknownSignerError(SignatureError):
    """签名者未在密钥库中登记。"""


class SignatureMismatchError(SignatureError):
    """签名或摘要校验失败（内容可能被篡改）。"""


# ---- 发布 ----


class ReleaseStateError(DomainError):
    """发布状态机不允许的操作。"""


class UnknownCommandError(ReleaseStateError):
    """收到不属于任何已下发指令的回执。"""


class ConflictingReceiptError(ReleaseStateError):
    """同一指令收到内容互相矛盾的回执。"""


# ---- 适用版本解析 ----


class ResolutionError(DomainError):
    """适用版本解析失败基类。"""


class NoApplicableVersionError(ResolutionError):
    """该设备/产品组合没有任何已生效版本。"""


class AuthorizationExpiredError(ResolutionError):
    """授权已过期或尚未生效。"""


class DeviceOutOfScopeError(ResolutionError):
    """设备不在发布的灰度范围内。"""


class AmbiguousVersionError(ResolutionError):
    """同一设备/产品解析出多个适用版本（不应发生，属防御性检查）。"""
