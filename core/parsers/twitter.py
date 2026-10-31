import asyncio
import re
from itertools import chain
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout
from bs4 import BeautifulSoup, Tag

from astrbot.api import logger

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import ParseResult, Platform, SendGroup
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle


class TwitterParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="twitter", display_name="推特")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.twitter
        self.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://xdown.app",
                "Referer": "https://xdown.app/",
            }
        )
        self.xdown_url = "https://xdown.app/api/ajaxSearch"
        self.cookiejar = CookieJar(config, self.mycfg, domain="xdown.app")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    async def _req_xdown_api(self, url: str) -> dict[str, Any]:
        async with self.session.post(
            url=self.xdown_url,
            data={"q": url, "lang": "zh-cn"},
            headers=self.headers,
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"xdown API {resp.status} {resp.reason}")
            return await resp.json()

    @handle(
        "twitter.com",
        (
            r"(?<![A-Za-z0-9.-])(?:(?:www|mobile)\.)?twitter\.com/"
            r"(?:[A-Za-z0-9_]+/)*status/\d+"
        ),
    )
    @handle(
        "x.com",
        (
            r"(?<![A-Za-z0-9.-])(?:www\.)?x\.com/"
            r"(?:[A-Za-z0-9_]+/)*status/\d+"
        ),
    )
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        # 从匹配对象中获取原始URL
        url = f"https://{searched.group(0)}"
        media_response, post = await asyncio.gather(
            self._req_xdown_api(url), self._req_twitter_post(url),
            return_exceptions=True,
        )
        if not isinstance(post, dict):
            post = {}
        html = (
            media_response.get("data")
            if isinstance(media_response, dict) and media_response.get("status") == "ok"
            else None
        )
        result = self.parse_twitter_html(html) if isinstance(html, str) and html.strip() else None
        body = post.get("text")
        body = body.strip() if isinstance(body, str) else ""
        media = post.get("media")
        has_media = isinstance(media, dict) and any(
            media.get(key) for key in ("all", "photos", "videos", "mosaic")
        )
        if result is None or not result.contents:
            if body and not has_media:
                result = self.result(text=body)
            elif result is None or has_media:
                raise ParseException("解析失败，未能获取推文正文或媒体")

        author = post.get("author")
        author = author if isinstance(author, dict) else {}
        handle_match = re.search(r"(?:twitter|x)\.com/([A-Za-z0-9_]+)/status/", url)
        fallback_handle = handle_match.group(1) if handle_match else ""
        if fallback_handle.lower() == "i":
            fallback_handle = ""
        screen_name = author.get("screen_name")
        screen_name = screen_name.strip() if isinstance(screen_name, str) else fallback_handle
        screen_name = screen_name or fallback_handle
        name = author.get("name")
        name = name.strip() if isinstance(name, str) else ""
        display_name = (
            f"{name} (@{screen_name})" if name and screen_name
            else name or (f"@{screen_name}" if screen_name else "作者信息暂不可用")
        )
        result.author = self.create_author(display_name)
        avatar_url = author.get("avatar_url")
        if isinstance(avatar_url, str) and avatar_url:
            result.author.avatar = asyncio.create_task(self._author_avatar(avatar_url))
        timestamp = post.get("created_timestamp")
        if isinstance(timestamp, int):
            result.timestamp = timestamp
        if body:
            # Use complete Unicode text for media posts as well as text-only posts.
            result.title = None
            result.text = body
        result.url = url
        result.text = f"{result.text}\n\n{url}" if result.text else url
        if self.cfg.single_heavy_render_card:
            # Image posts also need the author/caption information card.
            result.send_groups = [SendGroup(contents=list(result.contents), render_card=True)]
        return result

    async def _req_twitter_post(self, url: str) -> dict[str, Any]:
        """Fetch public metadata without forwarding the downloader's cookies."""
        status_id = url.rsplit("/", 1)[-1]
        try:
            async with self.session.get(
                f"https://api.fxtwitter.com/status/{status_id}",
                headers={"Accept": "application/json", "User-Agent": "PRTS-Parser/1.6.1"},
                timeout=ClientTimeout(total=8),
            ) as response:
                response.raise_for_status()
                payload = await response.json()
            if not isinstance(payload, dict) or payload.get("code") != 200:
                return {}
            post = payload.get("tweet")
            if not isinstance(post, dict) or str(post.get("id")) != status_id:
                return {}
            return post
        except (ClientError, TimeoutError, ValueError) as error:
            logger.warning(f"[twitter] 推文信息获取失败，保留媒体解析: {type(error).__name__}")
            return {}

    async def _author_avatar(self, url: str) -> Path | None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "pbs.twimg.com":
            return None
        try:
            return await asyncio.wait_for(
                self.downloader.download_img(
                    url, headers={"User-Agent": "Mozilla/5.0"}, proxy=self.proxy,
                ), timeout=8,
            )
        except (ClientError, TimeoutError, ParseException, OSError) as error:
            logger.warning(f"[twitter] 头像获取失败，使用默认头像: {type(error).__name__}")
            return None

    def parse_twitter_html(self, html_content: str) -> ParseResult:
        """解析 Twitter HTML 内容

        Args:
            html_content (str): Twitter HTML 内容

        Returns:
            ParseResult: 解析结果
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # Download sites sometimes put inline emoji in <img alt="…"> elements.
        title_tag = soup.find("h3")
        if title_tag:
            for inline_image in title_tag.find_all("img"):
                alternative = inline_image.get("alt")
                inline_image.replace_with(alternative if isinstance(alternative, str) else "")
            for line_break in title_tag.find_all("br"):
                line_break.replace_with("\n")

        # 初始化数据
        title = None
        cover_url = None
        video_url = None
        images_urls = []
        dynamic_urls = []

        # 1. 提取缩略图链接
        thumb_tag = soup.find("img")
        if isinstance(thumb_tag, Tag):
            if cover := thumb_tag.get("src"):
                cover_url = str(cover)

        # 2. 提取下载链接
        tw_button_tags = soup.find_all("a", class_="tw-button-dl")
        abutton_tags = soup.find_all("a", class_="abutton")
        for tag in chain(tw_button_tags, abutton_tags):
            if not isinstance(tag, Tag):
                continue
            href = tag.get("href")
            if href is None:
                continue

            href = str(href)
            text = tag.get_text(strip=True)
            if "下载 MP4" in text:
                video_url = href
                break
            elif "下载图片" in text:
                images_urls.append(href)
            elif "下载 gif" in text:
                dynamic_urls.append(href)

        # 3. 提取标题
        if title_tag:
            title = title_tag.get_text().strip()

        # 简洁的构建方式
        contents = []

        # 添加视频内容
        if video_url:
            contents.append(self.create_video_content(video_url, cover_url))

        # 添加图片内容
        if images_urls:
            contents.extend(self.create_image_contents(images_urls))

        # 添加动态内容
        if dynamic_urls:
            contents.extend(self.create_dynamic_contents(dynamic_urls))

        return self.result(
            title=title,
            author=self.create_author("无用户名"),
            contents=contents,
        )
        # # 4. 提取Twitter ID
        # twitter_id_input = soup.find("input", {"id": "TwitterId"})
        # if (
        #     twitter_id_input
        #     and isinstance(twitter_id_input, Tag)
        #     and (value := twitter_id_input.get("value"))
        #     and isinstance(value, str)
        # ):
