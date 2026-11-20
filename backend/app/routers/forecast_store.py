"""预测训练与报表产物接口：从迁移后的训练入口取观测范围，产物可追溯、可复算。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas import ActionResult
from app.services import forecast_training

router = APIRouter(prefix="/api/forecast_store", tags=["预测训练与产物"])


@router.get("/training-entries")
def list_training_entries() -> dict[str, object]:
    """预测训练入口：声明指标、训练窗口、预测周期与来源模式。"""
    rows = forecast_training.training_entries()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


@router.post("/run", response_model=ActionResult)
def run_forecast(
    training_code: str | None = Query(default=None),
    metric_code: str | None = Query(default=None),
    mode: str | None = Query(default=None, description="wide / switch / partition"),
    horizon: int | None = Query(default=None, ge=1, le=30),
) -> ActionResult:
    """执行十日预测并落一条报表产物；产物里带训练截止日与逐日来源。"""
    try:
        report = forecast_training.save_report(
            training_code=training_code, metric_code=metric_code, mode=mode, horizon=horizon)
    except (forecast_training.ForecastTrainingError, ValueError) as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(
        ok=True,
        message=f"预测已完成：训练截止 {report['训练截止日']}，来源 {report['来源模式名称']}",
        entry=report,
    )


@router.get("/reports")
def list_reports() -> dict[str, object]:
    """报表产物清单；每次生成都是新版本，切换存储后旧版本仍可按编号复查。"""
    rows = forecast_training.reports()
    summary = [
        {
            "id": row["id"],
            "报表编号": row["报表编号"],
            "预测指标": row["预测指标"],
            "生成时间": row["生成时间"],
            "来源模式": row["来源模式"],
            "来源模式名称": row["来源模式名称"],
            "训练截止日": row["训练截止日"],
            "训练点数": row["训练点数"],
            "训练区间合计": row["训练区间合计"],
            "预测合计": row["预测合计"],
            "已反映销售更正": row["已反映销售更正"],
            "可用于复核结账版": row["可用于复核结账版"],
        }
        for row in rows
    ]
    return {"items": summary, "total": len(summary)}


@router.get("/reports/{report_id}")
def get_report(report_id: int) -> dict[str, object]:
    """完整产物明细，含训练逐日明细，可从预测结果打开关联的训练明细。"""
    report = forecast_training.get_report(report_id)
    if report is None:
        return {"error": f"预测报表 {report_id} 不存在"}
    return report


@router.get("/reports/{report_id}/recompute")
def recompute(
    report_id: int,
    mode: str = Query(default="switch", description="wide / switch / partition"),
) -> dict[str, object]:
    """按当前存储复算同一窗口的十日合计与预测值，并与产物原值对照。"""
    try:
        return forecast_training.recompute_report(report_id, mode)
    except (forecast_training.ForecastTrainingError, ValueError) as error:
        return {"error": str(error)}
