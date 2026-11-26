// 题库两个 OpenAI 协议共用连接池与标准 HTTP/2 PING，正文和 SSE 解析保持原样。
const {Agent} = require('undici');
const dispatcher = new Agent({
  allowH2: true,
  pingInterval: 20000,
  // 总时限由调用方 AbortSignal 统一控制，避免隐藏的 300 秒超时覆盖用户配置。
  headersTimeout: 0,
  bodyTimeout: 0,
});
const transport = {
  dispatch(options, handler) {
    // fetch 自身也会传入 300 秒默认值，需在调度入口统一覆盖，交给 AbortSignal。
    return dispatcher.dispatch({...options, headersTimeout: 0, bodyTimeout: 0}, handler);
  },
};

module.exports = function streamFetch(url, options) {
  return fetch(url, {...options, dispatcher: transport});
};
