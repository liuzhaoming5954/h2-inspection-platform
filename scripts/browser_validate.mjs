// Real browser verification against the locally running validation stack.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.join(root, 'artifacts', 'validation')
await fs.mkdir(out, { recursive: true })
const modules = process.env.H2_NODE_MODULES
const playwright = modules
  ? createRequire(import.meta.url)(path.resolve(modules, 'playwright'))
  : createRequire(path.join(root, 'frontend', 'package.json'))('playwright')
const browser = await playwright.chromium.launch({
  executablePath: process.env.H2_CHROME_PATH ?? '/usr/bin/google-chrome',
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--enable-unsafe-swiftshader', '--use-angle=swiftshader'],
})
const context = await browser.newContext({ viewport: { width: 1600, height: 1100 }, locale: 'zh-CN' })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
try {
  await page.goto(process.env.H2_FRONTEND_URL ?? 'http://127.0.0.1:5173', { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: '巡检技术验证台', exact: true }).waitFor()
  await page.getByText('模拟机器人 · 合成数据', { exact: true }).waitFor()
  await page.locator('[data-testid=start-task]:not([disabled])').waitFor({ timeout: 15000 })
  await page.locator('#task-name').fill('浏览器联调：三点巡检')
  const createdPromise = page.waitForResponse(response => response.url().endsWith('/api/tasks') && response.request().method() === 'POST')
  await page.locator('[data-testid=start-task]').click()
  const created = await (await createdPromise).json()
  assert.ok(created.id)
  await page.waitForFunction(async taskId => {
    const response = await fetch(`/api/tasks/${taskId}`)
    const task = await response.json()
    return task.status === 'completed' && task.samples.length === 3
  }, created.id, { timeout: 30000 })
  await page.locator('.data-table tbody tr').nth(2).waitFor({ timeout: 10000 })
  await page.getByRole('button', { name: '人工复核', exact: true }).click()
  await page.getByRole('textbox', { name: '复核意见' }).fill('浏览器验证：三处采样和照片已核对，数据均为合成。')
  await page.getByRole('button', { name: '保存复核', exact: true }).click()
  await page.locator('.review-record').getByText('浏览器验证：三处采样和照片已核对，数据均为合成。').waitFor()
  await page.locator('[data-testid=generate-reports]').click()
  await page.locator('.download-link').filter({ hasText: 'PDF' }).waitFor({ timeout: 60000 })
  assert.equal(await page.locator('.download-link').count(), 3)
  assert.equal(await page.locator('.data-table tbody tr').count(), 3)
  assert.ok(await page.locator('canvas').count() >= 2, 'Three.js and chart canvases should exist')
  await page.screenshot({ path: path.join(out, 'dashboard.png'), fullPage: true })
  await page.reload({ waitUntil: 'networkidle' })
  await page.getByText('浏览器验证：三处采样和照片已核对，数据均为合成。', { exact: true }).waitFor()
  assert.equal(await page.locator('.data-table tbody tr').count(), 3, 'Reload should recover persisted samples')
  assert.deepEqual(errors, [], 'No uncaught browser errors')
  const result = { passed: true, mode: 'real browser against simulator-backed local services', task_id: created.id,
    checks: { page_loaded: true, task_submitted: true, three_samples_shown: true, review_saved: true,
      report_links_shown: true, webgl_and_chart_canvases_present: true, reload_retains_data: true },
    page_errors: errors, screenshot: 'dashboard.png' }
  await fs.writeFile(path.join(out, 'browser-result.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
} finally {
  await context.close()
  await browser.close()
}
