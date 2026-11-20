import { expect, test } from '@playwright/test'

// 需要后端处于种子初始状态（批次 MIG-20260830-01 中断、尚无预测产物）。
// 建议每次跑之前重启一次后端进程，内存存储会回到种子数据。
test('分区迁移页面：回填恢复、核对、下钻训练明细与复算', async ({ page }) => {
  test.setTimeout(120_000)
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`) })

  await page.goto('/series_migration')
  await expect(page.getByTestId('coverage-summary')).toContainText('新分区可用 6 天')
  await expect(page.getByTestId('reconcile-2026-08-28')).toContainText('已更正')
  await expect(page.getByTestId('batch-MIG-20260830-01')).toContainText('中断')

  await page.getByTestId('resume-backfill').click()
  await expect(page.getByTestId('page-message')).toContainText('批次 MIG-20260830-01 已完成')
  await expect(page.getByTestId('batch-MIG-20260830-01')).toContainText('已完成')

  await page.getByTestId('run-forecast').click()
  const newest = page.locator('[data-testid^="report-"]').last()
  await expect(newest).toContainText('切换期')
  const reportId = await newest.getAttribute('data-testid')
  await newest.getByRole('button', { name: '打开训练明细' }).click()
  await expect(page.getByTestId('report-detail')).toBeVisible()
  await expect(page.getByTestId('detail-cutoff')).toHaveText('2026-08-30')
  await expect(page.getByTestId('detail-sources')).toContainText('销售更正')
  await expect(page.getByTestId('training-2026-08-28')).toContainText('242000')
  await expect(page.getByTestId('training-2026-08-21')).toContainText('宽表')

  await page.getByTestId('recompute-switch').click()
  await expect(page.getByTestId('recompute-result')).toContainText('一致')
  await page.getByTestId('recompute-partition').click()
  await expect(page.getByTestId('recompute-result')).toContainText('不一致')
  await expect(page.getByTestId('recompute-result')).toContainText('原产物保留')

  await page.getByTestId('close-report').click()
  await expect(page.getByTestId('report-detail')).toHaveCount(0)
  expect(reportId).toMatch(/^report-\d+$/)
  expect(errors).toEqual([])
})
