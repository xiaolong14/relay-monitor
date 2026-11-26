// 用本地 TLS HTTP/2 服务核对正式 Node 传输层，不能靠模拟 fetch 验证 PING。
const assert = require('node:assert/strict');
const streamFetch = require('../providers/stream_transport.cjs');
const base = process.argv[2];
(async () => {
  const response = await streamFetch(base + '/stream', {method: 'POST', body: '{"原始":"正文"}',
    signal: AbortSignal.timeout(25000)});
  const result = await response.json();
  assert.ok(result.pings > 0, '上游必须实际收到标准 PING');
  assert.deepEqual(JSON.parse(result.body), {原始: '正文'});
  assert.equal(result.method, 'POST');
  const hanging = await streamFetch(base + '/hang', {method: 'POST', body: '{}',
    signal: AbortSignal.timeout(100)});
  await assert.rejects(hanging.text(), /abort|timeout/i);
  process.stdout.write('Node HTTP/2 标准 PING、原始正文及取消验证通过\n');
})().catch(error => {process.stderr.write(error.stack + '\n'); process.exitCode = 1;});
