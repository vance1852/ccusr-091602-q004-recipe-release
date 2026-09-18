"""配方草稿：工程师编辑的参数集合及其校验、规范化与内容摘要。

校验分三层（编辑期即时反馈）：
1. 量纲校验 —— 录入单位必须与参数声明单位同量纲；
2. 范围校验 —— 换算到声明单位后落在 [min, max] 内；
3. 依赖校验 —— 目录中声明的参数间约束全部成立。

规范化内容（canonical content）把每个参数统一换算到声明单位，
因此同一物理配方无论用哪种等价单位录入，内容摘要都相同。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from .catalog import ParameterCatalog
from .errors import (
    DependencyViolationError,
    RangeViolationError,
)
from .units import convert, dimension_of, get_unit

DIGEST_ALGORITHM = "SHA-256"


def canonical_json(obj) -> str:
    """确定性 JSON 序列化（排序键、紧凑分隔符），用于摘要与签名。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ParameterValue:
    value: float
    unit: str  # 工程师录入时使用的 UCUM 单位


@dataclass
class RecipeDraft:
    recipe_id: str
    product_id: str
    version: int
    author: str
    params: dict[str, ParameterValue] = field(default_factory=dict)
    created_at: datetime | None = None


def declared_values(draft: RecipeDraft, catalog: ParameterCatalog) -> dict[str, float]:
    """把草稿参数统一换算到目录声明单位，同时完成量纲校验。"""
    declared: dict[str, float] = {}
    for key, pv in draft.params.items():
        pdef = catalog.get(key)  # 未定义参数在此抛 UnknownParameterError
        # get_unit 校验单位存在；量纲不一致时 convert 抛 DimensionMismatchError
        if dimension_of(pv.unit) != get_unit(pdef.unit).dimension:
            # 走 convert 抛出带详细信息的 DimensionMismatchError
            convert(pv.value, pv.unit, pdef.unit)
        declared[key] = round(convert(pv.value, pv.unit, pdef.unit), 9)
    return declared


def validate_draft(draft: RecipeDraft, catalog: ParameterCatalog) -> None:
    """执行量纲、范围、依赖三层校验；任何一层失败都抛领域错误。"""
    values = declared_values(draft, catalog)  # 量纲 + 未定义参数

    for key, value in values.items():
        pdef = catalog.get(key)
        if pdef.min_value is not None and value < pdef.min_value:
            raise RangeViolationError(
                f"参数 {key} = {value} {pdef.unit} 低于下限 {pdef.min_value}"
            )
        if pdef.max_value is not None and value > pdef.max_value:
            raise RangeViolationError(
                f"参数 {key} = {value} {pdef.unit} 高于上限 {pdef.max_value}"
            )

    for rule in catalog.dependencies:
        if any(p not in values for p in rule.params):
            continue  # 依赖引用的参数未全部出现时跳过
        namespace = {p.replace(".", "__"): values[p] for p in rule.params}
        expr = rule.expression
        for p in sorted(rule.params, key=len, reverse=True):
            expr = expr.replace(p, p.replace(".", "__"))
        if not eval(expr, {"__builtins__": {}}, namespace):  # noqa: S307 - 目录为受信来源
            raise DependencyViolationError(
                f"违反参数依赖 {rule.key}: {rule.description or rule.expression}"
            )


def canonical_content(draft: RecipeDraft, catalog: ParameterCatalog) -> dict:
    """规范化内容：参数统一为声明单位取值，是摘要与下发的唯一依据。"""
    values = declared_values(draft, catalog)
    return {
        "recipe_id": draft.recipe_id,
        "product_id": draft.product_id,
        "version": draft.version,
        "params": {
            key: {"unit": catalog.get(key).unit, "value": values[key]}
            for key in sorted(values)
        },
    }


def content_digest(draft: RecipeDraft, catalog: ParameterCatalog) -> str:
    """内容摘要：冻结时计算并随发布记录保存，设备端据此核对。"""
    return hashlib.sha256(canonical_json(canonical_content(draft, catalog)).encode("utf-8")).hexdigest()
