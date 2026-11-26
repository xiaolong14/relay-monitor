"""真实本地 HTTP/2 帧测试：标准 PING、原始正文、断流、超时和连接回收。"""

import json
import datetime
import ipaddress
import os
from pathlib import Path
import shutil
import subprocess
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import httpcore
from relay_monitor import stream_transport


class StreamTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("未安装 Node.js")
        cls.process = subprocess.Popen(
            ["node", str(Path(__file__).with_name("h2_transport_server.cjs"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        cls.port = int(cls.process.stdout.readline().strip())

    @classmethod
    def tearDownClass(cls):
        cls.process.communicate(timeout=5)

    def setUp(self):
        def create_pool(_url, deadline):
            pool = httpcore.ConnectionPool(http1=False, http2=True,
                network_backend=stream_transport._PingBackend(interval=0.03, deadline=deadline))
            self.addCleanup(pool.close)
            return pool
        target = patch.object(stream_transport, "_pool_for", side_effect=create_pool)
        target.start()
        self.addCleanup(target.stop)

    def request(self, path="/stream", timeout=2):
        return stream_transport.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data='{"原始":"正文"}'.encode(),
            headers={"Content-Type": "application/json"}, method="POST"), timeout=timeout)

    def test_ping_is_acknowledged_without_extra_http_request_or_body_change(self):
        with self.request() as response:
            self.assertEqual(response.http_version, "HTTP/2")
            self.assertEqual(response.headers.get("x-request-id"), "local-h2-test")
            body = json.loads(response.read())
            self.assertGreater(body["pings"], 0)
            self.assertEqual(body["method"], "POST")
            self.assertEqual(json.loads(body["body"]), {"原始": "正文"})

    def test_stream_reset_preserves_delivered_bytes_and_reports_connection_error(self):
        with self.request("/reset") as response:
            self.assertEqual(response.read1(), b"data: partial\n\n")
            with self.assertRaises(ConnectionError):
                response.read1()

    def test_ping_does_not_reset_read_timeout(self):
        with self.request("/hang", timeout=0.1) as response:
            with self.assertRaises(TimeoutError):
                response.read1()

    def test_ping_does_not_extend_response_headers_timeout(self):
        with self.assertRaises(TimeoutError):
            self.request("/headers-hang", timeout=0.1)

    def test_http_error_body_remains_available_for_parameter_compatibility(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/error")
        try:
            self.assertEqual(caught.exception.code, 429)
            self.assertEqual(json.loads(caught.exception.read())["method"], "POST")
        finally:
            caught.exception.close()

    def test_response_close_stops_ping_worker(self):
        with self.request() as response:
            response.read()
            network = response.response.extensions["network_stream"]
            self.assertTrue(network.worker.is_alive())
        network.worker.join(1)
        self.assertFalse(network.worker.is_alive())

    def test_node_transport_sends_ping_over_real_tls_and_preserves_cancellation(self):
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
        except ImportError:
            self.skipTest("本地 TLS 集成测试需要 cryptography")
        with tempfile.TemporaryDirectory() as folder:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                    .not_valid_after(now + datetime.timedelta(days=1))
                    .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
                    .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
            certificate = Path(folder) / "cert.pem"
            private_key = Path(folder) / "key.pem"
            certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            private_key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
            process = subprocess.Popen(["node", str(Path(__file__).with_name("h2_transport_server.cjs")),
                                        str(certificate), str(private_key)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                port = int(process.stdout.readline().strip())
                env = dict(os.environ, NODE_EXTRA_CA_CERTS=str(certificate))
                env.pop("NODE_TLS_REJECT_UNAUTHORIZED", None)
                check = subprocess.run(["node", str(Path(__file__).with_name("stream_transport_check.cjs")),
                                        f"https://127.0.0.1:{port}"], capture_output=True, text=True,
                                       encoding="utf-8", timeout=35, env=env)
                self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            finally:
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
