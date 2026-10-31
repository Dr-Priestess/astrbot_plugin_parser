"""Map whole Unicode emoji to static PNG assets, preserving compound sequences."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import TypeVar
import uuid

import aiofiles
from apilmoji import EmojiCDNSource
import httpx
from PIL import Image

from astrbot.api import logger

T = TypeVar("T")


class StaticEmojiSource(EmojiCDNSource):
    """The emoji-datasource CDN serves codepoint.png, not /emoji?style=… APIs."""

    def __init__(self, *args, proxy: str | None = None, **kwargs):
        super().__init__(*args, max_concurrent=8, **kwargs)
        self.proxy = proxy

    @staticmethod
    def filenames(emoji: str) -> list[str]:
        points = [f"{ord(char):x}" for char in emoji]
        names = ["-".join(points)]
        # Some datasets omit optional emoji presentation selectors.
        without_selector = "-".join(point for point in points if point != "fe0f")
        if without_selector and without_selector not in names:
            names.append(without_selector)
        return [name + ".png" for name in names]

    def _get_emoji_path(self, emoji: str, is_discord: bool = False) -> Path:
        if is_discord:
            return super()._get_emoji_path(emoji, True)
        return self._emj_dir / self.filenames(emoji)[0]

    async def _download_emoji(self, emoji, is_discord=False, client=None):
        if is_discord:
            return await super()._download_emoji(emoji, True, client)
        if client is None:
            async with self._client() as shared:
                return await self._download_emoji(emoji, client=shared)
        path = self._get_emoji_path(emoji)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            async with asyncio.timeout(15):
                for filename in self.filenames(emoji):
                    url = f"{self.base_url.rstrip('/')}/{filename}"
                    async with client.stream("GET", url) as response:
                        if response.status_code != 200:
                            continue
                        contents = bytearray()
                        async for chunk in response.aiter_bytes():
                            contents.extend(chunk)
                            if len(contents) > 2 * 1024 * 1024:
                                raise ValueError("emoji image exceeds 2 MiB")
                    with Image.open(BytesIO(contents)) as image:
                        image.verify()
                    async with aiofiles.open(temporary, "wb") as output:
                        await output.write(contents)
                    temporary.replace(path)
                    return path
        except (httpx.HTTPError, TimeoutError, OSError, ValueError) as error:
            logger.warning(f"[emoji] 资源加载失败 {self.filenames(emoji)[0]}: {type(error).__name__}")
        finally:
            temporary.unlink(missing_ok=True)
        return None

    def _client(self):
        return httpx.AsyncClient(
            proxy=self.proxy, follow_redirects=True,
            headers={"User-Agent": "PRTS-Parser/1.6.1"},
            timeout=httpx.Timeout(8, connect=5),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
        )

    async def fetch_emojis(
        self, emojis: set[str], discord_emojis: set[str] | None = None,
        *, post_process: Callable[[Path], T] | None = None,
    ):
        paths: dict[str, Path | None] = {}
        missing = []
        for values, discord in [(emojis, False), (discord_emojis or set(), True)]:
            for value in values:
                path = self._get_emoji_path(value, discord)
                if path.is_file():
                    paths[value] = path
                else:
                    missing.append((value, discord))
        if missing:
            async with self._client() as client:
                async def fetch(value, discord):
                    async with self._semaphore:
                        return await self._download_emoji(value, discord, client)
                results = await asyncio.gather(*(fetch(value, discord) for value, discord in missing))
                paths.update((item[0], result) for item, result in zip(missing, results))
        if post_process:
            return {key: post_process(path) if path is not None else None for key, path in paths.items()}
        return paths
