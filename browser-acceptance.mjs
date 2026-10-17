// Real browser contract for the shared editor after source dependency updates.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { isPendingHiveIndex } from './browser-response-contract.mjs'

if (process.env.SHIZUHA_SELFHOST_CI !== '1') throw new Error('isolated CI only')
const install = process.env.SHIZUHA_INSTALLATION_DIR
const require = createRequire(path.join(install, 'sources/notes/frontend/package.json'))
const { chromium, expect } = require('@playwright/test')
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
const errors = []
const credentialLeaks = []
context.on('request', request => {
  if (new URL(request.url()).origin !== new URL(process.env.SHIZUHA_ACCEPTANCE_URL).origin
      && request.headers()['authorization']) credentialLeaks.push(new URL(request.url()).origin)
})
page.on('pageerror', error => errors.push(error.message))
const base = process.env.SHIZUHA_ACCEPTANCE_URL
const output = path.join(install, 'browser-evidence')
await mkdir(output, { recursive: true })
try {
  await page.goto(base + '/')
  await expect(page.getByRole('link', { name: 'Sign in', exact: true }).first()).toBeVisible()
  await expect(page.getByTestId('home-compose')).toHaveCount(0)
  assert.deepEqual(errors, [], 'anonymous Home must not crash')
  await page.screenshot({ path: path.join(output, 'home-anonymous.png'), fullPage: true })
  await page.goto(`${base}/wiki/`)
  await expect(page.locator('#username')).toBeVisible()
  assert.deepEqual(errors, [], 'anonymous Wiki/login must not crash')
  await page.locator('#username').fill('ossowner')
  await page.locator('#password').fill(process.env.SHIZUHA_BROWSER_PASSWORD)
  await Promise.all([
    page.waitForResponse(response => response.url().includes('/auth/login/') && response.ok()),
    page.locator('button[type="submit"]').click(),
  ])
  await page.waitForURL(url => url.pathname.startsWith('/wiki/'))
  const edit = `${base}/wiki/spaces/OSS/pages/${process.env.SHIZUHA_WIKI_PAGE_SLUG}/edit?organization=${process.env.SHIZUHA_WIKI_ORG}`
  await page.goto(edit)
  await expect(page.getByPlaceholder('Page title')).toHaveValue('Persistent acceptance page')
  const editor = page.locator('.tiptap[contenteditable="true"]')
  await expect(editor).toBeVisible()
  const content = 'Browser edited shared editor content survives save and reload.'
  await editor.fill(content)
  await Promise.all([
    page.waitForResponse(response => /\/wiki\/api\/pages\/[^/]+\/$/.test(new URL(response.url()).pathname)
      && ['PATCH', 'PUT'].includes(response.request().method()) && response.ok()),
    page.getByRole('button', { name: 'Save', exact: true }).click(),
  ])
  await page.waitForURL(url => !url.pathname.endsWith('/edit'))
  await expect(page.getByText(content, { exact: true }).first()).toBeVisible()
  await page.reload()
  await expect(page.getByText(content, { exact: true }).first()).toBeVisible()
  assert.deepEqual(errors, [], 'Wiki editor must not raise browser exceptions')
  await page.screenshot({ path: path.join(output, 'wiki-editor-saved.png'), fullPage: true })
  console.log('PASS Wiki anonymous login, shared editor typing/save and persisted browser reload')
  // Reuse the real login session. Each application must render its own content
  // and make a successful authenticated request to this installation.
  const applications = [
    { name: 'home', route: '/', testId: 'home-compose', api: /^\/api\/home\/summary/ },
    { name: 'id', route: '/id/account', heading: 'Account Settings', api: /^\/id\/api\/auth\/profile\/$/ },
    { name: 'admin', route: '/admin/', heading: 'Dashboard', api: /^\/admin\/api\/organizations\// },
    { name: 'hive', route: '/hive/agents', heading: 'Agents', api: /^\/hive\/api\/v1\/fleet\/(?:agent-cards|agents)\// },
    { name: 'pulse', route: '/pulse/projects', content: 'Acceptance project', api: /^\/pulse\/api\/projects\// },
    { name: 'connect', route: '/connect/messages', heading: 'Inbox', content: 'Acceptance conversation', api: /^\/connect\/api\/conversations\// },
    { name: 'atlas', route: '/atlas/', content: 'Acceptance map', api: /^\/atlas\/api\/maps/ },
    { name: 'drive', route: '/drive/my-drive', heading: 'My Drive', content: 'acceptance.txt', api: /^\/drive\/api\/files\// },
    { name: 'mail', route: '/mail/inbox', heading: 'Inbox', api: /^\/mail\/api\/messages\// },
  ]
  const evidence = []
  for (const application of applications) {
    const requests = []
    const responseChecks = []
    const collect = response => {
      const url = new URL(response.url())
      if (url.origin === new URL(base).origin && application.api.test(url.pathname)) {
        const request = { path: url.pathname, status: response.status(), method: response.request().method() }
        requests.push(request)
        if (application.name === 'hive' && request.status === 503) {
          responseChecks.push(response.json().then(body => {
            request.pendingIndex = isPendingHiveIndex(application.name, request, response.headers(), body)
          }).catch(() => { request.pendingIndex = false }))
        }
      }
    }
    page.on('response', collect)
    try {
      await page.goto(base + application.route)
      if (application.testId) await expect(page.getByTestId(application.testId)).toBeVisible()
      if (application.heading) {
        await expect(page.getByRole('heading', { name: application.heading, exact: true }).first()).toBeVisible()
      }
      if (application.content) await expect(page.getByText(application.content, { exact: true }).first()).toBeVisible()
      await expect.poll(() => requests.some(request => request.method === 'GET' && request.status === 200),
        { message: application.name + ' must load authenticated application data' }).toBe(true)
      if (application.name === 'mail') await expect(page.locator('main .animate-spin')).toHaveCount(0)
      assert.equal(new URL(page.url()).origin, new URL(base).origin)
      assert.ok(application.name === 'home' ? new URL(page.url()).pathname === '/'
        : new URL(page.url()).pathname.startsWith('/' + application.name + '/'), application.name + ' remained in application')
      assert.deepEqual(errors, [], application.name + ' must not raise browser exceptions')
      assert.deepEqual(credentialLeaks, [], 'installation credentials must remain on the installation origin')
      await Promise.all(responseChecks)
      assert.ok(!requests.some(request => request.status >= 500 && !request.pendingIndex), application.name + ' API must not fail')
      await page.screenshot({ path: path.join(output, application.name + '-authenticated.png'), fullPage: true })
      evidence.push({ application: application.name, route: application.route, requests })
      console.log('PASS ' + application.name + ' authenticated browser render and API')
    } catch (error) {
      console.error(JSON.stringify({ application: application.name, requests, browserErrors: errors, failure: error.message }))
      await page.screenshot({ path: path.join(output, application.name + '-failed.png'), fullPage: true }).catch(() => {})
      throw error
    } finally {
      page.off('response', collect)
      await writeFile(path.join(output, 'applications.json'), JSON.stringify(evidence, null, 2), { mode: 0o600 })
    }
  }
  const deniedContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  try {
    const denied = await deniedContext.newPage()
    const responses = []
    denied.on('response', response => {
      const url = new URL(response.url())
      if (url.origin === new URL(base).origin && ['/admin/api/me/', '/admin/api/organizations/'].includes(url.pathname)) {
        responses.push({ path: url.pathname, status: response.status() })
      }
    })
    await denied.goto(`${base}/id/login?continue=/admin/`)
    await denied.locator('#username').fill('ossoutsider')
    await denied.locator('#password').fill(process.env.SHIZUHA_BROWSER_PASSWORD)
    await denied.locator('button[type="submit"]').click()
    await expect(denied.getByText('Admin access required', { exact: true })).toBeVisible()
    await expect(denied.getByText('No Organizations', { exact: true })).toHaveCount(0)
    await denied.getByRole('button', { name: "ossoutsider's Workspace", exact: true }).click()
    await expect(denied.getByRole('button', { name: 'Other Organization', exact: true })).toBeVisible()
    await expect.poll(() => responses.some(item => item.path === '/admin/api/me/' && item.status === 403)
      && responses.some(item => item.path === '/admin/api/organizations/' && item.status === 200)).toBe(true)
    await denied.screenshot({ path: path.join(output, 'admin-access-denied.png'), fullPage: true })
    console.log('PASS Admin real denied entitlement retains organization context and truthful error')
  } finally {
    await deniedContext.close()
  }
} finally {
  await page.screenshot({ path: path.join(output, 'wiki-final.png'), fullPage: true }).catch(() => {})
  await browser.close()
}
