"""时序分区迁移测试：回填幂等、销售更正、逐日核对、预测来源与产物追溯。"""
from __future__ import annotations

import pytest

from app.services import forecast_training
from app.services import partition_model as model
from app.services.pipeline_orchestrator import run_all_pipelines, run_forecast_pipeline
from app.services.series_store import series_store
from app.store import store

BATCH = "MIG-20260830-01"
WINDOW = ["2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30"]
METRICS = ["METR-GMV", "METR-UV", "METR-CVR"]


def finish_seeded_batch() -> dict:
    """种子里批次停在「中断」，这里按同一批次号再次提交，把它走完。"""
    return series_store.submit_backfill(batch_id=BATCH, days=WINDOW, metrics=METRICS)


class TestWideTable:
    def test_wide_table_keeps_ten_days(self):
        rows = model.wide_rows("2026-08-21", "2026-08-30")
        assert [row["日期"] for row in rows] == [
            f"2026-08-{day}" for day in range(21, 31)]
        assert rows[-1]["gmv"] == 238500.0

    def test_wide_table_keeps_checkout_value(self):
        assert model.wide_row("2026-08-28")["gmv"] == 232000.0

    def test_wide_version_per_day(self):
        assert model.wide_version("2026-08-28") == "trend_wide_daily@2026-08-28"


class TestBackfill:
    def test_resume_interrupted_batch_is_idempotent(self):
        before = len(store.rows("_series_partitions"))
        outcome = finish_seeded_batch()
        # 种子已写入 18 条，恢复提交只补状态，不再多写一条观测。
        assert outcome["写入数"] == 0
        assert outcome["是否需要重试"] is False
        batch = series_store.get_batch(BATCH)
        assert batch["状态"] == "已完成"
        assert batch["尝试次数"] == 2
        assert len(store.rows("_series_partitions")) == before
        assert batch["中断原因"] is None

    def test_replay_from_zero_cursor_skips_all(self):
        """断点没来得及落盘时，重放整批只跳过，不产生重复观测。"""
        for row in store.rows("_series_batches"):
            if row["批次号"] == BATCH:
                row["断点"] = 0
                row["状态"] = "中断"
        before = len(store.rows("_series_partitions"))
        outcome = finish_seeded_batch()
        assert outcome["写入数"] == 0
        assert outcome["跳过数"] == 18
        assert outcome["批次"]["状态"] == "已完成"
        assert len(store.rows("_series_partitions")) == before


    def test_repeat_submit_keeps_single_effective(self):
        finish_seeded_batch()
        finish_seeded_batch()
        for day in WINDOW:
            for metric in METRICS:
                effective = [
                    row for row in store.rows("_series_partitions")
                    if row["日期"] == day and row["指标编号"] == metric
                    and row["状态"] == "有效"]
                assert len(effective) == 1, (day, metric, effective)

    def test_correction_keeps_single_effective_globally(self):
        finish_seeded_batch()
        series_store.correct(metric_code="METR-GMV", day="2026-08-28", value=242000)
        series_store.correct(metric_code="METR-CVR", day="2026-08-29", raw={
            "paid_uv": 661, "total_uv": 5500})
        keys = [
            (row["指标编号"], row["日期"])
            for row in store.rows("_series_partitions") if row["状态"] == "有效"]
        assert len(keys) == len(set(keys))

    def test_interrupt_then_resume_writes_rest(self):
        days = ["2026-08-21", "2026-08-22"]
        first = series_store.submit_backfill(
            batch_id="MIG-TEST-INT", days=days, metrics=["METR-GMV"], interrupt_after=1)
        assert first["批次"]["状态"] == "中断"
        assert first["写入数"] == 1
        assert first["是否需要重试"] is True
        assert series_store.effective("METR-GMV", "2026-08-21") is not None
        assert series_store.effective("METR-GMV", "2026-08-22") is None

        second = series_store.submit_backfill(
            batch_id="MIG-TEST-INT", days=days, metrics=["METR-GMV"])
        assert second["批次"]["状态"] == "已完成"
        assert second["写入数"] == 1
        # 断点已落盘，恢复时从中断点继续，不再回头扫描已写的那一天。
        assert second["跳过数"] == 0
        assert series_store.effective("METR-GMV", "2026-08-22") is not None

    def test_reused_batch_code_with_other_window_rejected(self):
        finish_seeded_batch()
        with pytest.raises(model.SeriesModelError):
            series_store.submit_backfill(
                batch_id=BATCH, days=["2026-08-24"], metrics=METRICS)

    def test_backfilled_raw_matches_wide(self):
        finish_seeded_batch()
        for day in WINDOW:
            wide = model.wide_row(day)
            for metric in METRICS:
                observation = series_store.effective(metric, day)
                if metric == "METR-GMV" and day == "2026-08-28":
                    # 销售更正后这一天的有效观测已不是宽表值；回填出来的历史版本才是。
                    assert observation["原始计数"] == {"gmv": 242000.0}
                    backfilled = series_store.history(metric, day)[0]
                    assert backfilled["原始计数"] == model.raw_from_wide(metric, wide)
                    continue
                assert observation["原始计数"] == model.raw_from_wide(metric, wide)
                assert observation["来源版本"] == model.wide_version(day)


