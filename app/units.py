"""UCUM 单位子集与量纲分析。

只实现配方场景需要的单位：温度、压力、时间、转速、速度、流量、
长度、质量、体积、浓度及若干无因次量。单位串使用 UCUM 代码
（如 ``degC``、``MPa``、``min``、``rpm``、``kg/h``）。

每个单位记录：量纲向量（7 个 SI 基本量纲）、线性换算到基准单位的
系数与偏置（摄氏温度需要偏置）。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

# 量纲顺序: (热力学温度 K, 质量 kg, 时间 s, 物质的量 mol, 长度 m, 电流 A, 发光强度 cd)
_DIM_NAMES = ("T", "M", "t", "N", "L", "I", "J")


@dataclass(frozen=True)
class Dimension:
    """SI 基本量纲的整数幂向量。"""

    vec: tuple[int, ...]

    @classmethod
    def dimensionless(cls) -> "Dimension":
        return cls((0, 0, 0, 0, 0, 0, 0))

    def __mul__(self, other: "Dimension") -> "Dimension":
        return Dimension(tuple(a + b for a, b in zip(self.vec, other.vec)))

    def __truediv__(self, other: "Dimension") -> "Dimension":
        return Dimension(tuple(a - b for a, b in zip(self.vec, other.vec)))

    def __pow__(self, p: int) -> "Dimension":
        return Dimension(tuple(a * p for a in self.vec))

    @property
    def is_dimensionless(self) -> bool:
        return all(v == 0 for v in self.vec)

    def __str__(self) -> str:
        parts = [f"{n}^{p}" for n, p in zip(_DIM_NAMES, self.vec) if p]
        return "·".join(parts) if parts else "1"


# 基本量纲
_T = Dimension((1, 0, 0, 0, 0, 0, 0))  # 热力学温度
_M = Dimension((0, 1, 0, 0, 0, 0, 0))  # 质量
_t = Dimension((0, 0, 1, 0, 0, 0, 0))  # 时间
_N = Dimension((0, 0, 0, 1, 0, 0, 0))  # 物质的量
_L = Dimension((0, 0, 0, 0, 1, 0, 0))  # 长度


@dataclass(frozen=True)
class Unit:
    code: str
    dimension: Dimension
    factor: float          # 乘性换算到基准单位
    offset: float = 0.0    # 加性偏置（基准单位），仅温度用
    aliases: tuple[str, ...] = ()

    def to_base(self, value: float) -> float:
        return value * self.factor + self.offset

    def from_base(self, value: float) -> float:
        return (value - self.offset) / self.factor


# 基准单位：K, kg, s, mol, m
_UNITS: dict[str, Unit] = {}


def _reg(u: Unit) -> Unit:
    _UNITS[u.code] = u
    for a in u.aliases:
        _UNITS[a] = u
    return u


# --- 温度 ---
_K = _reg(Unit("K", _T, 1.0))
_DEGC = _reg(Unit("degC", _T, 1.0, offset=273.15, aliases=("Cel", "℃")))
_DEGF = _reg(Unit("degF", _T, 5.0 / 9.0, offset=459.67 * 5.0 / 9.0))

# --- 质量 ---
_KG = _reg(Unit("kg", _M, 1.0))
_G = _reg(Unit("g", _M, 1e-3))
_MG = _reg(Unit("mg", _M, 1e-6))

# --- 时间 ---
_S = _reg(Unit("s", _t, 1.0, aliases=("sec",)))
_MIN = _reg(Unit("min", _t, 60.0))
_H = _reg(Unit("h", _t, 3600.0))

# --- 物质的量 ---
_MOL = _reg(Unit("mol", _N, 1.0))
_MMOL = _reg(Unit("mmol", _N, 1e-3))

# --- 长度 ---
_METER = _reg(Unit("m", _L, 1.0))
_MM = _reg(Unit("mm", _L, 1e-3))
_CM = _reg(Unit("cm", _L, 1e-2))
_UM = _reg(Unit("um", _L, 1e-6, aliases=("µm",)))

# --- 压力: 量纲 M·L^-1·t^-2，基准 Pa(kg·m^-1·s^-2) ---
_PA_DIM = _M / (_L * _t ** 2)
_PA = _reg(Unit("Pa", _PA_DIM, 1.0))
_KPA = _reg(Unit("kPa", _PA_DIM, 1e3))
_MPA = _reg(Unit("MPa", _PA_DIM, 1e6))
_BAR = _reg(Unit("bar", _PA_DIM, 1e5))
_PSI = _reg(Unit("psi", _PA_DIM, 6894.757293168))

# --- 体积: L^3，基准 m^3 ---
_M3 = _reg(Unit("m3", _L ** 3, 1.0, aliases=("m³",)))
_LITRE = _reg(Unit("L", _L ** 3, 1e-3, aliases=("l",)))
_MLITRE = _reg(Unit("mL", _L ** 3, 1e-6, aliases=("ml",)))

# --- 速度: L/t，基准 m/s ---
_MPS = _reg(Unit("m/s", _L / _t, 1.0))
_MMPMIN = _reg(Unit("mm/min", _L / _t, 1e-3 / 60.0))

# --- 转速: 无因次/时间（转/秒），基准 s^-1 ---
_RPS_DIM = _t ** -1
_RPS = _reg(Unit("rps", _RPS_DIM, 1.0))
_RPM = _reg(Unit("rpm", _RPS_DIM, 1.0 / 60.0))

# --- 流量 ---
_M3H = _reg(Unit("m3/h", _L ** 3 / _t, 1.0 / 3600.0, aliases=("m³/h",)))
_LMIN = _reg(Unit("L/min", _L ** 3 / _t, 1e-3 / 60.0))
_KGH = _reg(Unit("kg/h", _M / _t, 1.0 / 3600.0))
_GS = _reg(Unit("g/s", _M / _t, 1e-3))

# --- 密度: M/L^3，基准 kg/m^3 ---
_KGM3 = _reg(Unit("kg/m3", _M / _L ** 3, 1.0, aliases=("kg/m³",)))
_GCM3 = _reg(Unit("g/cm3", _M / _L ** 3, 1000.0, aliases=("g/cm³",)))

# --- 浓度 ---
# 摩尔浓度 mol/L（基准 mol/m^3）
_MOLL = _reg(Unit("mol/L", _N / _L ** 3, 1000.0))
# 质量分数/百分数：无因次
_PCT = _reg(Unit("%", Dimension.dimensionless(), 1e-2, aliases=("percent",)))
_PPM = _reg(Unit("ppm", Dimension.dimensionless(), 1e-6))
_PH = _reg(Unit("pH", Dimension.dimensionless(), 1.0))

# --- 无因次 ---
_ONE = _reg(Unit("1", Dimension.dimensionless(), 1.0, aliases=("", "count")))


class UnitError(ValueError):
    """未知或不受支持的单位代码。"""


def get_unit(code: str | None) -> Unit:
    if code is None:
        return _ONE
    if code not in _UNITS:
        raise UnitError(f"未知 UCUM 单位: {code!r}")
    return _UNITS[code]


def known_units() -> list[str]:
    return sorted(u.code for u in _UNITS.values() if u.code)


def compatible(code_a: str | None, code_b: str | None) -> bool:
    """两个单位是否可互相换算（量纲相同）。"""
    return get_unit(code_a).dimension == get_unit(code_b).dimension


def convert(value: float, src: str | None, dst: str | None) -> float:
    """把数值从 src 单位换算到 dst 单位；量纲不一致时抛出。"""
    a, b = get_unit(src), get_unit(dst)
    if a.dimension != b.dimension:
        raise UnitError(
            f"量纲不一致: {src or '1'} [{a.dimension}] vs {dst or '1'} [{b.dimension}]"
        )
    return b.from_base(a.to_base(float(value)))


def same_dimension(code: str | None, dim: Dimension) -> bool:
    return get_unit(code).dimension == dim
