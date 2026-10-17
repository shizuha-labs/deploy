const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {verifyBrokerIdentity} = require('./runtime-broker-acceptance.cjs');

async function fixture(t, {readyStatus = 200, tokenStatus = 200, username = 'fixture-agent'} = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'broker-acceptance-'));
  const socket = path.join(directory, 'broker.sock');
  let readiness = 0;
  const calls = [];
  const broker = http.createServer((req, res) => {
    calls.push(req.url);
    let body;
    if (req.url === '/readyz') {
      readiness++;
      res.statusCode = readiness === 1 ? 503 : readyStatus;
      body = {ready: res.statusCode === 200, has_token: res.statusCode === 200};
    } else {
      res.statusCode = tokenStatus;
      body = {access: 'fixture.identity.jwt'};
    }
    res.end(JSON.stringify(body));
  });
  const id = http.createServer((req, res) => {
    assert.equal(req.url, '/id/api/auth/user/');
    assert.equal(req.headers.authorization, 'Bearer fixture.identity.jwt');
    res.end(JSON.stringify({username}));
  });
  t.after(async () => {
    await Promise.all([new Promise(resolve => broker.close(resolve)), new Promise(resolve => id.close(resolve))]);
    await fs.rm(directory, {recursive: true});
  });
  await Promise.all([new Promise(resolve => broker.listen(socket, resolve)), new Promise(resolve => id.listen(0, '127.0.0.1', resolve))]);
  return {socket, origin: 'http://127.0.0.1:' + id.address().port, calls};
}

test('waits for actual broker mint readiness then proves the same identity at ID', async t => {
  const f = await fixture(t);
  await verifyBrokerIdentity(f.socket, f.origin, 'fixture-agent', {pollMs: 1});
  assert.deepEqual(f.calls, ['/readyz', '/readyz', '/token']);
});
test('permanent unready broker is not accepted', async t => {
  const f = await fixture(t, {readyStatus: 503});
  await assert.rejects(verifyBrokerIdentity(f.socket, f.origin, 'fixture-agent', {timeoutMs: 0}), /never became ready/);
  assert.deepEqual(f.calls, ['/readyz']);
});
test('readiness route errors cannot masquerade as a mint delay', async t => {
  const f = await fixture(t, {readyStatus: 404});
  await assert.rejects(verifyBrokerIdentity(f.socket, f.origin, 'fixture-agent', {pollMs: 1}), /readiness HTTP 404/);
});
test('ready broker with refused token fails', async t => {
  const f = await fixture(t, {tokenStatus: 401});
  await assert.rejects(verifyBrokerIdentity(f.socket, f.origin, 'fixture-agent', {pollMs: 1}), /did not serve/);
});
test('valid token for another identity fails', async t => {
  const f = await fixture(t, {username: 'another-agent'});
  await assert.rejects(verifyBrokerIdentity(f.socket, f.origin, 'fixture-agent', {pollMs: 1}), /another account/);
});
