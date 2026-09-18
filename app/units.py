"""UCUM 单位注册表与量纲换算。

每个单位登记：所属量纲、换算到该量纲基准单位的倍率与偏移量。
基准单位选择：质量 g、体积 L、时间 s、温度 K、压力 kPa、频率 Hz、无量纲 1。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DimensionMismatchError, UnknownUnitError


@dataclass(frozen=True)
class Unit:
    code: str  # UCUM 代码
    dimension: str  # 量纲名称
    factor: float  # canonical = value * factor + offset
    offset: float = 0.0


_UNITS: dict[str, Unit] = {}


def _register(code: str, dimension: str, factor: float, offset: float = 0.0) -> None:
    _UNITS[code] = Unit(code, dimension, factor, offset)


# 无量纲
_register("1", "dimensionless", 1.0)
_register("%", "dimensionless", 0.01)
_register("ppm", "dimensionless", 1e-6)
# 质量（基准 g）
_register("g", "mass", 1.0)
_register("kg", "mass", 1000.0)
_register("mg", "mass", 0.001)
_register("t", "mass", 1_000_000.0)
# 体积（基准 L）
_register("L", "volume", 1.0)
_register("mL", "volume", 0.001)
_register("m3", "volume", 1000.0)
# 时间（基准 s）
_register("s", "time", 1.0)
_register("min", "time", 60.0)
_register("h", "time", 3600.0)
# 温度（基准 K）
_register("K", "temperature", 1.0)
_register("Cel", "temperature", 1.0, 273.15)
_register("[degF]", "temperature", 5.0 / 9.0, 255.3722222222222)
# 压力（基准 kPa）
_register("kPa", "pressure", 1.0)
_register("Pa", "pressure", 0.001)
_register("MPa", "pressure", 1000.0)
_register("bar", "pressure", 100.0)
# 频率/转速（基准 Hz）
_register("Hz", "frequency", 1.0)
_register("rpm", "frequency", 1.0 / 60.0)


def get_unit(code: str) -> Unit:
    try:
        return _UNITS[code]
    except KeyError:
        raise UnknownUnitError(f"未注册的 UCUM 单位代码: {code!r}") from None


def dimension_of(code: str) -> str:
    return get_unit(code).dimension


def are_compatible(code_a: str, code_b: str) -> bool:
    """两个单位是否属于同一量纲（可互相换算）。"""
    return dimension_of(code_a) == dimension_of(code_b)


def convert(value: float, from_code: str, to_code: str) -> float:
    """把数值从 from_code 单位换算到 to_code 单位。"""
    src = get_unit(from_code)
    dst = get_unit(to_code)
    if src.dimension != dst.dimension:
        raise DimensionMismatchError(
            f"单位 {from_code!r}（{src.dimension}）与 {to_code!r}（{dst.dimension}）量纲不一致"
        )
    canonical = value * src.factor + src.offset
    return (canonical - dst.offset) / dst.factor
