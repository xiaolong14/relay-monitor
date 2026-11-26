"""长流公共传输：协商 HTTP/2，用标准 PING 保活，保留 urllib 响应接口。"""

from email.message import Message
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import httpcore


logger = logging.getLogger(__name__)
PING_INTERVAL_SECONDS = 20


class _Deadline:
    """从建连前开始计时，响应头等待和 PING ACK 都不能延长总时限。"""

    def __init__(self, timeout):
        self.network = None
        self.expired = False
        self.timer = threading.Timer(timeout, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def attach(self, network):
        self.network = network
        if self.expired:
            self._expire()

    def _expire(self):
        self.expired = True
        network = self.network
        if network:
            if isinstance(network, _PingStream):
                network.closed.set()
            sock = network.get_extra_info("socket")
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            network.close()

    def cancel(self):
        self.timer.cancel()


class _PingStream(httpcore.NetworkStream):
    """在 HTTP/2 帧之间写 PING；不增加 HTTP 请求，也不干预模型正文。"""

    def __init__(self, stream, interval=PING_INTERVAL_SECONDS, deadline=None):
        self.stream = stream
        self.interval = interval
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.worker = None
        self.pings = 0
        self.deadline = deadline
        if deadline:
            deadline.attach(self)

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        with self.lock:
            self.stream.write(buffer, timeout)
            # 只在 HTTP/2 连接前言写出后启动，HTTP/1.1 与 CONNECT 均不注入帧。
            if self.worker is None and buffer.startswith(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"):
                self.worker = threading.Thread(target=self._keepalive, name="上游HTTP2保活", daemon=True)
                self.worker.start()

    def _keepalive(self):
        while not self.closed.wait(self.interval):
            try:
                with self.lock:
                    if self.closed.is_set():
                        return
                    self.pings += 1
                    # RFC 9113：长度 8、类型 PING、连接级 stream_id=0、8 字节不透明数据。
                    frame = b"\x00\x00\x08\x06\x00\x00\x00\x00\x00" + self.pings.to_bytes(8, "big")
                    self.stream.write(frame, 10)
                logger.debug("上游 HTTP/2 标准 PING 已发送：次数=%s", self.pings)
            except Exception as exc:
                if not self.closed.is_set():
                    logger.warning("上游 HTTP/2 保活失败：类型=%s", type(exc).__name__)
                # 关闭坏连接唤醒正在等待响应的消费者，禁止保活线程静默退出后继续挂起。
                self.close()
                return

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _PingStream(self.stream.start_tls(ssl_context, server_hostname, timeout), self.interval, self.deadline)

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def close(self):
        self.closed.set()
        self.stream.close()


class _PingBackend(httpcore.NetworkBackend):
    def __init__(self, interval=PING_INTERVAL_SECONDS, deadline=None):
        self.backend = httpcore.SyncBackend()
        self.interval = interval
        self.deadline = deadline

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return _PingStream(self.backend.connect_tcp(host, port, timeout, local_address, socket_options), self.interval, self.deadline)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return _PingStream(self.backend.connect_unix_socket(path, timeout, socket_options), self.interval, self.deadline)

    def sleep(self, seconds):
        self.backend.sleep(seconds)


def _proxy_for(url):
    parsed = urlsplit(url)
    if urllib.request.proxy_bypass(parsed.hostname or ""):
        return None
    return urllib.request.getproxies().get(parsed.scheme)


def _pool_for(url, deadline):
    proxy = _proxy_for(url)
    # 每个长任务独占连接，取消或总超时不能连带中断同渠道其他并发任务。
    return httpcore.ConnectionPool(
        proxy=httpcore.Proxy(proxy) if proxy else None,
        http2=True, max_connections=1, max_keepalive_connections=0,
        retries=0, network_backend=_PingBackend(deadline=deadline),
    )


def _network_error(exc):
    if isinstance(exc, httpcore.TimeoutException):
        return TimeoutError("上游连接或流式读取超时")
    return ConnectionError("上游连接中断：" + type(exc).__name__)


class _Response:
    def __init__(self, context, response, url, pool, deadline):
        self.context = context
        self.response = response
        self.url = url
        self.pool = pool
        self.status = self.code = response.status
        self.headers = Message()
        for name, value in response.headers:
            self.headers[name.decode("ascii")] = value.decode("latin-1")
        self.iterator = iter(response.iter_stream())
        self.buffer = bytearray()
        self.http_version = response.extensions.get("http_version", b"").decode("ascii", "replace")
        self.closed = False
        self.deadline = deadline
        deadline.attach(response.extensions.get("network_stream"))

    def read1(self, size=-1):
        if self.deadline.expired:
            raise TimeoutError("上游请求超过总时限")
        if size == 0:
            return b""
        try:
            while not self.buffer:
                chunk = next(self.iterator, None)
                if self.deadline.expired:
                    raise TimeoutError("上游请求超过总时限")
                if chunk is None:
                    return b""
                self.buffer.extend(chunk)
        except httpcore.NetworkError as exc:
            if self.deadline.expired:
                raise TimeoutError("上游请求超过总时限") from exc
            raise _network_error(exc) from exc
        except (httpcore.TimeoutException, httpcore.ProtocolError) as exc:
            if self.deadline.expired:
                raise TimeoutError("上游请求超过总时限") from exc
            raise _network_error(exc) from exc
        count = len(self.buffer) if size < 0 else min(size, len(self.buffer))
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def read(self, size=-1):
        parts = []
        count = 0
        while size < 0 or count < size:
            part = self.read1(65536 if size < 0 else size - count)
            if not part:
                break
            parts.append(part)
            count += len(part)
        return b"".join(parts)

    def close(self):
        if not self.closed:
            self.closed = True
            self.deadline.cancel()
            try:
                self.context.__exit__(None, None, None)
            finally:
                self.pool.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def urlopen(request, timeout):
    """保留现有代理选择和 HTTPError 契约；整段正文始终逐块交付给原解析器。"""
    url = request.full_url
    headers = dict(request.header_items())
    headers.setdefault("User-agent", "RelayMonitor/1.0")
    headers["Accept-Encoding"] = "identity"
    headers["Host"] = urlsplit(url).netloc
    if request.data is not None:
        headers["Content-Length"] = str(len(request.data))
    started = time.monotonic()
    deadline = _Deadline(timeout)
    try:
        pool = _pool_for(url, deadline)
    except Exception:
        deadline.cancel()
        raise
    context = pool.stream(
        request.get_method(), url, headers=headers, content=request.data,
        extensions={"timeout": {name: timeout for name in ("connect", "read", "write", "pool")}},
    )
    try:
        raw = context.__enter__()
    except (httpcore.NetworkError, httpcore.TimeoutException, httpcore.ProtocolError) as exc:
        deadline.cancel()
        pool.close()
        if deadline.expired:
            raise TimeoutError("上游请求超过总时限") from exc
        raise _network_error(exc) from exc
    except Exception:
        deadline.cancel()
        pool.close()
        raise
    response = _Response(context, raw, url, pool, deadline)
    logger.info("上游流连接已建立：主机=%s，协议=%s，状态=%s，响应头耗时=%.3f秒，PING间隔=%s秒",
                urlsplit(url).hostname, response.http_version, response.status,
                time.monotonic() - started, PING_INTERVAL_SECONDS if response.http_version == "HTTP/2" else 0)
    if response.status >= 300:
        raise urllib.error.HTTPError(url, response.status, "上游 HTTP 错误", response.headers, response)
    return response
