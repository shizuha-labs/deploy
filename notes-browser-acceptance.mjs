// Final dashboard proof waits for actual authorized totals, not network idleness.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import path from 'node:path'

if (process.env.SHIZUHA_SELFHOST_CI !== '1') throw new Error('isolated CI only')
const install = process.env.SHIZUHA_INSTALLATION_DIR
const require = createRequire(path.join(install, 'sources/notes/frontend/package.json'))
const { chromium, expect } = require('@playwright/test')
const browser = await chromium.launch({ headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(process.env.SHIZUHA_ACCEPTANCE_URL + '/notes/')
  await page.locator('#username').fill('testuser')
  await page.locator('#password').fill(process.env.TEST_USER_PASSWORD)
  await Promise.all([
    page.waitForResponse(response => response.url().includes('/auth/login/') && response.ok()),
    page.locator('button[type="submit"]').click(),
  ])
  await expect(page.getByRole('heading', { name: 'Shizuha Notes' })).toBeVisible()
  await page.getByRole('button', { name: 'All Organizations', exact: true }).click()
  await page.getByRole('button', { name: 'Browser Acceptance', exact: true }).click()
  await expect.poll(() => page.evaluate(() => localStorage.getItem('shizuha_organization_id')))
    .toBe(process.env.TEST_ORGANIZATION_ID)
  const paths = {
    'Total Notes': '/notes/api/notes/?page_size=10',
    Notebooks: '/notes/api/notebooks/',
    Pinned: '/notes/api/search/?q=&pinned=true&page_size=10',
    Favorites: '/notes/api/search/?q=&favorite=true&page_size=10',
  }
  const totals = await page.evaluate(async paths => {
    const result = {}
    for (const [label, path] of Object.entries(paths)) {
      const response = await fetch(path, { headers: {
        Authorization: 'Bearer ' + localStorage.getItem('shizuha_access_token'),
        'X-Organization-ID': localStorage.getItem('shizuha_organization_id'),
      } })
      if (!response.ok) throw new Error(label + ' API failed: ' + response.status)
      result[label] = (await response.json()).count
    }
    return result
  }, paths)
  for (const [label, count] of Object.entries(totals)) {
    assert.ok(Number.isInteger(count) && count >= 0, label + ' must have an authoritative total')
    const card = page.locator('main p').filter({ hasText: new RegExp('^' + label + '$') }).locator('..')
    await expect(card.locator('p').last()).toHaveText(String(count))
  }
  assert.deepEqual(errors, [])
  await page.screenshot({ path: path.join(install, 'browser-evidence/notes.png'), fullPage: true })
  console.log('PASS Notes dashboard renders all four authorized server totals before final screenshot')
} finally {
  await browser.close()
}
