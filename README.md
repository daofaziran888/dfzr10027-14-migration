# 分析工坊 · 数据分析与数据应用平台

面向指标口径管理、多维度聚合查询、用户分群圈选、AB实验评估、时序趋势分析与数据服务发布的数据分析与数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 业务指标 | 指标编号、指标名称、计算公式 |
| 维度注册 | `dim_registry` | 分析维度 | 维度编号、维度名称、业务键 |
| 聚合任务 | `agg_task` | 聚合计算 | 任务编号、目标指标、聚合维度 |
| 多维立方体 | `cube_builder` | 数据立方体 | 立方体编号、事实来源、维度列表 |
| 分群规则 | `cohort_rule` | 用户分群 | 分群编号、分群名称、筛选条件 |
| 实验管理 | `experiment` | AB实验 | 实验编号、实验假设、分组方案 |
| 时序分析 | `trend_series` | 时序序列 | 序列编号、观测指标、时间粒度 |
| 归因模型 | `attribution` | 归因链路 | 链路编号、归因窗口、触点类型 |
| 数据分层 | `segmentation` | 数据切片 | 切片编号、源数据集、切分维度 |
| 报表生成 | `report_gen` | 分析报表 | 报表编号、报表类型、数据范围 |
| 预测分析 | `forecast` | 预测方案 | 方案编号、预测指标、预测算法 |
| 数据集市 | `data_mart` | 分析主题 | 主题编号、主题名称、业务域 |
| 统计建模 | `stat_model` | 统计模型 | 模型编号、模型类型、因变量 |
| 数据提取 | `data_extract` | 提取任务 | 任务编号、来源数据集、筛选条件 |
| 质量监控 | `quality_mon` | 监控规则 | 规则编号、监控对象、校验类型 |
| 对比分析 | `compare_job` | 对比任务 | 任务编号、基准数据、对比数据 |
| 数据服务 | `api_service` | 服务接口 | 接口编号、查询数据集、请求参数 |
| 调度编排 | `schedule_plan` | 调度计划 | 计划编号、关联任务、Cron表达式 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 时序分区迁移

日粒度时序原来只有一张宽表 `trend_wide_daily`（一天一行，GMV / UV / 转化率原始计数同列）。
迁移把它拆成按「指标 × 日期」读取的分区数据集 `trend_series_partitioned`（分区键 `dt=YYYY-MM-DD`）：

| 位置 | 作用 |
| --- | --- |
| `backend/app/services/partition_model.py` | 口径层：指标取值、精度、原始计数、CVR 分子分母同源校验 |
| `backend/app/services/series_store.py` | 回填 / 更正 / 按指标日期读取 / 与宽表逐日核对 / 版本链 |
| `backend/app/services/forecast_training.py` | 预测训练入口、报表产物、按模式复算 |
| `backend/app/routers/series.py` | `/api/series/*` |
| `backend/app/routers/forecast_store.py` | `/api/forecast_store/*` |
| `frontend/src/views/series_migration/` | 「分区迁移」页面 |

关键规则：

- **宽表只读保留**：08-21 至 08-30 的宽表不会被迁移删除，结账时点的值（含 08-28 的 232000）随时可回查。
- **唯一有效观测**：同一指标同一日只有一条 `状态=有效`；更正不覆盖历史，旧值转 `已替代` 并
  用 `替代自` / `替代为` 串成版本链。
- **原始计数可核对**：分区里保存回填时的原始计数，`/api/series/reconcile` 逐日与宽表对应版本
  对比原始计数与精度，能明确区分「一致」「已更正」「未回填」。
- **转化率同源**：CVR 的 `paid_uv` / `total_uv` 必须来自同一来源版本，记录在 `分量来源版本` 里。
- **回填幂等**：重复提交同一批次只跳过、不重复写入；`interrupt_after` 可模拟部署中断，再提交同一
  批次号从中断点继续。

### 来源模式

预测训练入口声明指标、训练窗口和来源模式，实际观测范围由分区可用日期决定：

| 模式 | 代码 | 读取行为 |
| --- | --- | --- |
| 宽表 | `wide` | 全部按宽表结账版本训练，可复核结账版 |
| 切换期 | `switch`（默认） | 有分区读分区，没有的日期按日回退宽表，逐日标注来源 |
| 全量启用 | `partition` | 只认新分区；窗口开头的未迁移日期会被跳过（记入 `少算日期`），中间有洞直接报错 |

周报场景：训练窗口 08-21 至 08-30、预测十天。切换期里前四天回退宽表，末六天读分区，
08-28 的 GMV 已按销售更正为 242000，因此该次预测**已反映销售更正、不能用来复核结账版**；
要看结账版请用 `wide` 模式复算。每次生成都追加一条报表产物，训练截止日、逐日来源版本
和算法参数都写在产物里，切换存储后旧产物仍可按报表编号复查。

常用接口：

```bash
# 查看切换进度与逐日核对
curl 'http://127.0.0.1:8000/api/series/coverage?metric_code=METR-GMV'
curl 'http://127.0.0.1:8000/api/series/reconcile?metric_code=METR-GMV'

# 恢复中断的回填批次
curl -X POST http://127.0.0.1:8000/api/series/backfill \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"MIG-20260830-01","days":["2026-08-25","2026-08-30"],"metrics":["METR-GMV"]}'

# 生成本次周报的十天预测
curl -X POST 'http://127.0.0.1:8000/api/forecast_store/run?metric_code=METR-GMV'

# 从产物复算，对照切换期与全量启用两种口径
curl 'http://127.0.0.1:8000/api/forecast_store/reports/1/recompute?mode=switch'
curl 'http://127.0.0.1:8000/api/forecast_store/reports/1/recompute?mode=partition'
```

## 测试

```bash
cd backend && .venv/bin/python -m pytest -q
cd frontend && npm run typecheck
```

`frontend/tests/series_migration.spec.ts` 需要后端处于种子初始状态（中断批次尚未恢复、
还没有预测产物），跑之前重启一次后端进程即可。

## 起源信息

领域：数据工程
子方向：数据分析与数据应用
