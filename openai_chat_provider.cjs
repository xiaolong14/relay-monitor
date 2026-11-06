// OpenAI Chat Completions 适配器：请求体只包含 Python 侧确认可用的字段，
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
  for (const field of ['temperature', 'reasoning_effort']) {
    if (field in next && text.includes(field)) drop(field);
  }
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
    for (const field of ['temperature', 'reasoning_effort']) {
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
          Accept: 'application/json',
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
      const data = await response.json();
      if (data && data.error) {
        return {error: 'OpenAI 接口返回错误', errorText: ''};
      }
      const choices = Array.isArray(data && data.choices) ? data.choices : [];
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
