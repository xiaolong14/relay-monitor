// Chat Completions 题库适配器回归：验证分片、用量、兼容降级、断流和超时取消。
const assert = require('node:assert/strict');
const Provider = require('../providers/openai_chat_provider.cjs');
const config = {apiBaseUrl: 'https://relay.invalid/v1', apiKey: 'sk-test', model: 'gpt-6.1-sol',
  max_tokens: 2048, max_tokens_field: 'max_completion_tokens', requestTimeoutMs: 1000, reasoning_effort: 'xhigh'};
const chunk = (content, finish_reason = null) => ({choices: [{index: 0, delta: {content}, finish_reason}]});
const wire = (...events) => events.map(event => `data: ${typeof event === 'string' ? event : JSON.stringify(event)}\r\n\r\n`).join('');

function response(text, held = false) {
  const bytes = new TextEncoder().encode(text);
  let offset = 0;
  return new Response(new ReadableStream({
    pull(controller) {
      if (offset < bytes.length) controller.enqueue(bytes.slice(offset, ++offset));
      else if (!held) controller.close();
    },
  }), {headers: {'content-type': 'text/event-stream'}});
}

(async () => {
  const requests = [];
  global.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    assert.match(options.headers.Accept, /text\/event-stream/);
    return response(wire(
      {choices: [{index: 0, delta: {reasoning_content: '推理'}}]},
      chunk('答'), chunk([{type: 'text', text: '案'}]),
      {choices: [{index: 1, delta: {content: '其他候选'}}]},
      chunk(null, 'stop'),
      {choices: [], usage: {prompt_tokens: 3, completion_tokens: 2, total_tokens: 5}}, '[DONE]',
    ), true);
  };
  const success = await new Provider({config}).callApi('测试');
  assert.equal(success.output, '答案');
  assert.deepEqual(success.tokenUsage, {prompt: 3, completion: 2, total: 5});
  assert.equal(requests[0].stream, true);
  assert.deepEqual(requests[0].stream_options, {include_usage: true});
  assert.equal(requests[0].reasoning_effort, 'xhigh');

  for (const [text, expected] of [
    [wire(chunk('部分正文')), /生成完成前断开/],
    [wire(chunk('部分正文', 'length'), '[DONE]'), /长度上限/],
    [wire(chunk('', 'content_filter'), '[DONE]'), /过滤/],
    [wire({error: {code: 'stream_timeout', message: '不要回显上游消息'}}), /stream_timeout/],
    ['data: {invalid}\n\n', /无效 JSON/],
  ]) {
    global.fetch = async () => response(text);
    const result = await new Provider({config}).callApi('测试');
    assert.match(result.error, expected);
    assert.equal(result.output, undefined);
    assert.doesNotMatch(result.error, /不要回显/);
  }

  const degraded = [];
  global.fetch = async (_url, options) => {
    const body = JSON.parse(options.body);
    degraded.push(body);
    return degraded.length === 1
      ? new Response('Unsupported parameter: stream_options.include_usage', {status: 400})
      : response(wire(chunk('2', 'stop'), '[DONE]'));
  };
  assert.equal((await new Provider({config}).callApi('测试')).output, '2');
  assert.equal(degraded[1].stream, true);
  const {stream_options, ...rest} = degraded[0];
  assert.deepEqual(degraded[1], rest);

  // 上游忽略 stream 时仍能读取 JSON，不能因此发送第二次非流式请求。
  global.fetch = async () => new Response(JSON.stringify({choices: [{message: {content: '2'}}]}));
  assert.equal((await new Provider({config}).callApi('测试')).output, '2');

  let aborted = false;
  global.fetch = async (_url, options) => new Response(new ReadableStream({start(controller) {
    options.signal.addEventListener('abort', () => {
      aborted = true;
      controller.error(new DOMException('cancelled', 'AbortError'));
    });
    controller.enqueue(new TextEncoder().encode(wire(chunk('还没结束'))));
  }}));
  assert.match((await new Provider({config: {...config, requestTimeoutMs: 30}}).callApi('测试')).error, /timed out/);
  assert.equal(aborted, true);

  global.fetch = async () => response(wire(chunk('x'.repeat(70 * 1024), 'stop'), '[DONE]'));
  assert.match((await new Provider({config: {...config, max_tokens: 1}}).callApi('测试')).error, /大小限制/);
  process.stdout.write('流式协议验证通过\n');
})().catch(error => { process.stderr.write(String(error.stack)); process.exitCode = 1; });
