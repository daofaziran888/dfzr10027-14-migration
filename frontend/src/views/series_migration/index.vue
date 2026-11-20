<template>
  <section class="page" data-module="series_migration">
    <header class="page-head">
      <div>
        <h2>时序分区迁移与预测追溯</h2>
        <p class="page-desc">
          宽表 trend_wide_daily 只读保留；新分区按「指标 × 日期」存放，同一指标同一日只有一条有效观测。
          切换期前四天按日回退宽表，末六天读新分区，08-28 的 GMV 已按销售更正为 242000。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="resume-backfill" type="button" @click="resumeBackfill">
          恢复回填批次
        </button>
        <button class="btn primary" data-testid="run-forecast" type="button" @click="runForecast">
          生成十日预测
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value" data-testid="migration-stat">{{ card.value }}</strong>
      </article>
    </div>

    <section class="panel">
      <h3>宽表结账版本（只读，保留 08-21 至 08-30）</h3>
      <table class="data-table">
        <thead>
          <tr><th>日期</th><th>GMV</th><th>UV</th><th>paid_uv</th><th>total_uv</th><th>宽表版本</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in wideRows" :key="String(row['日期'])" :data-testid="`wide-${row['日期']}`">
            <td>{{ row['日期'] }}</td>
            <td>{{ row['gmv'] }}</td>
            <td>{{ row['uv'] }}</td>
            <td>{{ row['paid_uv'] }}</td>
            <td>{{ row['total_uv'] }}</td>
            <td>{{ wideVersion }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>切换进度（按指标逐日）</h3>
      <div class="filter-bar">
        <label class="filter-item">
          <span>指标</span>
          <select v-model="coverageMetric" data-testid="coverage-metric" @change="loadCoverage">
            <option v-for="metric in metrics" :key="metric" :value="metric">{{ metric }}</option>
          </select>
        </label>
        <span class="page-desc" data-testid="coverage-summary">
          新分区可用 {{ coverage?.分区可用天数 ?? '—' }} 天，需回退宽表 {{ coverage?.宽表回退天数 ?? '—' }} 天
        </span>
      </div>
      <table class="data-table">
        <thead>
          <tr><th>日期</th><th>分区可用</th><th>分区观测值</th><th>来源版本</th><th>读取来源</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in coverage?.逐日 ?? []" :key="row['日期']" :data-testid="`coverage-${row['日期']}`">
            <td>{{ row['日期'] }}</td>
            <td>{{ row['分区可用'] ? '是' : '否' }}</td>
            <td>{{ row['分区观测值'] ?? '—' }}</td>
            <td>{{ row['来源版本'] ?? '—' }}</td>
            <td>{{ row['只能读宽表'] ? '宽表' : '新分区' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>回填批次与中断恢复</h3>
      <table class="data-table">
        <thead>
          <tr><th>批次号</th><th>状态</th><th>断点</th><th>已写入</th><th>跳过</th><th>尝试次数</th><th>中断原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="String(batch['批次号'])" :data-testid="`batch-${batch['批次号']}`">
            <td>{{ batch['批次号'] }}</td>
            <td :data-status="String(batch['状态'])">{{ batch['状态'] }}</td>
            <td>{{ batch['断点'] }}/{{ batch['总任务数'] }}</td>
            <td>{{ batch['已写入数'] }}</td>
            <td>{{ batch['跳过数'] }}</td>
            <td>{{ batch['尝试次数'] }}</td>
            <td>{{ batch['中断原因'] ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>逐日核对（新分区 vs 宽表对应版本）</h3>
      <div class="filter-bar">
        <label class="filter-item">
          <span>指标</span>
          <select v-model="reconcileMetric" data-testid="reconcile-metric" @change="loadReconcile">
            <option v-for="metric in metrics" :key="metric" :value="metric">{{ metric }}</option>
          </select>
        </label>
        <span class="page-desc" data-testid="reconcile-summary">
          已核对 {{ reconcile?.已核对天数 ?? '—' }} 天，更正 {{ reconcile?.更正天数 ?? '—' }} 天，
          未回填 {{ reconcile?.未回填天数 ?? '—' }} 天
        </span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>日期</th><th>宽表原始计数</th><th>分区原始计数</th><th>宽表值</th>
            <th>分区观测值</th><th>差异</th><th>精度一致</th><th>结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in reconcile?.逐日 ?? []" :key="row['日期']" :data-testid="`reconcile-${row['日期']}`">
            <td>{{ row['日期'] }}</td>
            <td>{{ formatRaw(row['宽表原始计数']) }}</td>
            <td>{{ formatRaw(row['分区原始计数']) }}</td>
            <td>{{ row['宽表值'] }}</td>
            <td>{{ row['分区观测值'] ?? '—' }}</td>
            <td>{{ row['观测值差异'] ?? '—' }}</td>
            <td>{{ row['精度一致'] === null ? '—' : row['精度一致'] ? '是' : '否' }}</td>
            <td :data-conclusion="String(row['结论'])">{{ row['结论'] }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>预测产物（可下钻训练明细，切换后旧版本仍可复查）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>报表编号</th><th>指标</th><th>来源模式</th><th>训练截止日</th>
            <th>训练点数</th><th>训练区间合计</th><th>十日预测合计</th>
            <th>反映销售更正</th><th>可复核结账版</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="report in reports" :key="report.id" :data-testid="`report-${report.id}`">
            <td>{{ report['报表编号'] }}</td>
            <td>{{ report['预测指标'] }}</td>
            <td>{{ report['来源模式名称'] }}</td>
            <td>{{ report['训练截止日'] }}</td>
            <td>{{ report['训练点数'] }}</td>
            <td>{{ report['训练区间合计'] }}</td>
            <td>{{ report['预测合计'] }}</td>
            <td>{{ report['已反映销售更正'] ? '是' : '否' }}</td>
            <td>{{ report['可用于复核结账版'] ? '是' : '否' }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`open-report-${report.id}`" @click="openReport(report.id)">
                打开训练明细
              </button>
            </td>
          </tr>
          <tr v-if="!reports.length">
            <td colspan="10" class="empty-state">还没有预测产物，点击「生成十日预测」</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="detail" class="modal-backdrop" data-testid="report-detail">
      <div class="modal report-modal">
        <h3>{{ detail['报表编号'] }} · 训练明细</h3>
        <dl class="detail-grid">
          <div><dt>来源模式</dt><dd>{{ detail['来源模式名称'] }}</dd></div>
          <div><dt>训练窗口</dt><dd>{{ detail['训练窗口']['起'] }} 至 {{ detail['训练窗口']['止'] }}</dd></div>
          <div><dt>实际训练范围</dt><dd>{{ detail['实际训练范围']['起'] }} 至 {{ detail['实际训练范围']['止'] }}</dd></div>
          <div><dt>训练截止日</dt><dd data-testid="detail-cutoff">{{ detail['训练截止日'] }}</dd></div>
          <div><dt>训练来源</dt><dd data-testid="detail-sources">{{ formatRaw(detail['训练来源']) }}</dd></div>
          <div><dt>训练区间合计</dt><dd data-testid="detail-actual-total">{{ detail['训练区间合计'] }}</dd></div>
          <div><dt>十日预测合计</dt><dd data-testid="detail-forecast-total">{{ detail['预测合计'] }}</dd></div>
          <div><dt>已校正日期</dt><dd>{{ (detail['已校正日期'] || []).join('、') || '无' }}</dd></div>
        </dl>

        <h4>参与训练的逐日观测</h4>
        <table class="data-table">
          <thead>
            <tr><th>日期</th><th>观测值</th><th>原始计数</th><th>来源类型</th><th>来源版本</th><th>分区</th></tr>
          </thead>
          <tbody>
            <tr v-for="point in detail['训练明细']" :key="point['日期']" :data-testid="`training-${point['日期']}`">
              <td>{{ point['日期'] }}</td>
              <td>{{ point['观测值'] }}</td>
              <td>{{ formatRaw(point['原始计数']) }}</td>
              <td>{{ point['来源类型'] }}</td>
              <td>{{ point['来源版本'] }}</td>
              <td>{{ point['分区'] ?? '宽表' }}</td>
            </tr>
          </tbody>
        </table>

        <h4>十日预测明细</h4>
        <table class="data-table">
          <thead><tr><th>日期</th><th>预测值</th><th>置信下界</th><th>置信上界</th></tr></thead>
          <tbody>
            <tr v-for="row in detail['预测']" :key="row['日期']">
              <td>{{ row['日期'] }}</td>
              <td>{{ row['预测值'] }}</td>
              <td>{{ row['置信下界'] }}</td>
              <td>{{ row['置信上界'] }}</td>
            </tr>
          </tbody>
        </table>

        <h4>按模式复算</h4>
        <div class="page-actions">
          <button
            v-for="mode in modes"
            :key="mode.代码"
            class="btn"
            type="button"
            :data-testid="`recompute-${mode.代码}`"
            @click="recompute(mode.代码)"
          >
            按{{ mode.名称 }}复算
          </button>
        </div>
        <div v-if="recomputed" class="recompute-box" data-testid="recompute-result">
          <p v-if="recomputed.error" class="error-text">{{ recomputed.error }}</p>
          <template v-else>
            <p>
              复算模式：{{ recomputed['复算模式名称'] }} ·
              训练截止日 {{ recomputed['训练截止日'] }} ·
              训练区间合计 {{ recomputed['训练区间合计'] }}（与原产物
              <strong>{{ recomputed['合计一致'] ? '一致' : '不一致' }}</strong>）·
              十日预测合计 {{ recomputed['预测合计'] }}（预测值
              <strong>{{ recomputed['预测一致'] ? '一致' : '不一致' }}</strong>）
            </p>
            <p class="page-desc">
              原产物保留：训练合计 {{ recomputed['原产物']['训练区间合计'] }}，
              预测合计 {{ recomputed['原产物']['预测合计'] }}，
              来源 {{ formatRaw(recomputed['原产物']['训练来源']) }}。
            </p>
          </template>
        </div>

        <div class="page-actions modal-foot">
          <button class="btn" type="button" data-testid="close-report" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span v-if="message" data-testid="page-message">{{ message }}</span>
      <span v-else>最近一次操作结果会显示在这里</span>
      <span v-if="errorMessage" class="error-text" data-testid="page-error">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Raw = Record<string, number>
type WideRow = { 日期: string; gmv: number; uv: number; paid_uv: number; total_uv: number }
type CoverageDay = { 日期: string; 分区可用: boolean; 分区观测值: number | null; 来源版本: string | null; 只能读宽表: boolean }
type Coverage = { 分区可用天数: number; 宽表回退天数: number; 是否完成切换: boolean; 逐日: CoverageDay[] }
type ReconcileDay = {
  日期: string
  宽表版本: string
  宽表值: number
  宽表原始计数: Raw
  分区观测值: number | null
  分区原始计数: Raw | null
  观测值差异: number | null
  精度一致: boolean | null
  结论: string
}
type Reconcile = { 已核对天数: number; 更正天数: number; 未回填天数: number; 逐日: ReconcileDay[] }
type Batch = {
  批次号: string
  状态: string
  断点: number
  总任务数: number
  已写入数: number
  跳过数: number
  尝试次数: number
  中断原因: string | null
  回填日期: string[]
  指标列表: string[]
}
type ReportSummary = {
  id: number
  报表编号: string
  预测指标: string
  来源模式名称: string
  训练截止日: string
  训练点数: number
  训练区间合计: number
  预测合计: number
  已反映销售更正: boolean
  可用于复核结账版: boolean
}
type ReportDetail = ReportSummary & {
  训练窗口: { 起: string; 止: string }
  实际训练范围: { 起: string; 止: string }
  训练来源: Raw
  训练明细: { 日期: string; 观测值: number; 原始计数: Raw; 来源类型: string; 来源版本: string; 分区: string | null }[]
  预测: { 日期: string; 预测值: number; 置信下界: number; 置信上界: number }[]
  已校正日期: string[]
}
type Mode = { 代码: string; 名称: string }

const metrics = ['METR-GMV', 'METR-UV', 'METR-CVR']
const wideRows = ref<WideRow[]>([])
const wideVersion = ref('')
const coverageMetric = ref('METR-GMV')
const coverage = ref<Coverage | null>(null)
const reconcileMetric = ref('METR-GMV')
const reconcile = ref<Reconcile | null>(null)
const batches = ref<Batch[]>([])
const reports = ref<ReportSummary[]>([])
const modes = ref<Mode[]>([])
const detail = ref<ReportDetail | null>(null)
const recomputed = ref<Record<string, any> | null>(null)
const message = ref('')
const errorMessage = ref('')

const cards = computed(() => [
  { label: '分区可用天数', value: coverage.value?.分区可用天数 ?? 0 },
  { label: '宽表回退天数', value: coverage.value?.宽表回退天数 ?? 0 },
  { label: '中断批次', value: batches.value.filter((row) => row.状态 === '中断').length },
  { label: '预测产物', value: reports.value.length },
])

function formatRaw(raw: Raw | null | undefined): string {
  if (!raw || !Object.keys(raw).length) return '—'
  return Object.entries(raw).map(([key, value]) => `${key}=${value}`).join('，')
}

async function loadWide() {
  const payload = await fetchJson<{ 版本: string; 逐日: WideRow[] }>('/api/series/wide')
  wideRows.value = payload.逐日
  wideVersion.value = payload.版本
}

async function loadCoverage() {
  coverage.value = await fetchJson<Coverage>(
    `/api/series/coverage?metric_code=${coverageMetric.value}`)
}

async function loadReconcile() {
  reconcile.value = await fetchJson<Reconcile>(
    `/api/series/reconcile?metric_code=${reconcileMetric.value}`)
}

async function loadBatches() {
  const payload = await fetchJson<{ items: Batch[] }>('/api/series/batches')
  batches.value = payload.items
}

async function loadReports() {
  const payload = await fetchJson<{ items: ReportSummary[] }>('/api/forecast_store/reports')
  reports.value = payload.items
}

async function loadModes() {
  const payload = await fetchJson<{ items: Mode[] }>('/api/series/modes')
  modes.value = payload.items
}

async function reloadAll() {
  await Promise.all([loadWide(), loadCoverage(), loadReconcile(), loadBatches(), loadReports()])
}

async function resumeBackfill() {
  errorMessage.value = ''
  message.value = ''
  const interrupted = batches.value.find((row) => row.状态 === '中断') ?? batches.value[0]
  if (!interrupted) {
    errorMessage.value = '没有可恢复的回填批次'
    return
  }
  try {
    const response = await request('/api/series/backfill', {
      method: 'POST',
      body: JSON.stringify({
        batch_id: interrupted.批次号,
        days: interrupted.回填日期,
        metrics: interrupted.指标列表,
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '恢复回填失败')
    message.value = `${payload.message}（写入 ${payload.entry['写入数']}，跳过 ${payload.entry['跳过数']}）`
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复回填失败'
  }
}

async function runForecast() {
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request('/api/forecast_store/run?metric_code=METR-GMV', { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '预测失败')
    message.value = payload.message
    await loadReports()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预测失败'
  }
}

async function openReport(id: number) {
  recomputed.value = null
  detail.value = await fetchJson<ReportDetail>(`/api/forecast_store/reports/${id}`)
}

async function recompute(mode: string) {
  if (!detail.value) return
  recomputed.value = await fetchJson<Record<string, any>>(
    `/api/forecast_store/reports/${detail.value.id}/recompute?mode=${mode}`)
}

onMounted(async () => {
  try {
    await loadModes()
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移数据读取失败'
  }
})
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; }
.panel h3 { font-size: 14px; margin: 0 0 10px; }
.panel h4 { font-size: 13px; margin: 14px 0 8px; }
.report-modal { width: min(980px, 100%); }
.detail-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 16px; margin: 0 0 12px; }
.detail-grid dt { font-size: 12px; color: var(--muted); }
.detail-grid dd { margin: 2px 0 0; font-size: 13px; }
.recompute-box { background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin-top: 10px; font-size: 13px; }
.modal-foot { margin-top: 14px; justify-content: flex-end; }
</style>
