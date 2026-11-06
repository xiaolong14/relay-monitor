// 复用 promptfoo 原生 Anthropic provider，补齐 SDK 基础地址与 400 参数降级重试。
const { loadApiProvider } = require('promptfoo');
const { Agent } = require('undici');

/**
 * 规范化 Anthropic SDK 的基础地址。
 * SDK 会自行拼接 /v1/messages，因此必须去掉末尾版本段与接口路径，
 * 否则配置了 /v1 的渠道会被请求成 /v1/v1/messages。
 * @param {string} value 渠道配置的 apiBaseUrl
 * @returns {string} 供 SDK 使用的基础地址
 */
function normalizeBaseUrl(value) {
  let url = String(value || '').trim().replace(/\/+$/, '');
  url = url.replace(/(\/v\d+(?:\.\d+)?)\1+/g, '$1');
  url = url.replace(/\/messages$/, '');
  return url.replace(/\/v\d+(?:\.\d+)?$/, '');
}

/**
 * 根据上游 400 错误生成降级参数：只移除可选字段，保留 model / messages / max_tokens。
 * @param {object} params 上一次发送的 Messages 参数
 * @param {string} errorText 上游错误文本
 * @returns {object|null} 可重试的参数；无需降级时返回 null
 */
function degradeParams(params, errorText) {
  const text = String(errorText || '').toLowerCase();
  const next = {...params};
  let changed = false;
  const drop = (field) => {
    if (field in next) {
      delete next[field];
      changed = true;
    }
  };
  if ('temperature' in next && text.includes('temperature')) drop('temperature');
  if ('thinking' in next && text.includes('thinking')) drop('thinking');
  if ('output_config' in next && (text.includes('output_config') || text.includes('effort'))) {
    drop('output_config');
  }
  if (!changed && /unsupported|unknown|not allowed|unrecognized|extra/.test(text)) {
    if ('temperature' in next) drop('temperature');
    else if ('output_config' in next) drop('output_config');
    else if ('thinking' in next) drop('thinking');
  }
  if ('max_tokens' in next && text.includes('max_tokens')
      && /too large|exceed|greater than|maximum/.test(text)) {
    // 上游限制输出上限时收敛到保守值，仍保留协议必需的 max_tokens 字段。
    next.max_tokens = Math.min(Number(next.max_tokens) || 8192, 8192);
    changed = true;
  }
  return changed ? next : null;
}

/**
 * 把上游错误整理成可检索的文本，便于定位被拒字段。
 * @param {Error & {status?: number, error?: object}} error SDK 抛出的错误
 * @returns {string} 错误文本
 */
function errorText(error) {
  const detail = error && error.error ? JSON.stringify(error.error) : '';
  return `${error && error.message ? error.message : ''} ${detail}`;
}

module.exports = class AnthropicTimeoutProvider {
  constructor(options = {}) {
    this.config = {...(options.config || {})};
    this.config.apiBaseUrl = normalizeBaseUrl(this.config.apiBaseUrl);
    this.providerId = `anthropic:messages:${this.config.model}`;
    const timeout = Number(this.config.requestTimeoutMs);
    if (!Number.isSafeInteger(timeout) || timeout <= 0) {
      throw new Error('Anthropic API 请求超时必须为正整数毫秒');
    }
    // 同步底层连接限制，避免传输层在 SDK 配置的超时之前提前中断。
    this.dispatcher = new Agent({ headersTimeout: timeout, bodyTimeout: timeout });
    this.provider = loadApiProvider(this.providerId, {
      options: { config: this.config }, env: options.env,
    }).then((provider) => {
      provider.anthropic = provider.anthropic.withOptions({
        timeout,
        fetchOptions: { ...provider.anthropic.fetchOptions, dispatcher: this.dispatcher },
      });
      // 包装 messages.create：只在 400/422 且错误指向可选字段时降级重试，
      // 其他错误原样抛出，保持 promptfoo 的错误分类行为。
      const create = provider.anthropic.messages.create.bind(provider.anthropic.messages);
      provider.anthropic.messages.create = async (params, requestOptions) => {
        let current = params;
        let lastError = null;
        for (let attempt = 0; attempt < 3; attempt += 1) {
          try {
            return await create(current, requestOptions);
          } catch (error) {
            lastError = error;
            if (![400, 422].includes(error && error.status)) throw error;
            const next = degradeParams(current, errorText(error));
            if (!next) throw error;
            current = next;
          }
        }
        throw lastError;
      };
      return provider;
    });
  }

  /**
   * @returns {string} provider 标识
   */
  id() {
    return this.providerId;
  }

  /**
   * @param {...any} args 透传给 promptfoo provider 的参数
   * @returns {Promise<object>} promptfoo 结果对象
   */
  async callApi(...args) {
    return (await this.provider).callApi(...args);
  }

  /**
   * 释放连接池。
   * @returns {Promise<void>} 无返回值
   */
  async cleanup() {
    try {
      await (await this.provider).cleanup?.();
    } finally {
      await this.dispatcher.close();
    }
  }
};
