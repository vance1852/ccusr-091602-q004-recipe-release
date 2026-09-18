"""参数目录：声明每个工艺参数的单位、允许范围、风险等级与参数间依赖。

- 单位用 UCUM 代码声明，范围以声明单位表示；
- 依赖规则用表达式书写（如 ``cool.temperature < mix.temperature``），
  在配方的声明单位取值上求值；表达式来自受信任的目录而非外部输入。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import UnknownParameterError

RISK_STANDARD = "standard"
RISK_HIGH = "high_risk"


@dataclass(frozen=True)
class ParameterDef:
    key: str
    unit: str  # 声明单位（UCUM）
    min_value: float | None = None
    max_value: float | None = None
    risk: str = RISK_STANDARD
    description: str = ""


@dataclass(frozen=True)
class DependencyRule:
    key: str
    expression: str  # 引用参数键（含点号），求值时替换为安全变量名
    params: tuple[str, ...]  # 表达式引用的参数键
    description: str = ""


@dataclass
class ParameterCatalog:
    parameters: dict[str, ParameterDef] = field(default_factory=dict)
    dependencies: list[DependencyRule] = field(default_factory=list)

    def get(self, key: str) -> ParameterDef:
        try:
            return self.parameters[key]
        except KeyError:
            raise UnknownParameterError(f"参数目录未定义参数: {key!r}") from None

    def high_risk_keys(self, param_keys) -> list[str]:
        """在给定参数键中筛出高风险参数。"""
        return [k for k in param_keys if self.get(k).risk == RISK_HIGH]


def default_catalog() -> ParameterCatalog:
    """示例工厂内置的混料工艺参数目录。"""
    params = [
        ParameterDef("mix.temperature", "Cel", 20.0, 250.0, RISK_HIGH, "混合温度"),
        ParameterDef("mix.duration", "min", 1.0, 240.0, RISK_STANDARD, "混合时长"),
        ParameterDef("mix.pressure", "bar", 0.5, 12.0, RISK_HIGH, "混合压力"),
        ParameterDef("dose.mass", "kg", 0.1, 500.0, RISK_STANDARD, "投料质量"),
        ParameterDef("stir.speed", "rpm", 10.0, 1500.0, RISK_STANDARD, "搅拌转速"),
        ParameterDef("cool.temperature", "Cel", 0.0, 80.0, RISK_STANDARD, "冷却温度"),
        ParameterDef("cool.duration", "min", 1.0, 480.0, RISK_STANDARD, "冷却时长"),
        ParameterDef("fill.ratio", "%", 10.0, 100.0, RISK_STANDARD, "填充率"),
    ]
    deps = [
        DependencyRule(
            "cool-below-mix",
            "cool.temperature < mix.temperature",
            ("cool.temperature", "mix.temperature"),
            "冷却温度必须低于混合温度",
        ),
        DependencyRule(
            "total-cycle",
            "mix.duration + cool.duration <= 600",
            ("mix.duration", "cool.duration"),
            "混合与冷却总周期不得超过 600 min",
        ),
        DependencyRule(
            "heavy-dose-needs-stir",
            "dose.mass <= 100 or stir.speed >= 100",
            ("dose.mass", "stir.speed"),
            "投料超过 100 kg 时搅拌转速不得低于 100 rpm",
        ),
    ]
    return ParameterCatalog(
        parameters={p.key: p for p in params},
        dependencies=deps,
    )
