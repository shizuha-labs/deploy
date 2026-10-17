// Isolated CI model fixture. The HTTP/SSE parser, agent loop and write tool are
// real; only model responses are scripted. This is never a production service.
const http = require('node:http');
const key = process.env.FIXTURE_KEY;
const marker = process.env.FIXTURE_MARKER;
if (!key || !marker) throw new Error('Explicit isolated fixture configuration required');
let requests = 0;
let toolObserved = false;
http.createServer(async (req, res) => {
  if (req.headers.authorization !== `Bearer ${key}`) {
    res.writeHead(401); res.end(); return;
  }
  if (req.method === 'GET') {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(req.url === '/evidence' ? { requests, toolObserved } :
      { object: 'list', data: [{ id: 'selfhost-acceptance', object: 'model', owned_by: 'isolated-test' }] }));
    return;
  }
  let raw = '';
  for await (const part of req) { raw += part; if (raw.length > 4000000) { res.writeHead(413); res.end(); return; } }
  let body;
  try { body = JSON.parse(raw); } catch { res.writeHead(400); res.end(); return; }
  const relevant = (body.messages || []).some(m => m.role === 'user' && JSON.stringify(m.content).includes(marker));
  const hasTool = relevant && (body.messages || []).some(m => m.role === 'tool' && m.tool_call_id === 'selfhost-write');
  if (relevant) requests++;
  if (hasTool) toolObserved = true;
  const call = relevant && !hasTool;
  const message = call ? { role: 'assistant', content: null, tool_calls: [{
    id: 'selfhost-write', type: 'function', function: { name: 'write', arguments: JSON.stringify({
      file_path: '/home/agent/.shizuha/acceptance-proof.txt', content: marker + '\n',
    }) },
  }] } : { role: 'assistant', content: hasTool ? `Completed ${marker}` : 'Ready' };
  const common = { id: 'selfhost-response', object: 'chat.completion', created: Math.floor(Date.now() / 1000), model: body.model };
  if (!body.stream) {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ ...common, choices: [{ index: 0, message, finish_reason: call ? 'tool_calls' : 'stop' }],
      usage: { prompt_tokens: 100, completion_tokens: 30, total_tokens: 130 } })); return;
  }
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
  const delta = call ? { role: 'assistant', tool_calls: [{ index: 0, ...message.tool_calls[0] }] } : message;
  res.write('data: ' + JSON.stringify({ ...common, object: 'chat.completion.chunk', choices: [{ index: 0, delta, finish_reason: null }] }) + '\n\n');
  res.write('data: ' + JSON.stringify({ ...common, object: 'chat.completion.chunk', choices: [{ index: 0, delta: {}, finish_reason: call ? 'tool_calls' : 'stop' }],
    usage: { prompt_tokens: 100, completion_tokens: 30, total_tokens: 130 } }) + '\n\n');
  res.end('data: [DONE]\n\n');
}).listen(8099, '0.0.0.0');
