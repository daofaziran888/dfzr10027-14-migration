"""时序分区数据模型：宽表版本、指标口径与观测记录构造。

这一层不依赖 store，只负责把「宽表的一行」翻译成「某个指标某一天的观测」，
让迁移、回填、更正和训练入口共用同一套口径：

* 指标值怎么算（GMV 直接取金额，UV 取人数，CVR 用 paid_uv / total_uv）；
* 观测值保留几位小数（沿用 metric_def 里的 ``精度位数``）；
* 原始计数长什么样（回填后要能逐日与宽表版本核对）；
* CVR 的分子和分母必须标注同一个来源版本。

store 只负责持久化与版本管理，口径判断全部收在这里。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.fixtures import METRIC_DEFS, TREND_POINTS

# 旧宽表：一天一行，GMV / UV / 转化率原始计数挤在同一行。
WIDE_TABLE_NAME = "trend_wide_daily"
# 本次周报保留的十天宽表窗口；宽表不会被迁移流程删除。
WIDE_RETENTION = ("2026-08-21", "2026-08-30")
# 周报要求覆盖的十天窗口。
TEN_DAY_WINDOW = ("2026-08-21", "2026-08-30")

# 迁移/回填默认处理的指标顺序。
METRIC_ORDER = ["METR-GMV", "METR-UV", "METR-CVR"]

_DEF_BY_CODE = {item["指标编号"]: item for item in METRIC_DEFS}

_UNITS = {
    "METR-GMV": "元",
    "METR-UV": "人",
    "METR-CVR": "比率",
}

# 指标的计算语义：可加指标按日求和，比率指标按分子分母汇总后相除。
_AGGREGATION = {
    "METR-GMV": "sum",
    "METR-UV": "sum",
    "METR-CVR": "ratio",
}


class SeriesModelError(ValueError):
    """口径不合法时抛出，路由层会把它转成可读的业务错误。"""


class SeriesCoverageError(SeriesModelError):
    """训练/报表所需日期在选定存储里不完整。"""


def metric_name(metric_code: str) -> str:
    definition = _DEF_BY_CODE.get(metric_code)
    return str(definition["指标名称"]) if definition else metric_code


def metric_precision(metric_code: str) -> int:
    definition = _DEF_BY_CODE.get(metric_code)
    if definition is None:
        raise SeriesModelError(f"指标 {metric_code} 未在 metric_def 中定义")
    return int(definition["精度位数"])


def metric_unit(metric_code: str) -> str:
    return _UNITS.get(metric_code, "")


def metric_aggregation(metric_code: str) -> str:
    return _AGGREGATION.get(metric_code, "sum")


def known_metric(metric_code: str) -> bool:
    return metric_code in _DEF_BY_CODE


def quantize(value: float, precision: int) -> float | int:
    """按指标精度四舍五入；精度为 0 的指标返回整数，避免 5750.0 这种噪声。"""
    if precision <= 0:
        return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    step = Decimal(1).scaleb(-precision)
    return float(Decimal(str(value)).quantize(step, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# 旧宽表读取（只读；迁移不删除宽表，结账版本始终可按日期回查）
# ---------------------------------------------------------------------------

def wide_rows(start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    rows = []
    for row in TREND_POINTS:
        day = str(row["日期"])
        if start and day < start:
            continue
        if end and day > end:
            continue
        rows.append(dict(row))
    return rows


def wide_row(day: str) -> dict[str, Any] | None:
    for row in TREND_POINTS:
        if str(row["日期"]) == day:
            return dict(row)
    return None


def wide_version(day: str | None = None) -> str:
    """宽表版本号；保留窗口以最后一天为版本，便于核对同一批结账数。"""
    snapshot_day = day or WIDE_RETENTION[1]
    return f"{WIDE_TABLE_NAME}@{snapshot_day}"


# ---------------------------------------------------------------------------
# 口径计算
# ---------------------------------------------------------------------------

def raw_from_wide(metric_code: str, wide: dict[str, Any]) -> dict[str, Any]:
    """从宽表一行抽出某个指标的原始计数（含 CVR 的分子分母）。"""
    if metric_code == "METR-GMV":
        return {"gmv": float(wide["gmv"])}
    if metric_code == "METR-UV":
        return {"uv": int(wide["uv"])}
    if metric_code == "METR-CVR":
        # 分子分母必须来自同一行，也就是同一个宽表版本。
        return {"paid_uv": int(wide["paid_uv"]), "total_uv": int(wide["total_uv"])}
    raise SeriesModelError(f"指标 {metric_code} 没有定义原始计数口径")


def value_from_raw(metric_code: str, raw: dict[str, Any]) -> float | int:
    precision = metric_precision(metric_code)
    if metric_code == "METR-GMV":
        return quantize(float(raw["gmv"]), precision)
    if metric_code == "METR-UV":
        return quantize(float(raw["uv"]), precision)
    if metric_code == "METR-CVR":
        total = float(raw.get("total_uv") or 0)
        paid = float(raw.get("paid_uv") or 0)
        return quantize(paid / total if total else 0.0, precision)
    raise SeriesModelError(f"指标 {metric_code} 没有定义取值口径")


def raw_from_value(metric_code: str, value: float) -> dict[str, Any]:
    """更正数值时反推原始计数；CVR 必须显式给出分子分母。"""
    if metric_code == "METR-GMV":
        return {"gmv": quantize(float(value), metric_precision(metric_code))}
    if metric_code == "METR-UV":
        return {"uv": quantize(float(value), metric_precision(metric_code))}
    raise SeriesModelError(
        f"{metric_code} 是比率指标，更正时须同时提供 paid_uv 与 total_uv，不能只给一个比率值")


def component_sources(metric_code: str, source_version: str) -> dict[str, str]:
    """比率指标记录分子分母各自的来源版本，便于检查是否同源。"""
    if metric_code == "METR-CVR":
        return {"paid_uv": source_version, "total_uv": source_version}
    return {}


def ensure_same_component_source(observation: dict[str, Any]) -> None:
    """CVR 的分子与分母必须来自同一来源版本，混版直接判为无效观测。"""
    if observation.get("指标编号") != "METR-CVR":
        return
    versions = observation.get("分量来源版本") or {}
    distinct = {str(value) for value in versions.values() if value}
    if len(distinct) > 1:
        raise SeriesModelError(
            f"{observation.get('日期')} 的转化率分子分母来源不一致：{versions}")


def make_observation(
    metric_code: str,
    day: str,
    raw: dict[str, Any],
    *,
    source_type: str,
    source_version: str,
    source_dataset: str,
    batch: str,
    version: int,
    row_id: int,
    note: str = "",
    created_at: str | None = None,
) -> dict[str, Any]:
    """构造一条分区观测；同一指标同一天只会有一条 ``状态=有效`` 的版本。"""
    precision = metric_precision(metric_code)
    observation = {
        "id": row_id,
        "分区": f"dt={day}",
        "指标编号": metric_code,
        "指标名称": metric_name(metric_code),
        "日期": day,
        "观测值": value_from_raw(metric_code, raw),
        "精度位数": precision,
        "单位": metric_unit(metric_code),
        "原始计数": dict(raw),
        "来源类型": source_type,
        "来源版本": source_version,
        "来源数据集": source_dataset,
        "批次号": batch,
        "版本": version,
        "状态": "有效",
        "替代自": None,
        "替代为": None,
        "构建时间": created_at,
        "备注": note,
    }
    sources = component_sources(metric_code, source_version)
    if sources:
        observation["分量来源版本"] = sources
    ensure_same_component_source(observation)
    return observation


# ---------------------------------------------------------------------------
# 聚合与核对
# ---------------------------------------------------------------------------

def aggregate_points(metric_code: str, points: list[dict[str, Any]]) -> float | int:
    """把多天观测汇总成一个数：可加指标求和，转化率按分子分母汇总。"""
    precision = metric_precision(metric_code)
    if not points:
        return quantize(0, precision)
    if metric_aggregation(metric_code) == "ratio":
        raw = {"paid_uv": 0.0, "total_uv": 0.0}
        for point in points:
            for key in raw:
                raw[key] += float((point.get("原始计数") or {}).get(key) or 0)
        return quantize(raw["paid_uv"] / raw["total_uv"] if raw["total_uv"] else 0.0, precision)
    total = sum(float(point.get("观测值") or 0) for point in points)
    return quantize(total, precision)
