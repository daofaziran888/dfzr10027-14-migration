"""预测训练入口：从迁移后的分区数据集取实际观测，生成可追溯的报表产物。

训练入口（``_forecast_training_entries``）只描述「用哪个指标、哪段窗口、哪种来源模式」，
真正的观测范围由分区存储的可用日期决定：切换期里尚未入分区的日期回退宽表，
全量启用模式则要求窗口内全部日期都已在分区中。

每次生成都会留下一条带版本的报表产物，训练截止日、逐日来源和算法参数都写在产物里；
切换存储之后旧产物不会被覆盖，仍可按报表编号复查。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services import partition_model as model
from app.services import series_store
from app.services.forecast_engine import (
    exponential_smoothing_forecast,
    linear_forecast,
    moving_average_forecast,
)
from app.store import store

TRAINING_MODULE = "_forecast_training_entries"
REPORT_MODULE = "_forecast_reports"


class ForecastTrainingError(ValueError):
    """训练入口配置或训练数据不满足要求。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def training_entries() -> list[dict[str, Any]]:
    return store.rows(TRAINING_MODULE)


def find_training_entry(code: str | None = None, metric_code: str | None = None) -> dict[str, Any]:
    rows = training_entries()
    if not rows:
        raise ForecastTrainingError("没有可用的预测训练入口，请先登记训练入口")
    if code:
        for row in rows:
            if row.get("训练编号") == code:
                return row
        raise ForecastTrainingError(f"训练入口 {code} 不存在")
    if metric_code:
        for row in rows:
            if row.get("预测指标") == metric_code:
                return row
    return rows[0]


def _algorithm_forecast(
    algorithm: str, values: list[float], horizon: int,
) -> dict[str, Any]:
    if "移动平均" in algorithm or "移动" in algorithm:
        return moving_average_forecast(values, min(7, max(2, len(values))), horizon)
    if "指数平滑" in algorithm or "平滑" in algorithm:
        return exponential_smoothing_forecast(values, 0.3, horizon)
    return linear_forecast(values, horizon)


def build_forecast(
    *,
    training_code: str | None = None,
    metric_code: str | None = None,
    mode: str | None = None,
    horizon: int | None = None,
) -> dict[str, Any]:
    """按训练入口读取实际观测范围并执行预测，不写报表产物。"""
    entry = find_training_entry(training_code, metric_code)
    target_metric = str(entry.get("预测指标"))
    start = str(entry.get("训练窗口起"))
    end = str(entry.get("训练窗口止"))
    source_mode = series_store.series_store.validate_mode(
        mode or str(entry.get("来源模式") or series_store.DEFAULT_SOURCE_MODE))
    steps = int(horizon or str(entry.get("预测周期", "10")).replace("天", "") or 10)

    dataset = series_store.series_store.read_series(target_metric, start, end, source_mode)
    points = dataset["点"]
    values = [float(point["观测值"]) for point in points]

    algorithm = str(entry.get("预测算法") or "线性趋势")
    result = _algorithm_forecast(algorithm, values, steps)
    if "error" in result:
        raise ForecastTrainingError(f"{target_metric} 训练数据不足：{result['error']}")

    predictions = [float(value) for value in result["predictions"]]
    precision = model.metric_precision(target_metric)
    last_day = datetime.strptime(dataset["训练截止日"], "%Y-%m-%d").date()
    forecast_rows = [
        {
            "日期": (last_day + timedelta(days=index + 1)).isoformat(),
            "预测值": model.quantize(predictions[index], precision),
            "置信下界": model.quantize(predictions[index] * 0.92, precision),
            "置信上界": model.quantize(predictions[index] * 1.08, precision),
        }
        for index in range(steps)
    ]

    actual_total = model.aggregate_points(target_metric, points)
    forecast_total = model.quantize(
        sum(predictions), model.metric_precision(target_metric))
    corrected_days = [
        point["日期"] for point in points
        if _is_corrected(target_metric, point)
    ]

    return {
        "训练编号": entry.get("训练编号"),
        "预测指标": target_metric,
        "指标名称": model.metric_name(target_metric),
        "预测算法": algorithm,
        "算法标识": result.get("model", "linear_trend"),
        "预测周期": steps,
        "来源模式": source_mode,
        "来源模式名称": series_store.SOURCE_MODES[source_mode],
        "训练窗口": {"起": start, "止": end},
        "实际训练范围": {"起": dataset["实际起始日期"], "止": dataset["训练截止日"]},
        "少算日期": dataset.get("少算日期", []),
        "训练截止日": dataset["训练截止日"],
        "训练点数": len(points),
        "训练来源": dataset["来源汇总"],
        "训练明细": points,
        "训练区间合计": actual_total,
        "预测合计": forecast_total,
        "预测": forecast_rows,
        "已校正日期": corrected_days,
        "已反映销售更正": bool(corrected_days),
        "可用于复核结账版": not corrected_days,
        "模型参数": {
            key: result.get(key)
            for key in ("slope", "intercept", "r2", "window", "alpha", "mse", "mape")
            if key in result
        },
    }


