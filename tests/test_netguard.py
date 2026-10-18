"""SSRF guard: initial URLs and redirect hops."""

import http.server
import logging
import socketserver
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import netguard


class HostGuardTest(unittest.TestCase):
    def test_rejects_empty_and_internal_names(self):
        self.assertIsNotNone(netguard.host_error(""))
        self.assertIsNotNone(netguard.host_error("localhost"))
        self.assertIsNotNone(netguard.host_error("metadata.google.internal"))
        self.assertIsNotNone(netguard.host_error("printer.local"))
        self.assertIsNotNone(netguard.host_error("router.lan"))
        self.assertIsNotNone(netguard.host_error("singlelabel"))

    def test_rejects_literal_private_and_link_local(self):
        for host in ("127.0.0.1", "10.0.0.1", "192.168.0.1", "169.254.169.254", "::1"):
            self.assertIsNotNone(netguard.host_error(host), host)

    def test_rejects_when_resolution_points_private(self):
        with mock.patch.object(netguard, "resolve_host", return_value=["10.0.0.1"]):
            self.assertIsNotNone(netguard.host_error("example.com"))

    def test_rejects_if_any_address_is_private(self):
        with mock.patch.object(netguard, "resolve_host", return_value=["93.184.216.34", "169.254.169.254"]):
            self.assertIsNotNone(netguard.host_error("example.com"))

    def test_allows_public(self):
        with mock.patch.object(netguard, "resolve_host", return_value=["93.184.216.34"]):
            self.assertIsNone(netguard.host_error("example.com"))

    def test_fails_closed_when_dns_fails(self):
        with mock.patch.object(netguard, "resolve_host", return_value=None):
            self.assertIsNotNone(netguard.host_error("example.com"))

    def test_validate_url_scheme_and_shape(self):
        self.assertIsNotNone(netguard.validate_url(""))
        self.assertIsNotNone(netguard.validate_url("ftp://example.com/x"))
        self.assertIsNotNone(netguard.validate_url("http://"))
        self.assertIsNotNone(netguard.validate_url("http://" + "a" * 3000))

    def test_validate_url_blocks_metadata(self):
        self.assertIsNotNone(netguard.validate_url("http://169.254.169.254/latest/meta-data/"))


class UrllibRedirectGuardTest(unittest.TestCase):
    def setUp(self):
        netguard.install_urllib_redirect_guard()
        from yt_dlp.networking import _urllib

        self.handler = _urllib.RedirectHandler()
        self.request = urllib.request.Request("http://public.example/start")

    def test_blocks_redirect_to_private(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.handler.redirect_request(
                self.request, None, 302, "Found", {}, "http://169.254.169.254/")
        self.assertIn("blocked", str(ctx.exception).lower())

    def test_follows_redirect_to_public(self):
        with mock.patch.object(netguard, "host_error", return_value=None):
            result = self.handler.redirect_request(
                self.request, None, 302, "Found", {}, "http://cdn.example/video")
        self.assertEqual(result.full_url, "http://cdn.example/video")


class _Handler(http.server.BaseHTTPRequestHandler):
    target = "http://169.254.169.254/"
    hits = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path == "/start":
            self.send_response(302)
            self.send_header("Location", self.target)
            self.end_headers()
        else:
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class GuardInstallResultTest(unittest.TestCase):
    def test_urllib_guard_install_succeeds_and_is_idempotent(self):
        self.assertTrue(netguard.install_urllib_redirect_guard())
        self.assertTrue(netguard.install_urllib_redirect_guard())

    def test_urllib_guard_install_failure_returns_false(self):
        from yt_dlp.networking import _urllib

        with mock.patch.object(_urllib, "RedirectHandler", None):
            self.assertFalse(netguard.install_urllib_redirect_guard())

    def _ydl(self):
        import yt_dlp

        return yt_dlp.YoutubeDL({"quiet": True, "logger": logging.getLogger("test")})

    def test_curl_guard_install_succeeds_and_is_idempotent(self):
        ydl = self._ydl()
        if "CurlCFFI" not in ydl._request_director.handlers:
            self.skipTest("curl_cffi handler not available")
        self.assertTrue(netguard.install_ydl_redirect_guard(ydl))
        self.assertTrue(netguard.install_ydl_redirect_guard(ydl))

    def test_curl_guard_returns_false_when_director_unavailable(self):
        class FakeYDL:
            pass

        self.assertFalse(netguard.install_ydl_redirect_guard(FakeYDL()))

    def test_curl_guard_returns_false_when_handler_absent(self):
        ydl = self._ydl()
        ydl._request_director.handlers.pop("CurlCFFI", None)
        self.assertFalse(netguard.install_ydl_redirect_guard(ydl))

    def test_curl_guard_returns_false_when_internals_are_unusable(self):
        class Handler:
            _get_instance = None  # present but not callable
            _send = None

        class Director:
            handlers = {"CurlCFFI": Handler()}

        class FakeYDL:
            _request_director = Director()

        self.assertFalse(netguard.install_ydl_redirect_guard(FakeYDL()))


class CurlCffiRedirectGuardTest(unittest.TestCase):
    def setUp(self):
        try:
            import yt_dlp
        except ImportError:  # pragma: no cover
            self.skipTest("yt-dlp not installed")

        self.server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

        self.ydl = yt_dlp.YoutubeDL({"quiet": True, "logger": logging.getLogger("test")})
        if "CurlCFFI" not in self.ydl._request_director.handlers:
            self.skipTest("curl_cffi handler not available")
        netguard.install_ydl_redirect_guard(self.ydl)
        self.handler = self.ydl._request_director.handlers["CurlCFFI"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _send(self, target):
        from yt_dlp.networking.common import Request

        _Handler.target = target
        return self.handler.send(Request(f"http://127.0.0.1:{self.port}/start"))

    def test_blocks_redirect_to_private(self):
        from yt_dlp.networking.exceptions import RequestError

        with self.assertRaises(RequestError):
            self._send("http://127.0.0.1:9/blocked")

    def test_follows_redirect_when_public(self):
        with mock.patch.object(netguard, "host_error", return_value=None):
            response = self._send(f"http://127.0.0.1:{self.port}/final")
        self.assertEqual(response.status, 200)


class UrllibHandlerIntegrationTest(unittest.TestCase):
    """The urllib opener must abort the hop, not just report an error."""

    def setUp(self):
        import yt_dlp

        netguard.install_urllib_redirect_guard()
        self.server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        _Handler.hits = []
        _Handler.target = f"http://127.0.0.1:{self.port}/internal-only"
        self.ydl = yt_dlp.YoutubeDL({"quiet": True, "logger": logging.getLogger("test")})
        self.handler = self.ydl._request_director.handlers["Urllib"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_redirect_to_private_is_not_fetched(self):
        from yt_dlp.networking.common import Request

        with mock.patch.object(
                netguard, "host_error",
                side_effect=lambda host: "blocked" if host == "127.0.0.1" else None):
            with self.assertRaises(Exception):
                self.handler.send(Request(f"http://127.0.0.1:{self.port}/start"))

        self.assertIn("/start", _Handler.hits)
        self.assertNotIn("/internal-only", _Handler.hits)


if __name__ == "__main__":
    unittest.main()
