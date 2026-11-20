"""时序分区数据集接口：回填、更正、按指标/日期读取、逐日核对与版本链。

宽表 ``trend_wide_daily`` 仍保留可查；这里的读写都落在新分区，
切换期读取按日回退宽表，并逐日标注来源版本。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas import ActionResult
from app.services import partition_model as model
from app.services.series_store import SOURCE_MODES, series_store

router = APIRouter(prefix="/api/series", tags=["时序分区数据集"])


def _failure(error: Exception) -> ActionResult:
    return ActionResult(ok=False, message=str(error))


@router.get("/wide")
def list_wide(
    start: str = Query(default=model.WIDE_RETENTION[0]),
    end: str = Query(default=model.WIDE_RETENTION[1]),
) -> dict[str, object]:
    """读取旧宽表窗口（只读）；迁移不会删除宽表。"""
    rows = model.wide_rows(start, end)
    return {
        "数据集": model.WIDE_TABLE_NAME,
        "版本": model.wide_version(end),
        "起始日期": start,
        "截止日期": end,
        "行数": len(rows),
        "字段": ["日期", "gmv", "uv", "paid_uv", "total_uv"],
        "逐日": rows,
    }


@router.get("/partitions")
def list_partitions(
    metric_code: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict[str, object]:
    """列出分区观测；默认只返回有效版本，被替代的历史值可加 include_superseded 查看。"""
    return list_partitions_detail(metric_code, start, end, include_superseded=False)


@router.get("/partitions/history")
def list_partitions_detail(
    metric_code: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    include_superseded: bool = Query(default=True),
) -> dict[str, object]:
    rows = []
    for row in series_store._partitions():
        if metric_code and row.get("指标编号") != metric_code:
            continue
        if start and str(row.get("日期")) < start:
            continue
        if end and str(row.get("日期")) > end:
            continue
        if not include_superseded and row.get("状态") != "有效":
            continue
        rows.append(dict(row))
    rows.sort(key=lambda item: (str(item.get("日期")), str(item.get("指标编号")), int(item.get("版本", 0))))
    return {
        "数据集": "trend_series_partitioned",
        "行数": len(rows),
        "分区键": "dt",
        "唯一约束": "同一指标同一日仅一条状态=有效",
        "逐行": rows,
    }


@router.get("/coverage")
def coverage(
    metric_code: str = Query(default="METR-GMV"),
    start: str = Query(default=model.WIDE_RETENTION[0]),
    end: str = Query(default=model.WIDE_RETENTION[1]),
) -> dict[str, object]:
    """查看某指标逐日是否已进入新分区，以及哪些日期仍需回退宽表。"""
    try:
        return series_store.coverage(metric_code, start, end)
    except model.SeriesModelError as error:
        return {"error": str(error)}


@router.get("/series")
def read_series(
    metric_code: str = Query(...),
    start: str = Query(default=model.TEN_DAY_WINDOW[0]),
    end: str = Query(default=model.TEN_DAY_WINDOW[1]),
    mode: str = Query(default="switch", description="wide / switch / partition"),
) -> dict[str, object]:
    """按指标和日期读取时序，逐日标注来源；全量启用遇到缺口会明确报错。"""
    try:
        return series_store.read_series(metric_code, start, end, mode)
    except model.SeriesModelError as error:
        return {"error": str(error), "指标编号": metric_code, "来源模式": mode}


@router.get("/reconcile")
def reconcile(
    metric_code: str = Query(default="METR-GMV"),
    start: str = Query(default=model.WIDE_RETENTION[0]),
    end: str = Query(default=model.WIDE_RETENTION[1]),
) -> dict[str, object]:
    """把新分区的有效观测与宽表对应版本逐日核对原始计数和精度。"""
    try:
        return series_store.reconcile(metric_code, start, end)
    except model.SeriesModelError as error:
        return {"error": str(error)}


@router.get("/history")
def history(
    metric_code: str = Query(...),
    day: str = Query(..., description="日期，如 2026-08-28"),
) -> dict[str, object]:
    """查看某指标某日的完整版本链：宽表回填、销售更正与替代关系。"""
    rows = series_store.history(metric_code, day)
    if not rows:
        return {"error": f"{day} 的 {metric_code} 没有分区观测", "指标编号": metric_code, "日期": day}
    return {
        "指标编号": metric_code,
        "日期": day,
        "宽表版本": model.wide_version(day),
        "宽表原始计数": model.raw_from_wide(metric_code, model.wide_row(day))
        if model.wide_row(day) else None,
        "版本数": len(rows),
        "版本链": rows,
    }


@router.get("/batches")
def list_batches() -> dict[str, object]:
    """回填批次清单：状态、断点、写入/跳过数量。"""
    rows = series_store.list_batches()
    return {"items": rows, "total": len(rows)}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, object]:
    batch = series_store.get_batch(batch_id)
    if batch is None:
        return {"error": f"回填批次 {batch_id} 不存在"}
    return batch


@router.post("/backfill", response_model=ActionResult)
def submit_backfill(
    payload: dict[str, object] | None = None,
    batch_id: str | None = Query(default=None, description="复用已有批次号即从中断点恢复"),
    days: list[str] | None = Query(default=None, description="回填日期列表，缺省为保留窗口末六天"),
    metrics: list[str] | None = Query(default=None),
    interrupt_after: int | None = Query(
        default=None, description="测试用：写完第 N 条观测后模拟部署中断"),
) -> ActionResult:
    """提交（或恢复）宽表回填；重复提交幂等，不会产生第二条有效观测。

    ``days`` / ``metrics`` 既可以用重复查询参数，也可以放在 JSON 请求体里，
    前端恢复批次时直接把批次记录的窗口原样回传。
    """
    body = payload or {}
    batch_id = batch_id or (str(body.get("batch_id")) if body.get("batch_id") else None)
    body_days = body.get("days")
    body_metrics = body.get("metrics")
    if isinstance(body_days, list) and body_days:
        days = [str(day) for day in body_days]
    if isinstance(body_metrics, list) and body_metrics:
        metrics = [str(code) for code in body_metrics]
    try:
        outcome = series_store.submit_backfill(
            batch_id=batch_id, days=days, metrics=metrics, interrupt_after=interrupt_after)
    except model.SeriesModelError as error:
        return _failure(error)
    return ActionResult(
        ok=True,
        message=str(outcome["消息"]),
        entry={
            "批次": outcome["批次"],
            "写入数": outcome["写入数"],
            "跳过数": outcome["跳过数"],
            "是否需要重试": outcome["是否需要重试"],
        },
    )


@router.post("/corrections", response_model=ActionResult)
def submit_correction(payload: dict[str, object]) -> ActionResult:
    """登记销售更正：旧观测转已替代，新观测成为唯一有效值并保留版本链。"""
    metric_code = str(payload.get("指标编号") or "")
    day = str(payload.get("日期") or "")
    if not metric_code or not day:
        return ActionResult(ok=False, message="更正必须提供 指标编号 与 日期")
    raw = payload.get("原始计数")
    value = payload.get("观测值")
    try:
        outcome = series_store.correct(
            metric_code=metric_code,
            day=day,
            value=float(value) if value is not None else None,
            raw=dict(raw) if isinstance(raw, dict) else None,
            reason=str(payload.get("原因") or ""),
            source_version=str(payload.get("来源版本") or "") or None,
        )
    except model.SeriesModelError as error:
        return _failure(error)
    return ActionResult(
        ok=True,
        message=f"{day} 的 {metric_code} 已更正为 {outcome['新观测']['观测值']}",
        entry={
            "原观测值": outcome["原观测"]["观测值"],
            "新观测值": outcome["新观测"]["观测值"],
            "新观测": outcome["新观测"],
        },
    )


@router.get("/modes")
def list_modes() -> dict[str, object]:
    """可用的来源模式与说明。"""
    return {
        "default": "switch",
        "items": [{"代码": key, "名称": name} for key, name in SOURCE_MODES.items()],
    }
