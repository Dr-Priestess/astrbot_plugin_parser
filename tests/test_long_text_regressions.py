"""发送层回归：真实 AstrBot 组件，所有 QQ 发送与媒体处理均隔离。"""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.core.message.components import Image, Nodes, Plain, Video

from core.data import Author, GraphicsContent, ImageContent, ParseResult, Platform, SendGroup, TextContent, VideoContent
from core.exception import DownloadException
from core.render import Renderer
from core.sender import MessageSender


class LongTextRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.image = self.folder / "image.png"
        from PIL import Image as PILImage
        PILImage.new("RGB", (80, 50), "white").save(self.image)
        self.cfg = SimpleNamespace(
            cache_dir=self.folder, single_heavy_render_card=False, forward_threshold=10,
            text_forward_threshold=500, show_download_fail_tip=True, audio_to_file=False,
            proxy=None, emoji_cdn="https://example.invalid/emoji", emoji_style="FACEBOOK",
        )
        self.renderer = SimpleNamespace(render_card=AsyncMock(return_value=None))
        self.sender = MessageSender(self.cfg, self.renderer)
        self.event = SimpleNamespace(get_self_id=lambda: "offline-bot", chain_result=lambda c: c, send=AsyncMock())

    def result(self, **kwargs):
        return ParseResult(platform=Platform("bilibili", "B站"), **kwargs)

    @staticmethod
    def plain_text(chain):
        return "".join(
            seg.text for item in chain
            for seg in ([s for node in item.nodes for s in node.content] if isinstance(item, Nodes) else [item])
            if isinstance(seg, Plain)
        )

    async def send_text(self, text):
        result = self.result(contents=[TextContent(text)])
        result.send_groups = [SendGroup(result.contents, force_merge=False, render_card=False)]
        await self.sender.send_parse_result(self.event, result)
        return self.event.send.await_args.args[0]

    async def test_500_characters_stay_direct_and_501_fold(self):
        chain = await self.send_text("文" * 500)
        self.assertIsInstance(chain[0], Plain)
        self.event.send.reset_mock()
        chain = await self.send_text("文" * 501)
        self.assertIsInstance(chain[0], Nodes)
        self.assertEqual(self.plain_text(chain), "文" * 501)

    async def test_existing_configuration_defaults_to_500(self):
        del self.cfg.text_forward_threshold
        self.assertIsInstance((await self.send_text("文" * 501))[0], Nodes)

    async def test_threshold_is_configurable_and_can_be_disabled(self):
        for threshold in (1000, 0):
            with self.subTest(threshold=threshold):
                self.cfg.text_forward_threshold = threshold
                self.assertIsInstance((await self.send_text("文" * 600))[0], Plain)

    async def test_article_without_media_folds_and_preserves_complete_fallback(self):
        text = ("公告正文 保留空格 🥰\n" * 300) + "最后一行"
        result = self.result(title="更新公告", text=text)
        expected = "\n".join((result.header, text))
        await self.sender.send_parse_result(self.event, result)
        self.assertEqual(self.event.send.await_count, 1)
        chain = self.event.send.await_args.args[0]
        self.assertIsInstance(chain[0], Nodes)
        self.assertEqual(self.plain_text(chain), expected)
        self.assertTrue(all(len(seg.text) <= 1600 for node in chain[0].nodes for seg in node.content if isinstance(seg, Plain)))

    async def test_successful_long_article_card_is_compact_and_does_not_replace_original(self):
        self.renderer.render_card.return_value = self.image
        result = self.result(title="标题", text="段落内容\n" * 400)
        original = result.text
        await self.sender.send_parse_result(self.event, result)
        preview = self.renderer.render_card.await_args.args[0]
        self.assertIsNot(preview, result)
        self.assertLess(len(preview.text), 200)
        self.assertIn("全文见聊天记录", preview.extra["info"])
        self.assertEqual(result.text, original)
        self.assertIn(original, self.plain_text(self.event.send.await_args.args[0]))
        self.assertEqual(self.event.send.await_count, 1)

    async def test_image_caption_over_threshold_preserves_text_alongside_media(self):
        result = self.result(text="长图说明 " * 150, contents=[ImageContent(self.image)])
        await self.sender.send_parse_result(self.event, result)
        chain = self.event.send.await_args.args[0]
        self.assertIsInstance(chain[0], Nodes)
        self.assertIn(result.text, self.plain_text(chain))
        self.assertTrue(any(isinstance(seg, Image) for node in chain[0].nodes for seg in node.content))

    async def test_aggregate_text_and_graphics_also_fold(self):
        contents = [TextContent("甲" * 260), GraphicsContent(self.image, text="乙" * 250, alt="原图")]
        result = self.result(contents=contents, send_groups=[SendGroup(contents, force_merge=False, render_card=False)])
        await self.sender.send_parse_result(self.event, result)
        chain = self.event.send.await_args.args[0]
        self.assertIsInstance(chain[0], Nodes)
        self.assertEqual(self.plain_text(chain), "甲" * 260 + "乙" * 250 + "原图")

    async def test_following_video_group_does_not_repeat_or_merge_article(self):
        text = "独立正文" * 200
        contents = [TextContent(text), VideoContent(self.image)]
        result = self.result(text=text, contents=contents, send_groups=[
            SendGroup(contents[:1], render_card=False), SendGroup(contents[1:], force_merge=False),
        ])
        with patch("core.sender.transcode_video_audio_to_lc", AsyncMock(return_value=self.image)):
            await self.sender.send_parse_result(self.event, result)
        chains = [call.args[0] for call in self.event.send.await_args_list]
        self.assertEqual(len(chains), 2)
        self.assertIsInstance(chains[0][0], Nodes)
        self.assertEqual(self.plain_text(chains[0]), text)
        self.assertIsInstance(chains[1][0], Video)

    async def test_failed_graphics_download_still_preserves_long_text(self):
        async def fail():
            raise DownloadException()
        text = "图片失败也保留原文 " * 100
        content = GraphicsContent(asyncio.create_task(fail()), text=text, alt="原图说明")
        result = self.result(contents=[content], send_groups=[SendGroup([content], render_card=False)])
        await self.sender.send_parse_result(self.event, result)
        chain = self.event.send.await_args.args[0]
        self.assertIsInstance(chain[0], Nodes)
        self.assertIn(text, self.plain_text(chain))
        self.assertIn("原图说明", self.plain_text(chain))

    async def test_short_text_card_still_sends_once_without_duplicate_plain(self):
        self.renderer.render_card.return_value = self.image
        result = self.result(text="短消息", send_groups=[SendGroup(render_card=True)])
        await self.sender.send_parse_result(self.event, result)
        self.assertEqual(self.event.send.await_count, 1)
        self.assertIsInstance(self.event.send.await_args.args[0][0], Image)

    def test_splitting_preserves_every_space_newline_and_emoji(self):
        text = ("English 中文 👩‍💻 🇨🇳\n\n" * 200) + "尾行 "
        self.assertEqual("".join(self.sender._split_text(text)), text)

    def test_hard_split_does_not_break_compound_emoji_between_nodes(self):
        for emoji in ("👩‍💻", "👨‍👩‍👧‍👦", "🇨🇳", "👍🏽", "❤️"):
            text = "文" * 1599 + emoji + "尾" * 10
            with self.subTest(emoji=emoji):
                parts = self.sender._split_text(text)
                self.assertEqual("".join(parts), text)
                self.assertTrue(any(emoji in part for part in parts))

    async def test_failed_avatar_and_one_gallery_image_do_not_destroy_card(self):
        Renderer.load_resources()
        async def fail():
            raise DownloadException()
        avatar = asyncio.create_task(fail())
        picture = asyncio.create_task(fail())
        result = self.result(author=Author("很长的中文作者名字 English User", avatar), title="仍可阅读的卡片",
                             contents=[ImageContent(picture), ImageContent(self.image)])
        renderer = Renderer(self.cfg)
        path = await renderer.render_card(result)
        self.assertIsNotNone(path)
        from PIL import Image as PILImage
        with PILImage.open(path) as image:
            image.verify()


if __name__ == "__main__":
    unittest.main()
