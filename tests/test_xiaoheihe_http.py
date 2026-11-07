"""真实 curl_cffi + 本地 HTTP 服务，只使用虚拟 Cookie，不访问外部网站。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.exception import ParseException, TipException
from core.parsers.xiaoheihe import XiaoheiheParser


class XiaoheiheHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        self.payload = {"status": "ok", "result": {}}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append({"cookies": self.headers.get_all("Cookie"), "path": self.path})
                self.send_response(owner.status)
                if self.path == "/redirect":
                    self.send_header("Location", "/external")
                self.end_headers()
                self.wfile.write(json.dumps(owner.payload).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.parser = object.__new__(XiaoheiheParser)
        self.parser.headers = {"User-Agent": "synthetic-client"}
        self.parser.mycfg = SimpleNamespace(cookies="heybox_id=synthetic-user; pkey=auth=value; x_xhh_tokenid=Bdevice")
        self.parser.cfg = SimpleNamespace(common_timeout=3, proxy=None, parser=SimpleNamespace(xiaoheihe=SimpleNamespace(use_proxy=False)))

    async def test_single_cookie_header_contains_login_and_device(self):
        # 为本地回环地址临时开启虚拟凭证；生产只允许 HTTPS 小黑盒域。
        with patch.object(self.parser, "_is_auth_url", return_value=True):
            await self.parser._request_json("GET", self.url, cookies={"x_xhh_tokenid": "Bnew-device"}, headers={"cookie": "stale=bad"})
        cookies = self.requests[0]["cookies"]
        self.assertEqual(len(cookies), 1)
        self.assertIn("pkey=auth=value", cookies[0])
        self.assertIn("heybox_id=synthetic-user", cookies[0])
        self.assertEqual(cookies[0].count("x_xhh_tokenid="), 1)
        self.assertIn("x_xhh_tokenid=Bnew-device", cookies[0])
        self.assertNotIn("stale=", cookies[0])

    def test_cookie_is_never_attached_to_cdn_fingerprint_or_similar_domain(self):
        for url in ("https://fp-it.portal101.cn/deviceprofile/v4", "https://cdn.example.invalid/a.png",
                    "https://api.xiaoheihe.cn.attacker.invalid", "http://api.xiaoheihe.cn/"):
            with self.subTest(url=url):
                headers = self.parser._request_headers(url, {"Cookie": "old=private"}, {"x_xhh_tokenid": "Bnew"})
                self.assertNotIn("cookie", {k.lower() for k in headers})
        self.assertIn("Cookie", self.parser._request_headers("https://api.xiaoheihe.cn/"))

    async def test_cookie_changes_are_read_on_next_request(self):
        self.parser.mycfg.cookies = "Cookie: pkey=updated;\nx_xhh_tokenid=Bupdated"
        self.parser._fetch_xhh_tokenid = AsyncMock(side_effect=AssertionError("must reuse configured device"))
        ctx = await self.parser._build_request_context()
        self.assertEqual(ctx, {"x_xhh_tokenid": "Bupdated", "device_id": "updated"})
        self.assertIn("pkey=updated", self.parser._request_headers("https://api.xiaoheihe.cn/")["Cookie"])
        self.parser.mycfg.cookies = ""
        self.assertNotIn("Cookie", self.parser._request_headers("https://api.xiaoheihe.cn/"))

    async def test_captcha_reports_required_browser_verification_without_retry(self):
        self.payload = {"status": "show_captcha"}
        with self.assertRaisesRegex(TipException, "show_captcha"):
            await self.parser._request_json("GET", self.url)
        self.assertEqual(len(self.requests), 1)

    async def test_http_failures_are_not_treated_as_success(self):
        for status in (401, 403, 429, 503):
            with self.subTest(status=status):
                self.status = status
                with self.assertRaisesRegex(ParseException, str(status)):
                    await self.parser._request_json("GET", self.url)

    async def test_redirect_does_not_carry_cookie_to_external_destination(self):
        self.status = 302
        original_request_headers = self.parser._request_headers
        def request_headers(url, headers=None, cookies=None):
            with patch.object(self.parser, "_is_auth_url", return_value=url.endswith("/redirect")):
                return original_request_headers(url, headers, cookies)
        with patch.object(self.parser, "_request_headers", side_effect=request_headers):
            # 最后一跳返回 200；handler 在发送响应时读取 status。
            original_handler = self.server.RequestHandlerClass.do_GET
            owner = self
            def handler(instance):
                if instance.path == "/external":
                    owner.status = 200
                return original_handler(instance)
            with patch.object(self.server.RequestHandlerClass, "do_GET", handler):
                await self.parser._request_text(self.url + "/redirect")
        self.assertIsNotNone(self.requests[0]["cookies"])
        self.assertIsNone(self.requests[1]["cookies"])

    async def test_json_endpoint_does_not_follow_redirects(self):
        self.status = 302
        with self.assertRaisesRegex(ParseException, "302"):
            await self.parser._request_json("GET", self.url + "/redirect")
        self.assertEqual(len(self.requests), 1)

    async def test_non_object_payload_is_rejected(self):
        self.payload = []
        with self.assertRaises(ParseException):
            await self.parser._request_json("GET", self.url)


if __name__ == "__main__":
    unittest.main()
