// 本地 HTTP/2 对照端：只有收到标准 PING 才交付完整正文，模拟长流保活要求。
const http2 = require('node:http2');
const fs = require('node:fs');
const server = process.argv[2] ? http2.createSecureServer({
  cert: fs.readFileSync(process.argv[2]), key: fs.readFileSync(process.argv[3]),
}) : http2.createServer();
let pings = 0;
const sessions = new Set();
server.on('session', session => {
  sessions.add(session);
  session.on('close', () => sessions.delete(session));
  session.on('error', () => {});
  session.on('ping', () => { pings += 1; });
});
server.on('stream', (stream, headers) => {
  stream.on('error', () => {});
  if (headers[':path'] === '/reset') {
    stream.respond({':status': 200});
    stream.write('data: partial\n\n');
    setTimeout(() => stream.destroy(new Error('模拟异常断流'), http2.constants.NGHTTP2_INTERNAL_ERROR), 30);
    return;
  }
  const body = [];
  stream.on('data', chunk => body.push(chunk));
  stream.on('end', () => {
    if (headers[':path'] === '/headers-hang') return;
    stream.respond({':status': headers[':path'] === '/error' ? 429 : 200,
      'content-type': 'application/json', 'x-request-id': 'local-h2-test'});
    const before = pings;
    if (headers[':path'] === '/hang') return;
    const timer = setInterval(() => {
      if (pings > before || headers[':path'] === '/error') {
        clearInterval(timer);
        stream.end(JSON.stringify({pings, method: headers[':method'], body: Buffer.concat(body).toString()}));
      }
    }, 10);
    stream.on('close', () => clearInterval(timer));
  });
});
server.listen(0, '127.0.0.1', () => process.stdout.write(String(server.address().port) + '\n'));
process.stdin.resume();
process.stdin.on('end', () => {
  for (const session of sessions) session.destroy();
  server.close();
});
