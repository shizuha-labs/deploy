// Exercise the shipped editor, offline bundle, agent CLI and Wiki host boundary.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { execFileSync, spawnSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import path from 'node:path'

if (process.env.SHIZUHA_SELFHOST_CI !== '1') throw new Error('isolated CI only')
const install = process.env.SHIZUHA_INSTALLATION_DIR
const base = process.env.SHIZUHA_ACCEPTANCE_URL
const output = path.join(install, 'browser-evidence')
await mkdir(output, { recursive: true })
const work = await mkdtemp(path.join(install, 'atlas-acceptance-'))
const require = createRequire(path.join(install, 'sources/notes/frontend/package.json'))
const { chromium, expect } = require('@playwright/test')
// This exact archive has already passed the release archive/path/link gate.
execFileSync('tar', ['-xzf', path.join(install, 'sources/atlas/frontend/vendor/atlas.tgz'), '-C', work])
const library = path.join(work, 'package')
const core = await import(pathToFileURL(path.join(library, 'dist/core.mjs')))
const portable = await import(pathToFileURL(path.join(library, 'dist/portable.mjs')))
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 1500, height: 1080 }, acceptDownloads: true })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
page.on('dialog', dialog => dialog.accept())
async function click(locator) {
  await expect(locator).toBeVisible()
  await locator.scrollIntoViewIfNeeded()
  const box = await locator.boundingBox()
  assert.ok(box)
  await page.mouse.move(box.x + box.width * .55, box.y + box.height * .5, { steps: 9 })
  await page.waitForTimeout(90 + Math.random() * 90)
  await page.mouse.click(box.x + box.width * .55, box.y + box.height * .5)
}
async function type(locator, value) {
  await click(locator)
  await page.keyboard.press('Control+A')
  await page.keyboard.type(value, { delay: 24 })
}
async function choose(locator, label) {
  const options = await locator.locator('option').allTextContents()
  const index = options.indexOf(label)
  assert.ok(index >= 0, 'Expected visible option ' + label)
  await click(locator)
  await page.keyboard.press('Home')
  for (let i = 0; i < index; i++) await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')
}
async function exported(name) {
  const pending = page.waitForEvent('download')
  await click(page.getByRole('button', { name: 'Export Atlas document', exact: true }))
  const download = await pending
  const file = path.join(work, name + '.json')
  await download.saveAs(file)
  return { file, document: core.parseAtlasDocument(await readFile(file, 'utf8')) }
}
try {
  await page.goto(base + '/id/login?continue=/atlas/')
  await type(page.locator('#username'), 'ossowner')
  await type(page.locator('#password'), process.env.SHIZUHA_BROWSER_PASSWORD)
  await click(page.locator('button[type="submit"]'))
  await page.waitForURL(url => url.pathname === '/atlas/')
  await page.goto(base + '/atlas/editor')
  const palette = page.getByRole('complementary', { name: 'Component palette' })
  await expect(palette).toBeVisible()
  await type(page.getByRole('textbox', { name: 'Map title', exact: true }), 'Portable acceptance design')
  async function component(kind, label) {
    await click(palette.getByRole('button', { name: kind, exact: true }))
    await type(page.getByRole('textbox', { name: 'Label', exact: true }), label)
    await click(page.getByRole('button', { name: 'Apply changes', exact: true }))
  }
  await component('service', 'Acceptance API')
  await component('service', 'Acceptance worker')
  await click(page.getByRole('button', { name: 'Explore inside', exact: true }))
  await component('component', 'Nested processor')
  await click(page.getByRole('navigation', { name: 'Component hierarchy' }).getByRole('button').first())
  await click(palette.getByRole('button', { name: 'Relationship', exact: true }))
  await choose(page.getByRole('combobox', { name: 'From', exact: true }), 'Acceptance API')
  await choose(page.getByRole('combobox', { name: 'To', exact: true }), 'Acceptance worker')
  await click(page.getByRole('button', { name: 'Add relationship', exact: true }))
  const connected = await exported('connected')
  assert.equal(connected.document.model.edges.length, 1)
  const worker = connected.document.model.nodes.find(node => node.label === 'Acceptance worker')
  assert.equal(connected.document.model.nodes.find(node => node.label === 'Nested processor').parent_id, worker.id)
  await click(page.getByRole('button', { name: 'Undo', exact: true }))
  assert.equal((await exported('undone')).document.model.edges.length, 0)
  await click(page.getByRole('button', { name: 'Redo', exact: true }))
  const redone = await exported('redone')
  assert.equal(redone.document.model.edges.length, 1)
  const savedResponse = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/atlas/api/maps')
  await click(page.locator('header.bar').getByRole('button', { name: 'Save', exact: true }))
  const saved = await savedResponse
  assert.ok(saved.ok(), 'Atlas save must succeed')
  await page.waitForURL(url => /^\/atlas\/editor\/.+/.test(url.pathname))
  await page.reload()
  const reopened = await exported('reopened')
  assert.deepEqual(reopened.document.model, redone.document.model)
  await expect(page.locator('.react-flow__minimap')).toBeVisible()
  await page.screenshot({ path: path.join(output, 'atlas-editor.png'), fullPage: true })

  const operations = path.join(work, 'agent-operations.json')
  await writeFile(operations, JSON.stringify({ base_revision: reopened.document.revision, operations: [{ type: 'document.update', changes: { title: 'Agent accepted design' } }] }))
  const cli = path.join(library, 'scripts/atlas-cli.mjs')
  execFileSync(process.execPath, [cli, 'apply', reopened.file, operations, '--in-place', '--expected-revision', String(reopened.document.revision)])
  const currentBytes = await readFile(reopened.file, 'utf8')
  const stale = spawnSync(process.execPath, [cli, 'apply', reopened.file, operations, '--in-place', '--expected-revision', String(reopened.document.revision)], { encoding: 'utf8' })
  assert.notEqual(stale.status, 0)
  assert.match(stale.stderr, /revision|conflict/i)
  assert.equal(await readFile(reopened.file, 'utf8'), currentBytes)
  const agentDocument = core.parseAtlasDocument(currentBytes)
  const html = path.join(work, 'offline.html')
  await writeFile(html, core.createAtlasPortableHtml(agentDocument, { script: portable.script, style: portable.style }))
  const offlineRequests = []
  const record = request => { if (/^https?:/.test(request.url())) offlineRequests.push(request.url()) }
  page.on('request', record)
  await context.setOffline(true)
  await page.goto(pathToFileURL(html).href)
  await expect(page.getByRole('textbox', { name: 'Document title', exact: true })).toHaveValue('Agent accepted design')
  await click(page.getByRole('button', { name: 'Explore design', exact: true }))
  await expect(page.getByRole('textbox', { name: 'Search architecture' })).toBeVisible()
  await type(page.getByRole('textbox', { name: 'Search architecture' }), 'Acceptance worker')
  await click(page.locator('#atlas-search-results').getByRole('button').first())
  await click(page.getByRole('button', { name: 'Focus in Zen', exact: true }))
  await expect(page.getByRole('region', { name: 'Zen view controls' })).toBeVisible()
  await page.screenshot({ path: path.join(output, 'atlas-offline-zen.png'), fullPage: true })
  await click(page.getByRole('button', { name: 'Edit design', exact: true }))
  const htmlDownload = page.waitForEvent('download')
  await click(page.getByRole('button', { name: 'Download editable HTML', exact: true }))
  const downloaded = path.join(work, 'reopened-offline.html')
  await (await htmlDownload).saveAs(downloaded)
  await page.goto(pathToFileURL(downloaded).href)
  assert.deepEqual((await exported('offline-roundtrip')).document.model, agentDocument.model)
  assert.deepEqual(offlineRequests, [])
  page.off('request', record)
  await context.setOffline(false)

  await page.goto(base + '/atlas/')
  const access = await page.evaluate(() => localStorage.getItem('shizuha_access_token'))
  assert.ok(access)
  const headers = { Authorization: 'Bearer ' + access, 'X-Organization-ID': process.env.SHIZUHA_WIKI_ORG }
  async function api(method, route, data, expected = 200) {
    const response = await context.request.fetch(base + route, { method, headers, data })
    assert.equal(response.status(), expected, method + ' ' + route)
    return response.json()
  }
  const spaces = await api('GET', '/wiki/api/spaces/')
  const space = (spaces.results || spaces).find(item => item.key === 'OSS')
  assert.ok(space)
  const content = { type: 'doc', content: [{ type: 'codeBlock', attrs: { language: 'atlas' }, content: [{ type: 'text', text: JSON.stringify(agentDocument) }] }] }
  const wiki = await api('POST', '/wiki/api/pages/', { space: space.id, title: 'Atlas portable acceptance', content_json: content, status: 'published' }, 201)
  const route = '/wiki/api/pages/' + wiki.id + '/'
  // The create serializer omits version. Read the versioned detail before
  // editing so JSON serialization cannot silently omit an undefined guard.
  const initial = await api('GET', route)
  assert.ok(Number.isInteger(initial.version))
  const updated = await api('PATCH', route, { expected_version: initial.version, content_json: content, title: 'Atlas portable acceptance saved' })
  assert.equal(updated.version, initial.version + 1)
  await api('PATCH', route, { expected_version: initial.version, title: 'Stale overwrite' }, 409)
  const stored = await api('GET', route)
  assert.equal(stored.title, updated.title)
  assert.deepEqual(stored.content_json, content)
  await page.goto(`${base}/wiki/spaces/OSS/pages/${stored.slug}?organization=${process.env.SHIZUHA_WIKI_ORG}`)
  const frame = page.frameLocator('iframe[title="Atlas"]')
  await expect(frame.getByRole('textbox', { name: 'Search architecture' })).toBeVisible()
  await page.screenshot({ path: path.join(output, 'atlas-wiki-embed.png'), fullPage: true })
  await page.reload()
  await expect(frame.getByRole('textbox', { name: 'Search architecture' })).toBeVisible()
  assert.deepEqual(errors, [])
  console.log('PASS Atlas visual nesting/relationship/undo/redo/save, offline editor/explorer/Zen roundtrip, agent revision conflict and Wiki persistence/embed conflict')
} finally {
  await page.screenshot({ path: path.join(output, 'atlas-journey-final.png'), fullPage: true }).catch(() => {})
  await browser.close()
}