class TestCorrection:
    def test_0828_gmv_corrected_to_242000(self):
        finish_seeded_batch()
        gmv = series_store.effective("METR-GMV", "2026-08-28")
        assert gmv["观测值"] == 242000.0
        assert gmv["来源类型"] == "销售更正"
        # 宽表保持结账值，两个版本可同时查到。
        assert model.wide_row("2026-08-28")["gmv"] == 232000.0

    def test_correction_chain_keeps_history(self):
        finish_seeded_batch()
        chain = series_store.history("METR-GMV", "2026-08-28")
        assert [row["版本"] for row in chain] == [1, 2]
        assert chain[0]["状态"] == "已替代"
        assert chain[0]["观测值"] == 232000.0
        assert chain[0]["替代为"] == chain[1]["id"]
        assert chain[1]["状态"] == "有效"
        assert chain[1]["替代自"] == chain[0]["id"]
        assert sum(1 for row in chain if row["状态"] == "有效") == 1

    def test_correction_requires_existing_partition(self):
        # 08-21 只有宽表，没有分区观测，不能直接更正。
        with pytest.raises(model.SeriesModelError):
            series_store.correct(metric_code="METR-GMV", day="2026-08-21", value=1.0)

    def test_cvr_correction_requires_both_components(self):
        finish_seeded_batch()
        with pytest.raises(model.SeriesModelError):
            series_store.correct(metric_code="METR-CVR", day="2026-08-28", value=0.13)

    def test_cvr_components_share_source(self):
        finish_seeded_batch()
        observation = series_store.effective("METR-CVR", "2026-08-28")
        versions = observation["分量来源版本"]
        assert versions["paid_uv"] == versions["total_uv"] == observation["来源版本"]


class TestReadAndReconcile:
    def test_switch_mode_covers_ten_days(self):
        finish_seeded_batch()
        dataset = series_store.read_series("METR-GMV", "2026-08-21", "2026-08-30", "switch")
        assert dataset["点数"] == 10
        assert dataset["来源汇总"] == {"宽表存量": 9, "销售更正": 1}
        assert dataset["点"][0]["来源数据集"] == model.WIDE_TABLE_NAME
        assert dataset["点"][-1]["分区"] == "dt=2026-08-30"

    def test_partition_mode_uses_available_range(self):
        finish_seeded_batch()
        dataset = series_store.read_series("METR-GMV", "2026-08-21", "2026-08-30", "partition")
        # 前四天还没入新分区，实际观测范围从 08-25 开始，截止日不变。
        assert dataset["实际起始日期"] == "2026-08-25"
        assert dataset["训练截止日"] == "2026-08-30"
        assert dataset["少算日期"] == ["2026-08-21", "2026-08-22", "2026-08-23", "2026-08-24"]
        assert dataset["点数"] == 6
        assert dataset["来源汇总"] == {"宽表存量": 5, "销售更正": 1}

    def test_partition_mode_rejects_inner_hole(self):
        for row in store.rows("_series_batches"):
            if row["批次号"] == BATCH:
                row["状态"] = "已取消"
        series_store.submit_backfill(
            batch_id="MIG-HOLE", days=["2026-08-26"], metrics=["METR-GMV"])
        # 08-25 与 08-27 至 08-30 已有种子观测，08-26 也写进去了；
        # 人为删掉中间一天，模拟迁移漏数，全量启用模式必须拒绝带洞训练。
        store.rows("_series_partitions").remove(
            series_store.effective("METR-GMV", "2026-08-27"))
        with pytest.raises(model.SeriesCoverageError):
            series_store.read_series("METR-GMV", "2026-08-25", "2026-08-30", "partition")

    def test_read_accepts_chinese_mode_name(self):
        dataset = series_store.read_series("METR-GMV", "2026-08-21", "2026-08-30", "切换期")
        assert dataset["来源模式"] == "switch"

    def test_reconcile_with_wide_version(self):
        finish_seeded_batch()
        gmv = series_store.reconcile("METR-GMV", "2026-08-21", "2026-08-30")
        # 已核对 = 分区里有有效观测的天数（含被更正那天），未回填 = 仍只能读宽表的天数。
        assert gmv["已核对天数"] == 6
        assert gmv["更正天数"] == 1
        assert gmv["未回填天数"] == 4
        assert gmv["已核对天数"] + gmv["未回填天数"] == gmv["总天数"]
        corrected = [row for row in gmv["逐日"] if row["结论"].startswith("已更正")]
        assert corrected[0]["日期"] == "2026-08-28"
        assert corrected[0]["宽表原始计数"] == {"gmv": 232000.0}
        assert corrected[0]["分区原始计数"] == {"gmv": 242000.0}
        assert corrected[0]["观测值差异"] == 10000.0

    def test_reconcile_uv_precision(self):
        finish_seeded_batch()
        uv = series_store.reconcile("METR-UV", "2026-08-25", "2026-08-30")
        for row in uv["逐日"]:
            assert row["精度一致"] is True
            assert isinstance(row["分区观测值"], int)


