from dataclasses import replace
from itertools import chain
from pathlib import Path

from apilmoji.core import NodeType, parse_lines

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    DurationLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer
from .utils import transcode_video_audio_to_lc


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

    FORWARD_NODE_NAME = "小普 · 内容解析"

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    @staticmethod
    def _image_from_path(path: Path) -> Image:
        return Image.fromFileSystem(str(path))

    @staticmethod
    def _video_from_path(path: Path) -> Video:
        return Video.fromFileSystem(str(path))

    @staticmethod
    def _record_from_path(path: Path) -> Record:
        return Record.fromFileSystem(str(path))

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _is_long_text(self, text: str) -> bool:
        threshold = getattr(self.cfg, "text_forward_threshold", None)
        threshold = 500 if threshold is None else int(threshold)
        return threshold > 0 and len(text) > threshold

    @staticmethod
    def _content_text(contents: list) -> str:
        parts = []
        for content in contents:
            if isinstance(content, TextContent):
                parts.append(content.text)
            elif isinstance(content, GraphicsContent):
                parts.extend([content.text or "", content.alt or ""])
        return "".join(parts)

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
        render_card_override: bool | None = None,
        include_result_text: bool = True,
    ) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        # 基础卡片渲染条件：单条重媒体配置或显式指定
        is_single_heavy = len(heavy) == 1 and not light
        render_card = is_single_heavy and self.cfg.single_heavy_render_card
        if render_card_override is not None:
            render_card = render_card_override

        # 实际媒体段数量
        media_count = len(light) + len(heavy)

        # 达到阈值后，强制合并转发，避免刷屏
        force_merge = (media_count + (1 if render_card else 0)) >= self.cfg.forward_threshold
        if force_merge_override is not None:
            force_merge = force_merge_override

        # 正文可能存在于标准文字项，也可能只存在于 result.text（例如 B 站长文）。
        # 后续独立视频组不重复带上已经发送过的文章。
        content_text = self._content_text(light)
        fallback_text = ""
        if include_result_text and not content_text:
            fallback_text = "".join(seg.text for seg in self._build_text_fallback(result))
        long_text = self._is_long_text(content_text or fallback_text)
        if long_text:
            force_merge = True

        # 合并转发时始终内联预览卡片（除非显式禁用）
        if force_merge and render_card_override is not False:
            render_card = True

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 预览卡片：仅在“渲染卡片 + 不合并”时独立发送
            "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
            "long_text": long_text,
            "full_text": fallback_text if long_text else "",
            "content_text": content_text,
        }

    @staticmethod
    def _preview_result(result: ParseResult, plan: dict) -> ParseResult:
        """长文只在卡片上展示短预览；原文对象和转发正文不截断。"""
        if not plan["long_text"]:
            return result

        def clip(text: str | None, limit: int) -> str | None:
            return text if not text or len(text) <= limit else text[:limit].rstrip() + "…"

        text = result.text or plan["content_text"] or None
        count = len(text or result.title or "")
        extra = dict(result.extra)
        extra["info"] = f"正文 {count} 字 · 全文见聊天记录"
        contents = [
            replace(cont, text=None, alt=None)
            if isinstance(cont, GraphicsContent) else cont
            for cont in result.contents
        ]
        return replace(result, title=clip(result.title, 120), text=clip(text, 180),
                       contents=contents, extra=extra, render_image=None)

    async def _send_preview_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        plan: dict,
    ) -> bool:
        """
        发送预览卡片（独立消息）

        场景：
        - 只有一个重媒体
        - 未触发合并转发
        - 卡片作为“预览”，不与正文混合
        """
        if not plan["preview_card"]:
            return False

        if image_path := await self.renderer.render_card(result):
            await event.send(event.chain_result([self._image_from_path(image_path)]))
            return True
        return False

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        """
        segs: list[BaseMessageComponent] = []

        # 合并转发时，卡片以内联形式作为一个消息段参与合并
        if plan["render_card"] and plan["force_merge"]:
            if image_path := await self.renderer.render_card(self._preview_result(result, plan)):
                segs.append(self._image_from_path(image_path))

        if plan["full_text"]:
            segs.append(Plain(plan["full_text"]))

        # 轻媒体处理
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    segs.append(Plain(cont.text))
                continue

            # 图文文字独立于下载结果；图片失败也不能吞掉正文。
            if isinstance(cont, GraphicsContent) and cont.text:
                segs.append(Plain(cont.text))
            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                if isinstance(cont, GraphicsContent) and cont.alt:
                    segs.append(Plain(cont.alt))
                continue

            match cont:
                case ImageContent():
                    segs.append(self._image_from_path(path))
                case GraphicsContent() as g:
                    segs.append(self._image_from_path(path))
                    # GraphicsContent 允许携带补充文本
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except (SizeLimitException, DurationLimitException) as exc:
                if self.cfg.show_download_fail_tip:
                    message = (
                        "此项媒体超过时长限制"
                        if isinstance(exc, DurationLimitException)
                        else "此项媒体超过大小限制"
                    )
                    segs.append(Plain(message))
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    path = await transcode_video_audio_to_lc(path)
                    segs.append(self._video_from_path(path))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=self._to_file_uri(path))
                        if self.cfg.audio_to_file
                        else self._record_from_path(path)
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        return segs

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        text = "".join(seg.text for seg in segs if isinstance(seg, Plain))
        if not segs or not (force_merge or self._is_long_text(text)):
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()

        for seg in segs:
            # 一个节点过长仍难阅读，优先在段落边界拆分，拼回后与原文完全一致。
            parts = self._split_text(seg.text) if isinstance(seg, Plain) else [seg]
            for part in parts:
                component = Plain(part) if isinstance(part, str) else part
                nodes.nodes.append(Node(uin=self_id, name=self.FORWARD_NODE_NAME, content=[component]))

        return [nodes]

    @staticmethod
    def _split_text(text: str, limit: int = 1600) -> list[str]:
        parts = []
        while len(text) > limit:
            cut = text.rfind("\n", limit // 2, limit)
            if cut < 0:
                cut = text.rfind(" ", limit // 2, limit)
            if cut >= 0:
                cut += 1
            else:
                cut = limit
                # 无段落可切时仍保持 ZWJ、肤色和旗帜等组合 emoji 完整。
                start = max(0, cut - 32)
                cursor = start
                for nodes in parse_lines([text[start:cut + 32]]):
                    for node in nodes:
                        end = cursor + len(node.content)
                        if node.type is NodeType.EMOJI and cursor < cut < end:
                            cut = cursor or end
                        cursor = end
            parts.append(text[:cut])
            text = text[cut:]
        if text:
            parts.append(text)
        return parts

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line)
        return [Plain(text)] if text.strip() else []

    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(MessageSender._iter_contents(result)))]

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
        *,
        include_result_text: bool = True,
    ) -> bool:
        plan = self._build_send_plan(
            result,
            group.contents,
            force_merge_override=group.force_merge,
            render_card_override=group.render_card,
            include_result_text=include_result_text,
        )

        preview_sent = await self._send_preview_card(event, result, plan)

        segs = await self._build_segments(result, plan)
        segs = self._merge_segments_if_needed(event, segs, plan["force_merge"])

        if not segs:
            return preview_sent

        try:
            await event.send(event.chain_result(segs))
            return True
        except Exception as e:
            seg_meta = self._collect_seg_meta(segs)
            logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return False

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """提取消息段元信息，用于失败日志定位。"""
        meta: list[dict[str, str]] = []

        for seg in segs:
            item = {"type": seg.__class__.__name__}
            for attr in ("file", "path", "url"):
                value = getattr(seg, attr, None)
                if value:
                    item["media"] = str(value)
                    break
            meta.append(item)

        return meta

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送
        """
        groups = self._resolve_groups(result)

        sent = False
        for index, group in enumerate(groups):
            sent = await self._send_group(event, result, group, include_result_text=index == 0) or sent

        if not sent:
            segs = self._build_text_fallback(result)
            if not segs:
                logger.warning("发送结果为空，不执行发送")
                return

            segs = self._merge_segments_if_needed(event, segs, False)

            try:
                await event.send(event.chain_result(segs))
            except Exception as e:
                seg_meta = self._collect_seg_meta(segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return
