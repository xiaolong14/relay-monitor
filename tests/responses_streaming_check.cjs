// Responses 流式结束契约：终止事件立即结束，断流不能送去判分。
const assert = require('node:assert/strict');
const Provider = require('../providers/openai_responses_provider.cjs');
const config = {apiBaseUrl: 'https://relay.invalid/v1', apiKey: 'sk-test', model: 'gpt-6.1-sol',
  max_output_tokens: 2048, requestTimeoutMs: 200, reasoning_effort: 'xhigh', stream: false};
const wire = (...events) => events.map(event => `data: ${typeof event === 'string' ? event : JSON.stringify(event)}\r\n\r\n`).join('');

function response(text, signal, held = false) {
  const bytes = new TextEncoder().encode(text);
  let offset = 0;
  let abort;
  return new Response(new ReadableStream({
    start(controller) {
      abort = () => controller.error(new DOMException('模拟读取超时', 'AbortError'));
      signal.addEventListener('abort', abort, {once: true});
    },
    pull(controller) {
      if (offset < bytes.length) controller.enqueue(bytes.slice(offset, ++offset));
      else if (!held) {
        signal.removeEventListener('abort', abort);
        controller.close();
      }
    },
    cancel() { signal.removeEventListener('abort', abort); },
  }), {headers: {'content-type': 'text/event-stream'}});
}

(async () => {
  for (const type of ['response.completed', 'response.done']) {
    let captured;
    global.fetch = async (_url, options) => {
      captured = JSON.parse(options.body);
      return response(wire({type: 'response.output_text.delta', delta: '答案'},
        {type, response: {status: 'completed', usage: {input_tokens: 3, output_tokens: 2}}}), options.signal, true);
    };
    const result = await new Provider({config}).callApi('测试');
    assert.equal(result.output, '答案');
    assert.deepEqual(result.tokenUsage, {prompt: 3, completion: 2, total: 5});
    assert.equal(captured.stream, true);
    assert.deepEqual(captured.reasoning, {effort: 'xhigh'});
  }

  for (const [text, expected] of [
    [wire({type: 'response.output_text.delta', delta: '半截答案'}), /未收到终止事件/],
    [wire({type: 'response.output_text.delta', delta: '半截答案'}, '[DONE]'), /未收到终止事件/],
    [wire({type: 'response.incomplete', response: {status: 'incomplete', incomplete_details: {reason: 'max_output_tokens'}}}), /长度上限/],
    [wire({type: 'response.failed', response: {error: {code: 'stream_timeout'}}}), /stream_timeout/],
    [wire({type: 'error', code: 'stream_timeout'}), /stream_timeout/],
    [wire({type: 'response.cancelled', response: {status: 'cancelled', output_text: '不能判为成功'}}), /取消/],
  ]) {
    global.fetch = async (_url, options) => response(text, options.signal, !text.includes('半截答案'));
    const result = await new Provider({config}).callApi('测试');
    assert.match(result.error, expected);
    assert.equal(result.output, undefined);
  }
  process.stdout.write('Responses 流式协议验证通过\n');
})().catch(error => { process.stderr.write(String(error.stack)); process.exitCode = 1; });