class TestForecastTraining:
    def test_ten_day_forecast_from_training_entry(self):
        finish_seeded_batch()
        report = forecast_training.save_report(metric_code="METR-GMV")
        assert report["预测周期"] == 10
        assert len(report["预测"]) == 10
        assert report["训练截止日"] == "2026-08-30"
        assert report["预测"][0]["日期"] == "2026-08-31"
        assert report["预测"][-1]["日期"] == "2026-09-09"

    def test_report_records_cutoff_and_sources(self):
        finish_seeded_batch()
        report = forecast_training.save_report(metric_code="METR-GMV")
        assert report["来源模式"] == "switch"
        assert report["训练来源"] == {"宽表存量": 9, "销售更正": 1}
        # 宽表窗口合计 2142300，08-28 按销售更正 +10000 后为 2152300。
        assert report["训练区间合计"] == 2152300.0
        assert len(report["训练明细"]) == 10
        assert all("来源版本" in point for point in report["训练明细"])

    def test_report_says_not_checkout_replayable(self):
        finish_seeded_batch()
        report = forecast_training.save_report(metric_code="METR-GMV")
        # 08-28 已按销售更正训练，不能再用来复核结账版。
        assert report["已反映销售更正"] is True
        assert report["可用于复核结账版"] is False
        assert report["已校正日期"] == ["2026-08-28"]

    def test_wide_mode_replays_checkout_version(self):
        finish_seeded_batch()
        report = forecast_training.save_report(metric_code="METR-GMV", mode="wide")
        assert report["已反映销售更正"] is False
        assert report["可用于复核结账版"] is True
        assert report["训练区间合计"] == 2142300.0
        assert report["训练来源"] == {"宽表存量": 10}

    def test_recompute_switches_modes(self):
        finish_seeded_batch()
        report = forecast_training.save_report(metric_code="METR-GMV")
        switched = forecast_training.recompute_report(report["id"], "switch")
        assert switched["合计一致"] is True
        assert switched["预测一致"] is True

        strict = forecast_training.recompute_report(report["id"], "partition")
        assert strict["训练来源"] == {"宽表存量": 5, "销售更正": 1}
        assert strict["训练区间合计"] == 1350500.0
        assert strict["合计一致"] is False

    def test_old_report_survives_later_correction(self):
        finish_seeded_batch()
        report = forecast_training.save_report(metric_code="METR-GMV")
        original_total = report["训练区间合计"]
        series_store.correct(metric_code="METR-GMV", day="2026-08-25", value=205000)

        stored = forecast_training.get_report(report["id"])
        assert stored["训练区间合计"] == original_total
        assert [point["观测值"] for point in stored["训练明细"]
                if point["日期"] == "2026-08-25"] == [205400.0]

        fresh = forecast_training.recompute_report(report["id"], "switch")
        assert fresh["训练区间合计"] == round(original_total - 400, 2)
        assert fresh["合计一致"] is False

    def test_cvr_forecast_uses_same_source_ratios(self):
        finish_seeded_batch()
        report = forecast_training.save_report(metric_code="METR-CVR")
        assert report["训练点数"] == 10
        for point in report["训练明细"]:
            raw = point["原始计数"]
            assert round(raw["paid_uv"] / raw["total_uv"], 4) == point["观测值"]


class TestPipelineWiring:
    def test_pipeline_forecast_uses_partition_store(self):
        finish_seeded_batch()
        result = run_forecast_pipeline(horizon=10)
        assert result["metric_code"] == "METR-GMV"
        assert result["training_cutoff"] == "2026-08-30"
        assert result["source_mode"] == "switch"
        assert result["horizon"] == 10
        assert len(result["forecasts"]) == 10
        assert result["report_id"] == 1
        assert result["corrected_days"] == ["2026-08-28"]

    def test_run_all_pipelines_still_writes_seven_results(self):
        finish_seeded_batch()
        summary = run_all_pipelines()
        assert len(summary) == 7
        assert summary["forecast_report"]["source_mode"] == "switch"

    def test_pipeline_forecast_reports_coverage_error(self):
        series_store.submit_backfill(
            batch_id="MIG-PARTIAL", days=["2026-08-29", "2026-08-30"], metrics=["METR-GMV"])
        result = run_forecast_pipeline(horizon=10, metric_code="METR-GMV", source_mode="partition")
        # 窗口内 08-25 至 08-28 都有分区观测（种子），08-21 至 08-24 尚未入分区，
        # 实际范围从 08-25 开始，因此可以训练，只是少算前四天。
        assert result["training_cutoff"] == "2026-08-30"
        assert result["source_mode"] == "partition"
