"""时序分区数据集：宽表回填、销售更正、按指标/日期读取与逐日核对。

存储形态是一张「指标 × 日期」可裁剪的观测表（分区键 ``dt=YYYY-MM-DD``）：
同一指标同一天只保留一条 ``状态=有效`` 的观测，被更正的历史值改为 ``已替代``，
用 ``替代自`` / ``替代为`` 串起版本链，既满足唯一有效观测，也保留更正痕迹。

宽表 ``trend_wide_daily`` 始终只读保留，迁移与回填不会删除它，
所以结账时点（含 08-28 的 232000）始终可以按日期回查。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import partition_model as model
from app.store import store

PARTITION_MODULE = "_series_partitions"
BATCH_MODULE = "_series_batches"

# 读取/训练时的来源模式：切换期允许按日回退到宽表，全量启用只认新分区。
SOURCE_MODES = {
    "wide": "宽表",
    "switch": "切换期",
    "partition": "全量启用",
}
DEFAULT_SOURCE_MODE = "switch"

DATASET_NAME = "trend_series_partitioned"
CORRECTION_DATASET = "sales_correction"


class SeriesStoreService:
    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _partitions(self) -> list[dict[str, Any]]:
        return store.rows(PARTITION_MODULE)

    def effective(self, metric_code: str, day: str) -> dict[str, Any] | None:
        """取某指标某日唯一的有效观测。"""
        for row in self._partitions():
            if (row.get("指标编号") == metric_code
                    and row.get("日期") == day
                    and row.get("状态") == "有效"):
                return row
        return None

    def _history(self, metric_code: str, day: str) -> list[dict[str, Any]]:
        return [
            row for row in self._partitions()
            if row.get("指标编号") == metric_code and row.get("日期") == day
        ]

    def _next_id(self) -> int:
        return max((int(row.get("id", 0)) for row in self._partitions()), default=0) + 1

    def validate_mode(self, mode: str) -> str:
        """接受代码（switch）或中文名称（切换期），统一归一到代码。"""
        key = (mode or DEFAULT_SOURCE_MODE).strip()
        if key in SOURCE_MODES:
            return key
        for code, label in SOURCE_MODES.items():
            if key == label:
                return code
        raise model.SeriesModelError(
            f"来源模式「{mode}」不可用，可选：{'、'.join(SOURCE_MODES)} 或 "
            f"{'、'.join(SOURCE_MODES.values())}")

    # ------------------------------------------------------------------
    # 回填 / 迁移批次
    # ------------------------------------------------------------------
    def list_batches(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(BATCH_MODULE)]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if row.get("批次号") == batch_id:
                return dict(row)
        return None

    def resolve_window(
        self, start: str | None, end: str | None, days: list[str] | None = None,
    ) -> list[str]:
        """解析回填窗口；默认只回填保留窗口里的末六天。"""
        if days:
            window = [str(day) for day in days]
        else:
            window = [
                str(row["日期"]) for row in model.wide_rows(
                    start or model.WIDE_RETENTION[0], end or model.WIDE_RETENTION[1])
            ]
        if not window:
            raise model.SeriesModelError("回填窗口为空，请给出起止日期或日期列表")
        unknown = [day for day in window if model.wide_row(day) is None]
        if unknown:
            raise model.SeriesModelError(f"宽表中没有这些日期的数据：{'、'.join(unknown)}")
        return window

    def default_backfill_days(self) -> list[str]:
        """保留窗口的末六天：25 至 30 日。"""
        retention = model.wide_rows(*model.WIDE_RETENTION)
        return [str(row["日期"]) for row in retention[-6:]]

    def _batch_tasks(
        self, days: list[str], metrics: list[str],
    ) -> list[tuple[str, str]]:
        return [(day, metric) for day in days for metric in metrics]

    def _ensure_batch(
        self,
        batch_id: str | None,
        days: list[str],
        metrics: list[str],
    ) -> dict[str, Any]:
        batches = store.rows(BATCH_MODULE)
        if batch_id:
            for row in batches:
                if row.get("批次号") == batch_id:
                    if row.get("回填日期") != list(days) or row.get("指标列表") != list(metrics):
                        raise model.SeriesModelError(
                            f"回填批次 {batch_id} 已存在但窗口或指标不同，不能复用同一个批次号")
                    return row
        code = batch_id or f"MIG-{days[-1].replace('-', '')}-{len(batches) + 1:02d}"
        if any(row.get("批次号") == code for row in batches):
            raise model.SeriesModelError(f"回填批次 {code} 已存在且窗口不同，请换一个批次号")
        record = {
            "批次号": code,
            "状态": "待回填",
            "回填日期": list(days),
            "指标列表": list(metrics),
            "来源类型": "宽表存量",
            "来源数据集": model.WIDE_TABLE_NAME,
            "目标数据集": DATASET_NAME,
            "总任务数": len(days) * len(metrics),
            "已写入数": 0,
            "跳过数": 0,
            "断点": 0,
            "尝试次数": 0,
            "中断原因": None,
            "创建时间": self._now(),
            "更新时间": self._now(),
            "完成时间": None,
        }
        batches.append(record)
        return record

    def submit_backfill(
        self,
        *,
        batch_id: str | None = None,
        days: list[str] | None = None,
        metrics: list[str] | None = None,
        interrupt_after: int | None = None,
    ) -> dict[str, Any]:
        """提交回填（或恢复中断批次）。

        ``interrupt_after`` 用于模拟部署中断：写到第 N 条观测后批次转为「中断」，
        再提交同一批次号时从中断点继续。已存在的有效观测一律跳过，
        既保证一项指标一日只有一条有效观测，也不会用宽表旧值覆盖销售更正。
        """
        window = self.resolve_window(None, None, days) if days else self.default_backfill_days()
        metric_list = [str(code) for code in (metrics or model.METRIC_ORDER)]
        unknown = [code for code in metric_list if not model.known_metric(code)]
        if unknown:
            raise model.SeriesModelError(f"指标未定义：{'、'.join(unknown)}")

        batch = self._ensure_batch(batch_id, window, metric_list)
        if batch["状态"] == "已完成":
            return {
                "批次": batch,
                "写入数": 0,
                "跳过数": int(batch.get("总任务数") or 0),
                "消息": f"批次 {batch['批次号']} 已完成，重复提交不会重复写入",
                "是否需要重试": False,
            }
        previous_state = batch["状态"]

        tasks = self._batch_tasks(window, metric_list)
        cursor = int(batch.get("断点") or 0)
        already_written = int(batch.get("已写入数") or 0)
        written_now = 0
        skipped_now = 0
        batch["状态"] = "回填中"
        batch["尝试次数"] = int(batch.get("尝试次数") or 0) + 1
        batch["中断原因"] = None
        batch["更新时间"] = self._now()

        interrupted = False
        for index in range(cursor, len(tasks)):
            day, metric_code = tasks[index]
            if self.effective(metric_code, day) is not None:
                # 幂等：中断重试或重复提交时不再写第二条有效观测。
                skipped_now += 1
                batch["断点"] = index + 1
                batch["跳过数"] = int(batch.get("跳过数") or 0) + 1
                continue

            wide = model.wide_row(day)
            raw = model.raw_from_wide(metric_code, wide)
            observation = model.make_observation(
                metric_code,
                day,
                raw,
                source_type="宽表存量",
                source_version=model.wide_version(day),
                source_dataset=model.WIDE_TABLE_NAME,
                batch=batch["批次号"],
                version=max((int(item.get("版本", 0)) for item in self._history(metric_code, day)),
                            default=0) + 1,
                row_id=self._next_id(),
                note="迁移流水线从宽表回填",
                created_at=self._now(),
            )
            self._partitions().append(observation)
            written_now += 1
            batch["已写入数"] = int(batch.get("已写入数") or 0) + 1
            batch["断点"] = index + 1

            if interrupt_after is not None and written_now >= int(interrupt_after):
                interrupted = True
                batch["状态"] = "中断"
                batch["中断原因"] = (
                    f"模拟部署中断：回填进程写完第 {batch['断点']}/{len(tasks)} 条观测后退出，"
                    f"再次提交批次 {batch['批次号']} 可从中断点继续")
                batch["更新时间"] = self._now()
                break

        if not interrupted:
            batch["状态"] = "已完成"
            batch["完成时间"] = self._now()
        batch["更新时间"] = self._now()

        # 同一批次重复提交时 written_now 为 0，但仍把本次跳过的数量回给调用方。
        code = batch["批次号"]
        if batch["状态"] == "中断":
            message = f"批次 {code} 被部署中断，再次提交同一批次可继续"
        elif written_now == 0 and skipped_now == 0:
            message = f"批次 {code} 已完成，无待补观测"
        elif written_now == 0:
            message = f"批次 {code} 已完成，{skipped_now} 条观测已存在并跳过"
        elif previous_state == "中断":
            message = f"批次 {code} 已从中断恢复并完成，本次写入 {written_now} 条"
        elif already_written:
            message = f"批次 {code} 已完成，本次写入 {written_now} 条"
        else:
            message = f"批次 {code} 已完成，写入 {written_now} 条"
        return {
            "批次": dict(batch),
            "写入数": written_now,
            "跳过数": skipped_now,
            "消息": message,
            "是否需要重试": batch["状态"] == "中断",
        }

    def resume_backfill(self, batch_id: str) -> dict[str, Any]:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise model.SeriesModelError(f"回填批次 {batch_id} 不存在")
        return self.submit_backfill(
            batch_id=batch_id,
            days=batch["回填日期"],
            metrics=batch["指标列表"],
        )

    # ------------------------------------------------------------------
    # 销售更正
    # ------------------------------------------------------------------
    def correct(
        self,
        *,
        metric_code: str,
        day: str,
        value: float | None = None,
        raw: dict[str, Any] | None = None,
        reason: str = "",
        source_version: str | None = None,
    ) -> dict[str, Any]:
        """把某指标某日更正为新值：旧观测置为已替代，新观测成为唯一有效值。"""
        if not model.known_metric(metric_code):
            raise model.SeriesModelError(f"指标 {metric_code} 未定义")
        previous = self.effective(metric_code, day)
        if previous is None:
            raise model.SeriesModelError(
                f"{day} 的 {metric_code} 在新分区里没有有效观测，无法更正；请先回填该分区")

        if raw is None:
            if value is None:
                raise model.SeriesModelError("更正必须给出观测值或原始计数字段")
            raw = model.raw_from_value(metric_code, value)
        else:
            raw = {key: raw[key] for key in raw}
            if metric_code == "METR-CVR" and not {"paid_uv", "total_uv"} <= set(raw):
                raise model.SeriesModelError("转化率更正须同时提供 paid_uv 与 total_uv")
        if value is not None and metric_code != "METR-CVR":
            # 数值字段以原始计数为准，避免两个入口算出不同观测值。
            raw = model.raw_from_value(metric_code, value)

        version = int(previous.get("版本", 1)) + 1
        observation = model.make_observation(
            metric_code,
            day,
            raw,
            source_type="销售更正",
            source_version=source_version or f"{CORRECTION_DATASET}@{day}",
            source_dataset=CORRECTION_DATASET,
            batch=f"CORR-{day.replace('-', '')}-{version:02d}",
            version=version,
            row_id=self._next_id(),
            note=reason or "销售侧更正",
            created_at=self._now(),
        )
        observation["替代自"] = previous.get("id")
        observation["替代为"] = None
        previous["状态"] = "已替代"
        previous["替代为"] = observation["id"]
        self._partitions().append(observation)

        effective_rows = [
            row for row in self._partitions()
            if row.get("指标编号") == metric_code and row.get("日期") == day
            and row.get("状态") == "有效"
        ]
        if len(effective_rows) != 1:
            raise model.SeriesModelError(
                f"{day} 的 {metric_code} 出现 {len(effective_rows)} 条有效观测，违反唯一有效约束")
        return {
            "指标编号": metric_code,
            "日期": day,
            "原观测": previous,
            "新观测": observation,
        }

    # ------------------------------------------------------------------
    # 读取（按指标 + 日期）
    # ------------------------------------------------------------------
    def read_series(
        self,
        metric_code: str,
        start: str,
        end: str,
        mode: str = DEFAULT_SOURCE_MODE,
    ) -> dict[str, Any]:
        """按指标和日期读取时序；切换期按日回退宽表，并逐日标注来源。

        全量启用模式只认新分区：如果窗口开头的日期还没迁移进来，实际观测范围
        会从第一个可用日期开始（训练截止日不变）；但实际范围中间不允许有洞，
        有洞说明迁移没完成，直接报错而不是悄悄跳过一天。
        """
        mode_key = self.validate_mode(mode)
        if not model.known_metric(metric_code):
            raise model.SeriesModelError(f"指标 {metric_code} 未定义")
        days = [str(row["日期"]) for row in model.wide_rows(start, end)]
        if not days:
            raise model.SeriesModelError(f"{start} 至 {end} 没有任何宽表日期")

        points: list[dict[str, Any]] = []
        missing: list[str] = []
        skipped_prefix: list[str] = []
        for day in days:
            observation = None if mode_key == "wide" else self.effective(metric_code, day)
            if observation is not None:
                points.append(self._point_from_observation(observation))
                continue
            if mode_key in ("switch", "wide"):
                wide = model.wide_row(day)
                points.append(self._point_from_wide(metric_code, wide))
                continue
            missing.append(day)
            if not points:
                skipped_prefix.append(day)

        if missing and not points:
            raise model.SeriesCoverageError(
                f"{metric_code} 在 {start} 至 {end} 没有任何分区观测；"
                f"全量启用模式要求至少训练截止日之前的数据已入新分区")
        holes = [day for day in missing if day not in skipped_prefix]
        if holes:
            raise model.SeriesCoverageError(
                f"{metric_code} 在 {holes[0]} 至 {holes[-1]} 缺少 {len(holes)} 天分区观测"
                f"（{'、'.join(holes)}）；实际训练范围内不允许有缺口")

        actual_start = points[0]["日期"]
        return {
            "指标编号": metric_code,
            "指标名称": model.metric_name(metric_code),
            "精度位数": model.metric_precision(metric_code),
            "请求起始日期": start,
            "实际起始日期": actual_start,
            "起始日期": actual_start,
            "截止日期": end,
            "训练截止日": days[-1],
            "少算日期": skipped_prefix,
            "来源模式": mode_key,
            "来源模式名称": SOURCE_MODES[mode_key],
            "点数": len(points),
            "来源汇总": self._source_summary(points),
            "点": points,
        }

    def _point_from_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        return {
            "日期": observation["日期"],
            "观测值": observation["观测值"],
            "原始计数": dict(observation.get("原始计数") or {}),
            "来源类型": observation.get("来源类型"),
            "来源版本": observation.get("来源版本"),
            "来源数据集": observation.get("来源数据集"),
            "分区": observation.get("分区"),
            "观测记录id": observation.get("id"),
            "版本": observation.get("版本"),
            "精度位数": observation.get("精度位数"),
            "备注": observation.get("备注", ""),
        }

    def _point_from_wide(self, metric_code: str, wide: dict[str, Any]) -> dict[str, Any]:
        raw = model.raw_from_wide(metric_code, wide)
        return {
            "日期": str(wide["日期"]),
            "观测值": model.value_from_raw(metric_code, raw),
            "原始计数": raw,
            "来源类型": "宽表存量",
            "来源版本": model.wide_version(str(wide["日期"])),
            "来源数据集": model.WIDE_TABLE_NAME,
            "分区": None,
            "观测记录id": None,
            "版本": None,
            "精度位数": model.metric_precision(metric_code),
            "备注": "该日尚未进入新分区，只能从宽表读取",
        }

    def _source_summary(self, points: list[dict[str, Any]]) -> dict[str, int]:
        summary: dict[str, int] = {}
        for point in points:
            key = str(point.get("来源类型"))
            summary[key] = summary.get(key, 0) + 1
        return summary

    def coverage(self, metric_code: str, start: str, end: str) -> dict[str, Any]:
        """逐日检查新分区覆盖情况，用于展示切换进度。"""
        days = [str(row["日期"]) for row in model.wide_rows(start, end)]
        rows = []
        for day in days:
            observation = self.effective(metric_code, day)
            rows.append({
                "日期": day,
                "分区可用": observation is not None,
                "分区观测值": observation.get("观测值") if observation else None,
                "来源版本": observation.get("来源版本") if observation else None,
                "只能读宽表": observation is None,
            })
        available = sum(1 for row in rows if row["分区可用"])
        return {
            "指标编号": metric_code,
            "起始日期": start,
            "截止日期": end,
            "总天数": len(rows),
            "分区可用天数": available,
            "宽表回退天数": len(rows) - available,
            "是否完成切换": available == len(rows),
            "逐日": rows,
        }

    # ------------------------------------------------------------------
    # 与宽表版本逐日核对
    # ------------------------------------------------------------------
    def reconcile(
        self,
        metric_code: str,
        start: str,
        end: str,
    ) -> dict[str, Any]:
        """把分区里的有效观测与宽表对应版本逐日对比原始计数与精度。"""
        if not model.known_metric(metric_code):
            raise model.SeriesModelError(f"指标 {metric_code} 未定义")
        rows: list[dict[str, Any]] = []
        checked = 0
        for wide in model.wide_rows(start, end):
            day = str(wide["日期"])
            wide_raw = model.raw_from_wide(metric_code, wide)
            wide_value = model.value_from_raw(metric_code, wide_raw)
            observation = self.effective(metric_code, day)
            if observation is None:
                rows.append({
                    "日期": day,
                    "宽表版本": model.wide_version(day),
                    "宽表值": wide_value,
                    "宽表原始计数": wide_raw,
                    "分区观测值": None,
                    "分区原始计数": None,
                    "观测记录id": None,
                    "原始计数一致": None,
                    "观测值差异": None,
                    "精度一致": None,
                    "结论": "未回填",
                })
                continue
            raw_match = observation.get("原始计数") == wide_raw
            precision_match = int(observation.get("精度位数", -1)) == model.metric_precision(metric_code)
            delta = round(float(observation.get("观测值") or 0) - float(wide_value), 6)
            checked += 1
            rows.append({
                "日期": day,
                "宽表版本": model.wide_version(day),
                "宽表值": wide_value,
                "宽表原始计数": wide_raw,
                "分区观测值": observation.get("观测值"),
                "分区原始计数": dict(observation.get("原始计数") or {}),
                "观测记录id": observation.get("id"),
                "原始计数一致": raw_match,
                "观测值差异": delta,
                "精度一致": precision_match,
                "结论": (
                    "一致" if raw_match and precision_match
                    else "已更正（分区与宽表版本不同）" if not raw_match and precision_match
                    else "口径不一致"
                ),
            })
        differs = [row for row in rows if row["结论"].startswith("已更正")]
        pending = [row for row in rows if row["结论"] == "未回填"]
        return {
            "指标编号": metric_code,
            "指标名称": model.metric_name(metric_code),
            "精度位数": model.metric_precision(metric_code),
            "起始日期": start,
            "截止日期": end,
            "宽表版本": model.wide_version(end),
            "总天数": len(rows),
            "已核对天数": checked,
            "更正天数": len(differs),
            "未回填天数": len(pending),
            "逐日": rows,
        }

    # ------------------------------------------------------------------
    # 版本链 / 明细
    # ------------------------------------------------------------------
    def history(self, metric_code: str, day: str) -> list[dict[str, Any]]:
        return [dict(row) for row in sorted(
            self._history(metric_code, day), key=lambda item: int(item.get("版本", 0)))]

    def latest_batch(self) -> dict[str, Any] | None:
        batches = self.list_batches()
        return batches[-1] if batches else None


series_store = SeriesStoreService()
