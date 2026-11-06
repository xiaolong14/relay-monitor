// OpenAI Responses 流式适配器：让长请求持续接收 SSE，避免代理等待完整响应时超时。

function extractResponseText(response) {
  if (!response || typeof response !== 'object') return '';
  if (typeof response.output_text === 'string') return response.output_text;
  let text = '';
  for (const item of Array.isArray(response.output) ? response.output : []) {
    if (item?.type !== 'message' || item.role !== 'assistant') continue;
    for (const content of Array.isArray(item.content) ? item.content : []) {
      if (content?.type === 'output_text' && typeof content.text === 'string') {
        text += content.text;
      } else if (content?.type === 'refusal' && typeof content.refusal === 'string') {
        text += content.refusal;
      }
    }
  }
  return text;
}

function incompleteError(response) {
  if (response?.status !== 'incomplete') return null;
  const reason = response.incomplete_details?.reason;
  return reason === 'max_output_tokens'
    ? '模型输出达到长度上限'
    : `OpenAI Responses 返回未完成状态${reason ? `：${reason}` : ''}`;
}

function tokenUsage(response) {
  const usage = response?.usage || {};
  const prompt = Number(usage.input_tokens) || 0;
  const completion = Number(usage.output_tokens) || 0;
  return {prompt, completion, total: Number(usage.total_tokens) || prompt + completion};
}

function parseEvent(block, state) {
  const lines = block.split(/\r?\n/);
  const eventName = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
  const data = lines
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).replace(/^ /, ''))
    .join('\n');
  if (!data || data === '[DONE]') return;

  let payload;
  try {
    payload = JSON.parse(data);
  } catch {
    throw new Error('OpenAI Responses 流包含无效 JSON 事件');
  }
  const type = payload.type || eventName;
  if (type === 'response.output_text.delta' && typeof payload.delta === 'string') {
    state.output += payload.delta;
  } else if (type === 'response.output_text.done' && !state.output && typeof payload.text === 'string') {
    state.output = payload.text;
  } else if (type === 'response.completed' || type === 'response.incomplete') {
    state.response = payload.response || payload;
  } else if (type === 'response.failed' || type === 'error') {
    const error = payload.error || payload.response?.error || {};
    throw new Error(`OpenAI Responses 流返回错误${error.code ? `（${error.code}）` : ''}`);
  }
}

async function readEventStream(body, maxBytes) {
  if (!body) throw new Error('OpenAI Responses 流没有响应体');
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const state = {output: '', response: null};
  let buffer = '';
  let received = 0;
  let ended = false;

  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) {
        ended = true;
        break;
      }
      received += value.byteLength;
      if (received > maxBytes) throw new Error('渠道响应超过大小限制');
      buffer += decoder.decode(value, {stream: true});
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() || '';
      for (const block of blocks) parseEvent(block, state);
    }
  } finally {
    if (!ended) await reader.cancel().catch(() => {});
  }
  buffer += decoder.decode();
  if (buffer.trim()) parseEvent(buffer, state);

  const incomplete = incompleteError(state.response);
  if (incomplete) throw new Error(incomplete);
  if (!state.output) state.output = extractResponseText(state.response);
  if (!state.output.trim()) throw new Error('模型没有返回文本内容');
  return {output: state.output, response: state.response};
}

/**
 * 根据上游 400/422 错误去掉 Responses 协议的可选字段，避免因单个扩展字段被整题拒绝。
 * @param {object} body 上一次发送的请求体
 * @param {string} text 上游错误正文
 * @returns {object|null} 可重试的请求体；无需降级时返回 null
 */
function degradeBody(body, text) {
  const content = String(text || '').toLowerCase();
  const next = {...body};
  let changed = false;
  const drop = (field) => {
    if (field in next) {
      delete next[field];
      changed = true;
    }
  };
  if ('reasoning' in next && content.includes('reasoning')) drop('reasoning');
  if ('max_output_tokens' in next && content.includes('max_output_tokens')) drop('max_output_tokens');
  if (!changed && /unsupported|unknown|not allowed|unrecognized|extra/.test(content)) drop('reasoning');
  return changed ? next : null;
}

module.exports = class OpenAIResponsesStreamProvider {
  constructor(options = {}) {
    this.config = options.config || {};
    this.providerId = `openai:responses:${this.config.model || 'unknown'}`;
    this.timeout = Number(this.config.requestTimeoutMs);
    if (!Number.isSafeInteger(this.timeout) || this.timeout <= 0) {
      throw new Error('OpenAI Responses API 请求超时必须为正整数毫秒');
    }
  }

  id() {
    return this.providerId;
  }

  async callApi(prompt) {
    const {apiBaseUrl, apiKey, model} = this.config;
    if (!apiBaseUrl || !apiKey || !model) {
      return {error: 'OpenAI Responses 渠道地址、模型或 API Key 未配置'};
    }
    const maxOutputTokens = Number(this.config.max_output_tokens);
    if (!Number.isSafeInteger(maxOutputTokens) || maxOutputTokens <= 0) {
      return {error: 'OpenAI Responses 最大输出 Token 必须为正整数'};
    }
    const maxBytes = Math.max(64 * 1024, maxOutputTokens * 128 * 6 + 64 * 1024);
    const body = {
      model,
      input: prompt,
      max_output_tokens: maxOutputTokens,
      stream: this.config.stream !== false,
      ...(this.config.reasoning_effort
        ? {reasoning: {effort: this.config.reasoning_effort}}
        : {}),
    };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);
    try {
      let requestBody = body;
      let lastStatus = null;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const response = await fetch(`${String(apiBaseUrl).replace(/\/$/, '')}/responses`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'text/event-stream, application/json',
            Authorization: `Bearer ${apiKey}`,
          },
          body: JSON.stringify(requestBody),
          signal: controller.signal,
        });
        if (!response.ok) {
          // 错误正文只用于判断可降级字段，不写入评测结果，避免请求信息进入页面和历史库。
          const errorText = await response.text().catch(() => '');
          lastStatus = `${response.status} ${response.statusText}`;
          const degraded = [400, 422].includes(response.status)
            ? degradeBody(requestBody, errorText) : null;
          if (degraded) {
            requestBody = degraded;
            continue;
          }
          return {error: `API error: ${lastStatus}`};
        }

        const contentType = response.headers.get('content-type') || '';
        let output;
        let finalResponse;
        if (contentType.toLowerCase().includes('text/event-stream')) {
          ({output, response: finalResponse} = await readEventStream(response.body, maxBytes));
        } else {
          const raw = await response.text();
          if (Buffer.byteLength(raw) > maxBytes) throw new Error('渠道响应超过大小限制');
          finalResponse = JSON.parse(raw);
          const incomplete = incompleteError(finalResponse);
          if (incomplete) throw new Error(incomplete);
          if (finalResponse.error) throw new Error('OpenAI Responses 返回错误');
          output = extractResponseText(finalResponse);
          if (!output.trim()) throw new Error('模型没有返回文本内容');
        }
        return {output, tokenUsage: tokenUsage(finalResponse), raw: finalResponse};
      }
      return {error: `API error: ${lastStatus || '请求未完成'}`};
    } catch (error) {
      if (error?.name === 'AbortError') {
        return {error: `API request timed out after ${this.timeout} ms`};
      }
      return {error: `API call error: ${error instanceof Error ? error.message : String(error)}`};
    } finally {
      clearTimeout(timer);
    }
  }
};
