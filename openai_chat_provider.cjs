// OpenAI Chat Completions 流式适配器：逐段解析 SSE，保留结束原因与 Token 用量，
// 遇到上游 400/422 时按错误信息自动去掉或替换不兼容参数，避免整题因格式被拒。

/**
 * 规范化基础地址：去掉末尾斜杠、重复版本段与接口路径。
 * @param {string} value 渠道配置的 apiBaseUrl
 * @returns {string} 可安全拼接接口路径的基础地址
 */
function normalizeBaseUrl(value) {
  let url = String(value || '').trim().replace(/\/+$/, '');
  url = url.replace(/(\/v\d+(?:\.\d+)?)\1+/g, '$1');
  url = url.replace(/\/(chat\/completions|responses|messages)$/, '');
  return url;
}

/**
 * 生成候选端点：漏写 /v1 的地址额外尝试一次补全版本段，兼容不同中转站路由。
 * @param {string} value 渠道配置的 apiBaseUrl
 * @returns {string[]} 按优先级排列的完整请求地址
 */
function endpointCandidates(value) {
  const base = normalizeBaseUrl(value);
  const url = `${base}/chat/completions`;
  if (/\/v\d+(?:\.\d+)?(?:$|\/)/.test(base.replace(/^[a-z]+:\/\/[^/]+/i, ''))) {
    return [url];
  }
  return [url, `${base}/v1/chat/completions`];
}

/**
 * 构造最小合法请求体，字段名称与取值全部来自 Python 侧下发的配置。
 * @param {object} config provider 配置
 * @param {string} prompt 用户输入
 * @returns {object} Chat Completions 请求体
 */
function buildBody(config, prompt) {
  const body = {
    model: config.model,
    messages: [{role: 'user', content: prompt}],
    stream: true,
    stream_options: {include_usage: true},
  };
  const tokens = Number(config.max_tokens);
  if (Number.isSafeInteger(tokens) && tokens > 0) {
    body[config.max_tokens_field === 'max_completion_tokens' ? 'max_completion_tokens' : 'max_tokens'] = tokens;
  }
  if (typeof config.temperature === 'number') body.temperature = config.temperature;
  if (typeof config.reasoning_effort === 'string' && config.reasoning_effort) {
    body.reasoning_effort = config.reasoning_effort;
  }
  return body;
}

/**
 * 根据上游错误正文生成降级请求体：只处理可选字段，绝不动 model / messages。
 * @param {object} body 上一次发送的请求体
 * @param {string} errorText 上游错误正文
 * @returns {object|null} 可重试的请求体；无需降级时返回 null
 */
function degradeBody(body, errorText) {
  const text = String(errorText || '').toLowerCase();
  const next = {...body};
  let changed = false;
  const drop = (field) => {
    if (field in next) {
      delete next[field];
      changed = true;
    }
  };
  for (const field of ['stream_options', 'temperature', 'reasoning_effort']) {
    if (field in next && text.includes(field)) drop(field);
  }
  if (text.includes('include_usage')) drop('stream_options');
  const limit = 'max_completion_tokens' in next
    ? 'max_completion_tokens' : ('max_tokens' in next ? 'max_tokens' : null);
  const alternate = limit === 'max_completion_tokens' ? 'max_tokens' : 'max_completion_tokens';
  if (limit && text.includes(limit)) {
    if (alternate in next) {
      delete next[limit];
    } else {
      next[alternate] = next[limit];
      delete next[limit];
    }
    changed = true;
  }
  if (!changed && /unsupported|unknown|not allowed|unrecognized|extra/.test(text)) {
    for (const field of ['temperature', 'reasoning_effort', 'stream_options']) {
      if (field in next) {
        drop(field);
        break;
      }
    }
  }
  return changed ? next : null;
}

/**
 * 提取 Chat Completions 响应中的文本，兼容字符串与分段内容数组。
 * @param {object} message choices[0].message
 * @returns {string} 模型输出文本
 */
function messageText(message) {
  if (!message || typeof message !== 'object') return '';
  if (typeof message.refusal === 'string' && message.refusal) return message.refusal;
  const content = message.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter((part) => part && part.type === 'text' && typeof part.text === 'string')
    .map((part) => part.text)
    .join('');
}

/**
 * 读取 usage，统一成 promptfoo 需要的 tokenUsage 结构。
 * @param {object} data 上游响应
 * @returns {{prompt: number, completion: number, total: number}} 用量统计
 */
function tokenUsage(data) {
  const usage = (data && data.usage) || {};
  const prompt = Number(usage.prompt_tokens) || 0;
  const completion = Number(usage.completion_tokens) || 0;
  return {prompt, completion, total: Number(usage.total_tokens) || prompt + completion};
}

/** 统一检查结束原因，截断输出不能送去判分。 */
function checkCompletion(reason) {
  if (reason === 'length') throw new Error('模型输出达到长度上限，未完整生成');
  if (reason === 'content_filter') throw new Error('模型输出被内容过滤拦截');
}

