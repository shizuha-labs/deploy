import assert from 'node:assert/strict'
import test from 'node:test'
import { isPendingHiveIndex } from './browser-response-contract.mjs'

test('only the precise asynchronous Hive index response is pending', () => {
  const request = { method: 'GET', status: 503, path: '/hive/api/v1/fleet/agent-cards/' }
  const headers = { 'x-hive-cache': 'building', 'retry-after': '1' }
  const body = { error: 'agents index rebuilding', retry: true }
  assert.equal(isPendingHiveIndex('hive', request, headers, body), true)
  for (const [app, req, hdr, value] of [
    ['pulse', request, headers, body],
    ['hive', { ...request, status: 500 }, headers, body],
    ['hive', { ...request, method: 'POST' }, headers, body],
    ['hive', { ...request, path: '/hive/api/v1/fleet/agents/example/status/' }, headers, body],
    ['hive', request, {}, body],
    ['hive', request, headers, { error: 'dependency unavailable', retry: true }],
    ['hive', request, headers, { ...body, retry: false }],
  ]) assert.equal(isPendingHiveIndex(app, req, hdr, value), false)
})