def _is_corrected(metric_code: str, point: dict[str, Any]) -> bool:
    """判断参与训练的某一天是否已偏离结账时的宽表版本。"""
    wide = model.wide_row(point["日期"])
    if wide is None:
        return False
    return (point.get("原始计数") or {}) != model.raw_from_wide(metric_code, wide)


def save_report(
    *,
    report_code: str | None = None,
    training_code: str | None = None,
    metric_code: str | None = None,
    mode: str | None = None,
    horizon: int | None = None,
) -> dict[str, Any]:
    """生成并落库一条报表产物；每次生成都是新版本，旧产物保留可查。"""
    result = build_forecast(
        training_code=training_code, metric_code=metric_code, mode=mode, horizon=horizon)
    rows = store.rows(REPORT_MODULE)
    if report_code:
        # 同一报表编号重复生成时追加版本号（v2、v3），旧版本按 id 仍可复查。
        same_code = sum(1 for row in rows if str(row.get("报表编号", "")).split("#")[0] == report_code)
        code = report_code if same_code == 0 else f"{report_code}#v{same_code + 1}"
    else:
        code = None
    report_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    code = code or f"FREP-{result['预测指标'].replace('METR-', '')}-{report_id:03d}"
    artifact = {
        "id": report_id,
        "报表编号": code,
        "生成时间": _now(),
        "状态": "已生成",
        **result,
    }
    rows.append(artifact)
    return dict(artifact)


def reports() -> list[dict[str, Any]]:
    return [dict(row) for row in store.rows(REPORT_MODULE)]


def get_report(report_id: int) -> dict[str, Any] | None:
    for row in store.rows(REPORT_MODULE):
        if int(row.get("id", 0)) == int(report_id):
            return dict(row)
    return None


def recompute_report(report_id: int, mode: str) -> dict[str, Any]:
    """按当前存储复算指定模式下的十日合计与预测值，并与产物原值对照。"""
    stored = get_report(report_id)
    if stored is None:
        raise ForecastTrainingError(f"预测报表 {report_id} 不存在")
    fresh = build_forecast(
        metric_code=stored["预测指标"],
        mode=mode,
        horizon=int(stored.get("预测周期") or 10),
    )
    return {
        "报表编号": stored.get("报表编号"),
        "原产物": {
            "来源模式": stored.get("来源模式"),
            "训练截止日": stored.get("训练截止日"),
            "训练区间合计": stored.get("训练区间合计"),
            "预测合计": stored.get("预测合计"),
            "预测": stored.get("预测"),
            "训练来源": stored.get("训练来源"),
        },
        "复算模式": fresh["来源模式"],
        "复算模式名称": fresh["来源模式名称"],
        "训练截止日": fresh["训练截止日"],
        "训练来源": fresh["训练来源"],
        "训练区间合计": fresh["训练区间合计"],
        "预测合计": fresh["预测合计"],
        "预测": fresh["预测"],
        "合计一致": fresh["训练区间合计"] == stored.get("训练区间合计"),
        "预测一致": [row["预测值"] for row in fresh["预测"]]
        == [row["预测值"] for row in (stored.get("预测") or [])],
    }
