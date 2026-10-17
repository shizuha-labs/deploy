const http = require('node:http');

function brokerGet(socketPath, path) {
  return new Promise((resolve, reject) => {
    const request = http.get({socketPath, path}, response => {
      let body = '';
      response.on('data', part => { body += part; });
      response.on('end', () => {
        try { resolve({status: response.statusCode, body: JSON.parse(body)}); }
        catch { reject(new Error('Broker returned invalid JSON')); }
      });
    });
    request.on('error', reject);
    request.setTimeout(5000, () => request.destroy(new Error('Broker request timed out')));
  });
}

async function verifyBrokerIdentity(socketPath, origin, username, {timeoutMs = 60000, pollMs = 1000} = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const ready = await brokerGet(socketPath, '/readyz');
    if (ready.status === 200 && ready.body.ready === true && ready.body.has_token === true) break;
    if (ready.status !== 503 && ready.status !== 200) throw new Error('Broker readiness HTTP ' + ready.status);
    if (Date.now() >= deadline) throw new Error('Broker never became ready to serve an identity');
    await new Promise(resolve => setTimeout(resolve, pollMs));
  }
  const token = await brokerGet(socketPath, '/token');
  if (token.status !== 200 || typeof token.body.access !== 'string' || token.body.access.split('.').length !== 3) {
    throw new Error('Ready broker did not serve an identity JWT');
  }
  // ID validates the token's signature and session; a long response body alone
  // cannot prove that the broker minted this agent's working identity.
  const profile = await fetch(origin + '/id/api/auth/user/', {
    headers: {Authorization: 'Bearer ' + token.body.access},
    signal: AbortSignal.timeout(10000), redirect: 'error',
  });
  if (profile.status !== 200) throw new Error('Broker identity rejected by ID: HTTP ' + profile.status);
  const user = await profile.json();
  if (user.username !== username) throw new Error('Broker identity belongs to another account');
}

module.exports = {verifyBrokerIdentity};