/** 逐段消费 SSE；兼容忽略 stream 参数的渠道返回 JSON，接收大小始终受限。 */
async function readChatResponse(response, maxBytes) {
  if (!response.body) throw new Error('OpenAI Chat Completions 流没有响应体');
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', {fatal: true});
  const parts = [];
  const refusals = [];
  let buffer = '';
  let mode = null;
  let received = 0;
  let ended = false;
  let done = false;
  let finishReason = null;
  const raw = {choices: [], usage: null};

  const event = (block) => {
    const data = block.split(/\r?\n/).filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).replace(/^ /, '')).join('\n');
    if (!data) return;
    if (data === '[DONE]') { done = true; return; }
    let payload;
    try { payload = JSON.parse(data); }
    catch { throw new Error('OpenAI Chat Completions 流包含无效 JSON 事件'); }
    if (payload.error || payload.type === 'error') {
      const error = payload.error || payload;
      const code = typeof error.code === 'string' && /^[A-Za-z0-9_.-]{1,80}$/.test(error.code)
        ? error.code : null;
      throw new Error(`OpenAI Chat Completions 流返回错误${code ? `（${code}）` : ''}`);
    }
    if (payload.usage) raw.usage = payload.usage;
    if (payload.id) raw.id = payload.id;
    if (payload.model) raw.model = payload.model;
    // 本项目只请求一个候选，不拼接其他 index 的内容或推理字段。
    const choice = (payload.choices || []).find(item => (item.index ?? 0) === 0);
    if (!choice) return;
    const delta = choice.delta || {};
    const text = messageText({content: delta.content});
    if (text) parts.push(text);
    if (typeof delta.refusal === 'string') refusals.push(delta.refusal);
    if (choice.finish_reason != null) finishReason = choice.finish_reason;
  };

  try {
    while (!done) {
      const {done: eof, value} = await reader.read();
      if (eof) { ended = true; break; }
      received += value.byteLength;
      if (received > maxBytes) throw new Error('渠道响应超过大小限制');
      buffer += decoder.decode(value, {stream: true});
      if (!mode && buffer.trim()) mode = /^[\s]*[\[{]/.test(buffer) ? 'json' : 'sse';
      if (mode === 'json') continue;
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() || '';
      for (const block of blocks) { event(block); if (done) break; }
    }
    buffer += decoder.decode();
    if (mode === 'json') {
      console.warn('[Chat Completions] 渠道忽略流式请求，返回了同步 JSON');
      return JSON.parse(buffer);
    }
    if (!done && buffer.trim()) event(buffer);
    if (!done && finishReason == null) {
      throw new Error('上游 Chat Completions 流在生成完成前断开，未收到结束标记');
    }
    checkCompletion(finishReason);
    raw.choices = [{index: 0, finish_reason: finishReason,
      message: {role: 'assistant', content: parts.join(''), refusal: refusals.join('')}}];
    return raw;
  } finally {
    if (!ended) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

module.exports = class OpenAIChatProvider {
  /**
   * @param {object} options promptfoo provider 选项
   */
  constructor(options = {}) {
    this.config = options.config || {};
    this.providerId = `openai:chat:${this.config.model || 'unknown'}`;
    this.timeout = Number(this.config.requestTimeoutMs);
    if (!Number.isSafeInteger(this.timeout) || this.timeout <= 0) {
      throw new Error('OpenAI API 请求超时必须为正整数毫秒');
    }
  }

  /**
   * @returns {string} provider 标识
   */
  id() {
    return this.providerId;
  }

  /**
   * 发送请求；400/422 时按上游提示降级参数，404/405 时切换端点变体。
   * @param {string} prompt 用户输入
   * @returns {Promise<object>} promptfoo 结果对象
   */
  async callApi(prompt) {
    const {apiBaseUrl, apiKey, model} = this.config;
    if (!apiBaseUrl || !apiKey || !model) {
      return {error: 'OpenAI 渠道地址、模型或 API Key 未配置'};
    }
    const candidates = endpointCandidates(apiBaseUrl);
    let lastError = '';
    for (let index = 0; index < candidates.length; index += 1) {
      let body = buildBody(this.config, prompt);
      let retried = 0;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const outcome = await this.request(candidates[index], apiKey, body);
        if (outcome.response) {
          return outcome.response;
        }
        lastError = outcome.error;
        const degraded = outcome.errorText ? degradeBody(body, outcome.errorText) : null;
        if (degraded && retried < 2) {
          body = degraded;
          retried += 1;
          continue;
        }
        break;
      }
    }
    return {error: lastError || 'API call error: 请求未完成'};
  }

  /**
   * 单次请求并解析响应；失败时返回状态与错误正文供上层决定是否降级。
   * @param {string} url 完整请求地址
   * @param {string} apiKey 渠道密钥
   * @param {object} body 请求体
   * @returns {Promise<object>} 成功时为 {response}，失败时为 {error, errorText}
   */
  async request(url, apiKey, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream, application/json',
          Authorization: `Bearer ${apiKey}`,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!response.ok) {
        // 错误正文只用于判断可降级的字段，不回传给评测结果，避免泄露请求信息。
        const errorText = await response.text().catch(() => '');
        return {
          error: `API error: ${response.status} ${response.statusText}`,
          errorText: errorText.slice(0, 4096),
        };
      }
      const tokens = Number(body.max_completion_tokens || body.max_tokens) || 32768;
      const maxBytes = Math.max(64 * 1024, tokens * 128 * 6 + 64 * 1024);
      const data = await readChatResponse(response, maxBytes);
      if (data && (data.error || data.type === 'error')) {
        return {error: 'OpenAI 接口返回错误', errorText: ''};
      }
      const choices = Array.isArray(data && data.choices) ? data.choices : [];
      checkCompletion(choices[0]?.finish_reason);
      const output = messageText(choices[0] && choices[0].message);
      if (!output || !output.trim()) {
        return {error: '模型没有返回文本内容', errorText: ''};
      }
      return {response: {output, tokenUsage: tokenUsage(data), raw: data}};
    } catch (error) {
      if (error && error.name === 'AbortError') {
        return {error: `API request timed out after ${this.timeout} ms`, errorText: ''};
      }
      return {
        error: `API call error: ${error instanceof Error ? error.message : String(error)}`,
        errorText: '',
      };
    } finally {
      clearTimeout(timer);
    }
  }
};
