"""Real aiohttp requests against an offline CDN fixture, never public media."""
from __future__ import annotations

from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from aiohttp.abc import AbstractResolver
from aiohttp.test_utils import TestServer
from yarl import URL

from core.download import Downloader
from core.utils import generate_file_name


class LoopbackResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [{"hostname": host, "host": "127.0.0.1", "port": port,
                 "family": socket.AF_INET, "proto": 0, "flags": 0}]

    async def close(self):
        pass


class SignedMediaUrlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw_paths = []
        self.expected = None
        self.payload = b"RIFF\x10\x00\x00\x00WEBPfixture-body"

        async def image(request):
            self.raw_paths.append(request.raw_path)
            if self.expected is not None and request.raw_path != self.expected:
                return web.Response(status=403)
            return web.Response(body=self.payload, content_type="image/webp")

        app = web.Application()
        app.router.add_get("/{path:.*}", image)
        self.server = TestServer(app)
        await self.server.start_server()
        self.downloader = Downloader(SimpleNamespace(cache_dir=Path(self.temp.name), source_max_size=1,
                                                    download_timeout=3, download_retry_times=0))
        await self.downloader.close()
        self.downloader.client = ClientSession(connector=TCPConnector(resolver=LoopbackResolver()),
                                               timeout=ClientTimeout(total=3))

    async def asyncTearDown(self):
        await self.downloader.close()
        await self.server.close()

    def url(self, host="p3-sign.douyinpic.com", path="/image~tplv-dy:1440:1718:q80.webp", query=None):
        query = query or "x-expires=1999999999&x-signature=abc%2Fdef%2Bghi%3D&key=%30&tag=a&tag=b"
        return f"http://{host}:{self.server.port}{path}?{query}"

    async def test_signed_image_reaches_server_without_requoting_and_uses_original_cache_key(self):
        url = self.url()
        self.expected = URL(url, encoded=True).raw_path_qs
        self.assertNotEqual(URL(url).raw_path_qs, self.expected)
        path = await self.downloader.download_img(url, proxy=None)
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertEqual(path.name, generate_file_name(url, ".jpg"))
        self.assertEqual(self.raw_paths, [self.expected])
        self.assertEqual(await self.downloader.download_img(url, proxy=None), path)
        self.assertEqual(len(self.raw_paths), 1)

    async def test_ordinary_signed_url_still_canonicalizes_unicode_and_spaces(self):
        url = self.url(host="media.example.invalid", path="/普通 图片.webp")
        self.expected = URL(url).raw_path_qs
        path = await self.downloader.download_img(url, proxy=None)
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertEqual(self.raw_paths, [self.expected])

    async def test_lookalike_domain_does_not_get_the_douyin_exception(self):
        url = self.url(host="p3-sign.douyinpic.com.attacker.invalid")
        self.expected = URL(url).raw_path_qs
        await self.downloader.download_img(url, proxy=None)
        self.assertEqual(self.raw_paths, [self.expected])

    async def test_unsigned_douyin_url_keeps_regular_canonicalization(self):
        url = self.url(query="key=%30&text=%E4%BD%A0%E5%A5%BD", path="/未签名 图片.webp")
        self.expected = URL(url).raw_path_qs
        await self.downloader.download_img(url, proxy=None)
        self.assertEqual(self.raw_paths, [self.expected])
