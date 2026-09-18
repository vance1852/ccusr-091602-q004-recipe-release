"""参数目录与设备能力注册表。

目录定义每个配方参数的量纲、全局允许范围、风险等级与参数间依赖；
设备能力表列出每种设备型号支持的参数、原生单位（控制器实际接收
的单位）与设备自身的物理上下限。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import units
from .errors import ValidationError
from .units import Dimension


@dataclass(frozen=True)
class ParamSpec:
    key: str
    name: str
    kind: str                     # "numeric" | "enum"
    dimension: Dimension | None = None
    range_unit: str | None = None  # 全局范围的计量单位
    min_value: float | None = None
    max_value: float | None = None
    choices: tuple[str, ...] = ()
    high_risk: bool = False
    mandatory: bool = False
    requires: tuple[str, ...] = ()  # 本参数出现时必须同时存在的参数
    description: str = ""


@dataclass(frozen=True)
class DependencyRule:
    """当 ``when_param`` 取值属于 ``when_in`` 时，``require`` 必须存在。"""

    when_param: str
    when_in: tuple[str, ...]
    require: str
    description: str = ""


@dataclass(frozen=True)
class RelationRule:
    """跨参数关系约束: left op right（转换到相同基准量纲后比较）。"""

    left: str
    op: str            # "<" | "<="
    right: str
    description: str = ""


@dataclass(frozen=True)
class Catalog:
    params: dict[str, ParamSpec]
    dependencies: tuple[DependencyRule, ...] = ()
    relations: tuple[RelationRule, ...] = ()

    def spec(self, key: str) -> ParamSpec:
        if key not in self.params:
            raise ValidationError([f"未知参数: {key}"])
        return self.params[key]

    # --------------------------------------------------------------- 校验
    def validate_recipe(self, parameters: dict) -> list[str]:
        """返回全部问题的列表；空列表表示通过。不抛异常。"""
        issues: list[str] = []

        for key, item in parameters.items():
            spec = self.params.get(key)
            if spec is None:
                issues.append(f"未知参数: {key}")
                continue
            value, unit = item["value"], item.get("unit")
            if spec.kind == "enum":
                if value not in spec.choices:
                    issues.append(
                        f"参数 {key}={value!r} 不在允许取值 {list(spec.choices)} 内"
                    )
            else:
                issues.extend(self._check_numeric(spec, value, unit))

        for spec in self.params.values():
            if spec.mandatory and spec.key not in parameters:
                issues.append(f"缺少必填参数: {spec.key}")
            if spec.key in parameters:
                for req in spec.requires:
                    if req not in parameters:
                        issues.append(f"参数 {spec.key} 依赖的 {req} 缺失")

        for rule in self.dependencies:
            if rule.when_param in parameters:
                v = parameters[rule.when_param]["value"]
                if v in rule.when_in and rule.require not in parameters:
                    issues.append(
                        f"参数 {rule.when_param}={v!r} 时必须提供 {rule.require}"
                        + (f"（{rule.description}）" if rule.description else "")
                    )

        for rule in self.relations:
            if rule.left in parameters and rule.right in parameters:
                a = self._base_value(rule.left, parameters[rule.left])
                b = self._base_value(rule.right, parameters[rule.right])
                if a is None or b is None:
                    continue
                ok = a < b if rule.op == "<" else a <= b
                if not ok:
                    issues.append(
                        f"参数关系不满足: {rule.left} {rule.op} {rule.right}"
                        + (f"（{rule.description}）" if rule.description else "")
                    )
        return issues

    def _check_numeric(self, spec: ParamSpec, value, unit) -> list[str]:
        issues: list[str] = []
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"参数 {spec.key} 的值必须是数值，得到 {value!r}"]
        try:
            if spec.dimension is not None:
                if not units.compatible(unit, spec.range_unit):
                    return [
                        f"参数 {spec.key} 量纲错误: 收到 {unit or '1'}，"
                        f"应为 [{spec.dimension}]（如 {spec.range_unit}）"
                    ]
                base = units.convert(value, unit, spec.range_unit)
            else:
                base = float(value)
        except units.UnitError as exc:
            return [f"参数 {spec.key}: {exc}"]
        if spec.min_value is not None and base < spec.min_value - 1e-9:
            issues.append(
                f"参数 {spec.key}={value}{unit or ''} 低于下限 "
                f"{spec.min_value}{spec.range_unit or ''}"
            )
        if spec.max_value is not None and base > spec.max_value + 1e-9:
            issues.append(
                f"参数 {spec.key}={value}{unit or ''} 高于上限 "
                f"{spec.max_value}{spec.range_unit or ''}"
            )
        return issues

    def _base_value(self, key: str, item: dict) -> float | None:
        spec = self.params[key]
        try:
            return units.convert(item["value"], item.get("unit"), spec.range_unit)
        except (units.UnitError, TypeError, KeyError):
            return None


@dataclass(frozen=True)
class DeviceParam:
    key: str
    native_unit: str | None       # 控制器消息中使用的单位
    min_value: float              # 以 native_unit 计
    max_value: float


@dataclass(frozen=True)
class DeviceModel:
    model: str
    name: str
    group: str                    # 设备组：同组设备在同一波下发
    capabilities: dict[str, DeviceParam] = field(default_factory=dict)

    def check_recipe(self, catalog: Catalog, parameters: dict) -> list[str]:
        """检查该设备将收到的参数子集是否落在能力范围内。

        配方可覆盖多个工段，设备只接收自身能力内的参数，能力外参数
        不会出现在下发指令中，因此不视为错误。
        """
        issues: list[str] = []
        for key, item in parameters.items():
            cap = self.capabilities.get(key)
            if cap is None:
                continue
            spec = catalog.params[key]
            if spec.kind == "enum":
                continue
            try:
                v = units.convert(item["value"], item.get("unit"), cap.native_unit)
            except units.UnitError as exc:
                issues.append(f"参数 {key}: {exc}")
                continue
            if v < cap.min_value - 1e-9 or v > cap.max_value + 1e-9:
                issues.append(
                    f"参数 {key} 下发值 {v:.4g}{cap.native_unit or ''} 超出设备 "
                    f"{self.model} 能力 [{cap.min_value}, {cap.max_value}]"
                    f"{cap.native_unit or ''}"
                )
        return issues


@dataclass(frozen=True)
class Device:
    device_id: str
    model: str
    line: str
    name: str = ""


# --------------------------------------------------------------------- 注册表
class Registry:
    def __init__(self, catalog: Catalog, models: dict[str, DeviceModel],
                 devices: dict[str, Device]):
        self.catalog = catalog
        self.models = models
        self.devices = devices

    def device(self, device_id: str) -> Device:
        if device_id not in self.devices:
            raise ValidationError([f"未知设备: {device_id}"])
        return self.devices[device_id]

    def model_of(self, device_id: str) -> DeviceModel:
        return self.models[self.device(device_id).model]

    def group_of(self, device_id: str) -> str:
        return self.model_of(device_id).group

    def groups(self) -> list[str]:
        seen: list[str] = []
        for m in self.models.values():
            if m.group not in seen:
                seen.append(m.group)
        return seen

    def devices_in_group(self, group: str) -> list[Device]:
        return [d for d in self.devices.values()
                if self.models[d.model].group == group]

    def check_targets(self, device_ids, parameters: dict) -> dict[str, list[str]]:
        return {d: self.model_of(d).check_recipe(self.catalog, parameters)
                for d in device_ids}


def default_catalog() -> Catalog:
    """仓库列出的标准参数目录（示例工艺：涂布/烘干线）。"""
    T = units._T
    P = units._PA_DIM
    t = units._t
    RPM_DIM = units._RPS_DIM
    return Catalog(
        params={
            "coating_speed": ParamSpec(
                "coating_speed", "涂布速度", "numeric",
                dimension=units._L / t, range_unit="mm/min",
                min_value=500.0, max_value=30000.0,
                description="基材走带速度",
            ),
            "coating_gap": ParamSpec(
                "coating_gap", "涂布间隙", "numeric",
                dimension=units._L, range_unit="um",
                min_value=20.0, max_value=500.0,
            ),
            "mixing_speed": ParamSpec(
                "mixing_speed", "搅拌转速", "numeric",
                dimension=RPM_DIM, range_unit="rpm",
                min_value=50, max_value=2000,
            ),
            "mixing_time": ParamSpec(
                "mixing_time", "搅拌时间", "numeric",
                dimension=t, range_unit="min",
                min_value=1, max_value=240,
            ),
            "drying_temp": ParamSpec(
                "drying_temp", "烘干温度", "numeric",
                dimension=T, range_unit="degC",
                min_value=20.0, max_value=180.0,
            ),
            "curing_temp": ParamSpec(
                "curing_temp", "固化温度", "numeric",
                dimension=T, range_unit="degC",
                min_value=20.0, max_value=220.0, high_risk=True,
                requires=("curing_time",),
                description="高温固化段，超温可能导致整批材料报废",
            ),
            "curing_time": ParamSpec(
                "curing_time", "固化时间", "numeric",
                dimension=t, range_unit="min",
                min_value=0.5, max_value=120.0,
            ),
            "vacuum_pressure": ParamSpec(
                "vacuum_pressure", "真空压力", "numeric",
                dimension=P, range_unit="kPa",
                min_value=-101.0, max_value=0.0,
                description="表压，负值表示真空",
            ),
            "slurry_flow": ParamSpec(
                "slurry_flow", "浆料流量", "numeric",
                dimension=units._L ** 3 / t, range_unit="L/min",
                min_value=0.1, max_value=50.0,
            ),
            "solvent_ratio": ParamSpec(
                "solvent_ratio", "溶剂质量分数", "numeric",
                dimension=Dimension.dimensionless(), range_unit="%",
                min_value=0.0, max_value=15.0, high_risk=True,
            ),
            "mix_mode": ParamSpec(
                "mix_mode", "搅拌模式", "enum",
                choices=("normal", "vacuum"), mandatory=True,
            ),
            "vacuum_enabled": ParamSpec(
                "vacuum_enabled", "启用真空脱泡", "enum",
                choices=("on", "off"),
            ),
        },
        dependencies=(
            DependencyRule("mix_mode", ("vacuum",), "vacuum_pressure",
                           "真空搅拌必须设定真空压力"),
            DependencyRule("vacuum_enabled", ("on",), "vacuum_pressure",
                           "真空脱泡开启时必须设定真空压力"),
        ),
        relations=(
            RelationRule("drying_temp", "<", "curing_temp",
                         "烘干温度必须低于固化温度"),
        ),
    )


def default_registry() -> Registry:
    """仓库登记的设备型号与现场设备清单。"""
    cat = default_catalog()

    def cap(key, native_unit, lo, hi) -> DeviceParam:
        spec = cat.params[key]
        # 全局范围先换算到设备原生单位做交叉校验
        lo_b = units.convert(lo, native_unit, spec.range_unit)
        hi_b = units.convert(hi, native_unit, spec.range_unit)
        assert lo_b >= (spec.min_value or lo_b) - 1e-6, key
        assert hi_b <= (spec.max_value or hi_b) + 1e-6, key
        return DeviceParam(key, native_unit, lo, hi)

    models = {
        "COATER-A": DeviceModel(
            "COATER-A", "涂布机A型（试点组）", "canary",
            capabilities={
                "coating_speed": cap("coating_speed", "mm/min", 500, 25000),
                "coating_gap": cap("coating_gap", "um", 20, 400),
                "slurry_flow": cap("slurry_flow", "L/min", 0.2, 40.0),
                "drying_temp": cap("drying_temp", "degC", 20, 160),
                "curing_temp": cap("curing_temp", "degC", 20, 200),
                "curing_time": cap("curing_time", "min", 1, 90),
                "vacuum_pressure": cap("vacuum_pressure", "kPa", -100, 0),
                "mix_mode": DeviceParam("mix_mode", None, 0, 0),
                "vacuum_enabled": DeviceParam("vacuum_enabled", None, 0, 0),
                "solvent_ratio": cap("solvent_ratio", "%", 0, 12),
                "mixing_speed": cap("mixing_speed", "rpm", 100, 1500),
                "mixing_time": cap("mixing_time", "min", 1, 180),
            },
        ),
        "COATER-B": DeviceModel(
            "COATER-B", "涂布机B型（推广组）", "broad",
            capabilities={
                "coating_speed": cap("coating_speed", "mm/min", 800, 20000),
                "coating_gap": cap("coating_gap", "um", 30, 500),
                "slurry_flow": cap("slurry_flow", "L/min", 0.5, 30.0),
                "drying_temp": cap("drying_temp", "degC", 20, 170),
                "curing_temp": cap("curing_temp", "degC", 20, 210),
                "curing_time": cap("curing_time", "min", 1, 100),
                "vacuum_pressure": cap("vacuum_pressure", "kPa", -101, 0),
                "mix_mode": DeviceParam("mix_mode", None, 0, 0),
                "vacuum_enabled": DeviceParam("vacuum_enabled", None, 0, 0),
                "solvent_ratio": cap("solvent_ratio", "%", 0, 15),
                "mixing_speed": cap("mixing_speed", "rpm", 80, 1800),
                "mixing_time": cap("mixing_time", "min", 1, 200),
            },
        ),
        "MIXER-X": DeviceModel(
            "MIXER-X", "搅拌釜X型（配料组）", "prep",
            capabilities={
                "mixing_speed": cap("mixing_speed", "rpm", 50, 2000),
                "mixing_time": cap("mixing_time", "min", 1, 240),
                "mix_mode": DeviceParam("mix_mode", None, 0, 0),
                "vacuum_enabled": DeviceParam("vacuum_enabled", None, 0, 0),
                "vacuum_pressure": cap("vacuum_pressure", "bar", -1.0, 0.0),
                "solvent_ratio": cap("solvent_ratio", "%", 0, 15),
                "slurry_flow": cap("slurry_flow", "L/min", 0.1, 50.0),
            },
        ),
    }

    devices = {
        "DEV-CN-01": Device("DEV-CN-01", "COATER-A", "L1", "1号线试点涂布机"),
        "DEV-CN-02": Device("DEV-CN-02", "COATER-A", "L1", "1号线试点涂布机2"),
        "DEV-CB-01": Device("DEV-CB-01", "COATER-B", "L2", "2号线涂布机"),
        "DEV-CB-02": Device("DEV-CB-02", "COATER-B", "L3", "3号线涂布机"),
        "DEV-MX-01": Device("DEV-MX-01", "MIXER-X", "L0", "中央搅拌釜"),
    }
    return Registry(cat, models, devices)
