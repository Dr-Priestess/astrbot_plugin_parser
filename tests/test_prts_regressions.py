"""Run in the real AstrBot SDK environment; all QQ sends and API calls are mocked."""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientError
import httpx
from PIL import Image

from core.clean import CacheCleaner
from core.data import Author, ImageContent, ParseResult, Platform, SendGroup, VideoContent
from core.emoji_source import StaticEmojiSource
from core.exception import DownloadException, ParseException
from core.parsers.twitter import TwitterParser
from core.render import Renderer
from core.sender import MessageSender


class ParserRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="parser-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.picture = self.folder / "picture.png"
        Image.new("RGBA", (64, 64), (255, 110, 30, 255)).save(self.picture)
        self.png = self.picture.read_bytes()
        self.cfg = SimpleNamespace(
            cache_dir=self.folder, single_heavy_render_card=True,
            forward_threshold=5, show_download_fail_tip=True, audio_to_file=False,
            proxy=None, parser=SimpleNamespace(twitter=SimpleNamespace(use_proxy=False)),
            emoji_cdn="https://assets.example.invalid/64/", emoji_style="FACEBOOK",
        )
        self.parser = object.__new__(TwitterParser)
        self.parser.cfg = self.cfg
        self.parser.headers = {}
        self.parser._session = None

        async def get_file(*args, **kwargs):
            return self.picture

        self.parser.downloader = SimpleNamespace(
            download_img=lambda *args, **kwargs: asyncio.create_task(get_file()),
            download_video=lambda *args, **kwargs: asyncio.create_task(get_file()),
        )
        self.parser._req_xdown_api = AsyncMock(return_value={"status": "ok", "data": None})
        self.parser._req_twitter_post = AsyncMock(return_value={
            "id": "123", "text": "完整正文 hello world 🥰 👩‍💻 🇨🇳 👍🏽 ❤️",
            "author": {"name": "作者 🥰", "screen_name": "someone"},
            "created_timestamp": 1790000000,
        })
        self.match = re.search(r"x\.com/[^/]+/status/\d+", "https://x.com/someone/status/123")

    async def parse(self):
        return await self.parser._parse(self.match)

    def event(self):
        return SimpleNamespace(
            chain_result=lambda segments: segments, send=AsyncMock(),
            get_self_id=lambda: "offline-bot",
        )

    def result(self, **kwargs):
        return ParseResult(
            platform=Platform("twitter", "推特"), author=Author("作者"), **kwargs,
        )

    async def test_text_only_null_media_preserves_author_body_and_unicode(self):
        result = await self.parse()
        self.assertEqual(result.author.name, "作者 🥰 (@someone)")
        self.assertIn("hello world 🥰 👩‍💻 🇨🇳 👍🏽 ❤️", result.text)
        self.assertIn("https://x.com/someone/status/123", result.text)
        self.assertEqual(result.timestamp, 1790000000)
        self.assertEqual(result.contents, [])
        self.assertTrue(result.send_groups[0].render_card)

    async def test_text_only_survives_download_service_outage(self):
        self.parser._req_xdown_api.side_effect = ClientError("offline")
        self.assertIn("完整正文", (await self.parse()).text)

    async def test_missing_media_is_not_misrepresented_as_text_only(self):
        post = self.parser._req_twitter_post.return_value
        post["media"] = {"photos": [{"url": "https://example.invalid/photo"}]}
        with self.assertRaises(ParseException):
            await self.parse()

    async def test_missing_media_rejected_even_with_title_only_html(self):
        self.parser._req_xdown_api.return_value = {"status": "ok", "data": "<h3>caption</h3>"}
        self.parser._req_twitter_post.return_value["media"] = {"videos": [{"url": "missing"}]}
        with self.assertRaises(ParseException):
            await self.parse()

    async def test_media_posts_use_full_body_and_timestamp(self):
        self.parser._req_xdown_api.return_value = {
            "status": "ok", "data": '<h3>short</h3><a class="abutton" href="https://example.invalid/video">下载 MP4</a>',
        }
        result = await self.parse()
        self.assertIsNone(result.title)
        self.assertIn("完整正文 hello world", result.text)
        self.assertEqual(result.timestamp, 1790000000)
        self.assertEqual(len(result.video_contents), 1)
        await result.video_contents[0].get_path()

    async def test_image_posts_explicitly_include_information_card(self):
        self.parser._req_xdown_api.return_value = {
            "status": "ok", "data": '<h3>caption</h3><a class="abutton" href="https://example.invalid/image">下载图片</a>',
        }
        result = await self.parse()
        self.assertTrue(result.send_groups[0].render_card)
        self.assertEqual(result.send_groups[0].contents, result.contents)
        await result.img_contents[0].get_path()

    async def test_metadata_outage_keeps_media_and_account_fallback(self):
        self.parser._req_twitter_post.side_effect = ClientError("offline")
        self.parser._req_xdown_api.return_value = {
            "status": "ok", "data": '<h3>hello 🥰 world</h3><a class="abutton" href="https://example.invalid/video">下载 MP4</a>',
        }
        result = await self.parse()
        self.assertEqual(result.author.name, "@someone")
        self.assertEqual(result.title, "hello 🥰 world")
        await result.video_contents[0].get_path()

    async def test_i_status_does_not_claim_i_is_author(self):
        self.parser._req_twitter_post.return_value["author"] = {}
        self.match = re.search(r"x\.com/[^/]+/status/\d+", "https://x.com/i/status/123")
        self.assertEqual((await self.parse()).author.name, "作者信息暂不可用")

    async def test_disabled_card_switch_keeps_text_fallback(self):
        self.cfg.single_heavy_render_card = False
        self.assertEqual((await self.parse()).send_groups, [])

    def test_inline_html_emoji_and_spaces_survive_extraction(self):
        result = self.parser.parse_twitter_html('<h3>hello <img alt="🥰" src="x"> world<br>second line</h3>')
        self.assertEqual(result.title, "hello 🥰 world\nsecond line")

    async def test_avatar_failure_is_optional(self):
        self.parser.downloader.download_img = AsyncMock(side_effect=DownloadException())
        self.assertIsNone(await self.parser._author_avatar("https://pbs.twimg.com/avatar.png"))

    async def test_avatar_host_must_match_exactly(self):
        self.parser.downloader.download_img = Mock(side_effect=AssertionError("should not download"))
        self.assertIsNone(await self.parser._author_avatar("https://pbs.twimg.com.attacker.invalid/avatar.png"))
        self.assertIsNone(await self.parser._author_avatar("http://pbs.twimg.com/avatar.png"))

    async def test_post_id_validation_and_cookie_isolation(self):
        response = AsyncMock()
        response.__aenter__.return_value = response
        response.raise_for_status = Mock()
        response.json.return_value = {"code": 200, "tweet": {"id": "999", "text": "wrong post"}}
        session = SimpleNamespace(closed=False, get=Mock(return_value=response))
        self.parser._session = session
        self.parser.headers["cookie"] = "private-downloader-cookie"
        post = await TwitterParser._req_twitter_post(self.parser, "https://x.com/someone/status/123")
        self.assertEqual(post, {})
        self.assertNotIn("cookie", {key.lower() for key in session.get.call_args.kwargs["headers"]})

    async def test_text_card_is_sent_once(self):
        result = await self.parse()
        event = self.event()
        renderer = SimpleNamespace(render_card=AsyncMock(return_value=self.picture))
        await MessageSender(self.cfg, renderer).send_parse_result(event, result)
        self.assertEqual(event.send.await_count, 1)
        self.assertEqual([type(s).__name__ for s in event.send.await_args.args[0]], ["Image"])

    async def test_failed_text_card_falls_back_to_readable_body_once(self):
        result = await self.parse()
        event = self.event()
        renderer = SimpleNamespace(render_card=AsyncMock(return_value=None))
        await MessageSender(self.cfg, renderer).send_parse_result(event, result)
        self.assertEqual(event.send.await_count, 1)
        text = event.send.await_args.args[0][0].text
        self.assertIn("作者 🥰 (@someone)", text)
        self.assertIn("hello world 🥰", text)

    def test_upstream_merged_gallery_card_rule_preserved(self):
        self.cfg.single_heavy_render_card = False
        sender = MessageSender(self.cfg, None)
        result = self.result(contents=[ImageContent(self.picture) for _ in range(5)])
        plan = sender._build_send_plan(result)
        self.assertTrue(plan["force_merge"])
        self.assertTrue(plan["render_card"])
        self.assertFalse(plan["preview_card"])

    def test_emoji_filenames_preserve_joiners_flags_and_skin_tones(self):
        for text, filename in [
            ("😀", "1f600.png"), ("👩‍💻", "1f469-200d-1f4bb.png"),
            ("🇨🇳", "1f1e8-1f1f3.png"), ("👍🏽", "1f44d-1f3fd.png"),
        ]:
            self.assertEqual(StaticEmojiSource.filenames(text)[0], filename)
        self.assertEqual(StaticEmojiSource.filenames("❤️"), ["2764-fe0f.png", "2764.png"])

    async def test_static_emoji_fetch_downloads_png_and_reuses_cache(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, content=self.png)
        source = StaticEmojiSource(base_url=self.cfg.emoji_cdn, cache_dir=self.folder / "emoji")
        with patch.object(source, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))):
            result = await source.fetch_emojis({"😀", "👩‍💻"})
        self.assertTrue(all(path.is_file() for path in result.values()))
        self.assertEqual({request.url.path for request in requests}, {"/64/1f600.png", "/64/1f469-200d-1f4bb.png"})
        self.assertTrue(all(not request.url.query for request in requests))
        with patch.object(source, "_client", side_effect=AssertionError("cache miss")):
            cached = await source.fetch_emojis({"😀", "👩‍💻"})
        self.assertEqual(cached, result)

    async def test_emoji_variation_selector_has_static_asset_fallback(self):
        requested = []
        def respond(request):
            requested.append(request.url.path)
            return httpx.Response(404) if "fe0f" in request.url.path else httpx.Response(200, content=self.png)
        source = StaticEmojiSource(base_url=self.cfg.emoji_cdn, cache_dir=self.folder / "emoji")
        with patch.object(source, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))):
            result = await source.fetch_emojis({"❤️"})
        self.assertTrue(result["❤️"].is_file())
        self.assertEqual(requested, ["/64/2764-fe0f.png", "/64/2764.png"])

    async def test_emoji_download_rejects_non_image_and_leaves_no_partial_cache(self):
        source = StaticEmojiSource(base_url=self.cfg.emoji_cdn, cache_dir=self.folder / "emoji")
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not an image"))
        with patch.object(source, "_client", lambda: httpx.AsyncClient(transport=transport)):
            self.assertIsNone((await source.fetch_emojis({"😀"}))["😀"])
        self.assertEqual(list((self.folder / "emoji").rglob("*.png")), [])
        self.assertEqual(list((self.folder / "emoji").rglob("*.tmp")), [])

    async def test_emoji_fetch_supports_renderer_postprocessing(self):
        source = StaticEmojiSource(base_url=self.cfg.emoji_cdn, cache_dir=self.folder / "emoji")
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=self.png))
        with patch.object(source, "_client", lambda: httpx.AsyncClient(transport=transport)):
            output = await source.fetch_emojis({"😀"}, post_process=lambda path: path.stat().st_size)
        self.assertEqual(output["😀"], len(self.png))

    def test_wrapping_keeps_compound_emoji_whole(self):
        Renderer.load_resources()
        renderer = Renderer(self.cfg)
        font = renderer.fontset.text_font
        for emoji in ["❤️", "👩‍💻", "🇨🇳", "👍🏽", "👨‍👩‍👧‍👦"]:
            text = "A" + emoji + "B"
            width = font.font.size + font.get_char_width_fast("A") - 1
            wrapped = renderer._wrap_text(text, width, font)
            self.assertEqual("".join(wrapped), text)
            self.assertTrue(any(emoji in line for line in wrapped), wrapped)

    async def test_cleaner_preserves_root_and_external_symlink_target(self):
        cache = self.folder / "cache"
        cache.mkdir()
        (cache / "old.png").write_bytes(self.png)
        outside = self.folder / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        try:
            (cache / "external-link").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        inode = cache.stat().st_ino
        cleaner = object.__new__(CacheCleaner)
        cleaner.cfg = SimpleNamespace(cache_dir=cache)
        await cleaner._clean_plugin_cache()
        self.assertEqual(cache.stat().st_ino, inode)
        self.assertEqual(list(cache.iterdir()), [])
        self.assertTrue((outside / "keep").is_file())

    @unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "POSIX ACL tools unavailable")
    async def test_cleaner_keeps_default_gateway_read_acl(self):
        cache = self.folder / "acl-cache"
        cache.mkdir()
        subprocess.run(["setfacl", "-m", "u:995:r-x,d:u:995:r-x", str(cache)], check=True)
        before = subprocess.check_output(["getfacl", "-ncp", str(cache)], text=True)
        cleaner = object.__new__(CacheCleaner)
        cleaner.cfg = SimpleNamespace(cache_dir=cache)
        await cleaner._clean_plugin_cache()
        self.assertEqual(subprocess.check_output(["getfacl", "-ncp", str(cache)], text=True), before)
        new_file = cache / "new-video.mp4"
        new_file.write_bytes(b"offline")
        acl = subprocess.check_output(["getfacl", "-ncp", str(new_file)], text=True)
        self.assertIn("user:995:r-x", acl)
        self.assertIn("#effective:r--", acl)


if __name__ == "__main__":
    unittest.main()
