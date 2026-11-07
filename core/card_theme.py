"""小普内容卡的框架；色板与关系卡一致，装饰始终留在边缘。"""
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

GROUND = "#FCF9F6"
INK = "#37313D"
MUTED = "#756B79"
ACCENT = "#705180"
RULE = "#E9E0E9"
GOLD = "#B59266"
ORBIT = "#B596C3"
ASSETS = Path(__file__).parent / "resources" / "prts"
FONT_PATH = ASSETS / "relationship.woff"


@lru_cache(maxsize=8)
def font(size: int, weight: int = 400) -> ImageFont.FreeTypeFont:
    loaded = ImageFont.truetype(BytesIO(FONT_PATH.read_bytes()), size)
    loaded.set_variation_by_axes([weight])
    return loaded


def _clouds(size: tuple[int, int]) -> Image.Image:
    width, height = size
    clouds = Image.new("RGBA", size)
    draw = ImageDraw.Draw(clouds)
    # 雾气在外框最明显，阅读面将其压到很低的对比度。
    draw.ellipse((width - 410, -170, width + 160, 260), fill=(204, 184, 219, 112))
    draw.ellipse((width - 190, -100, width + 140, 110), fill=(232, 207, 197, 90))
    draw.ellipse((-220, height - 320, 320, height + 220), fill=(204, 184, 219, 100))
    draw.ellipse((-160, height - 80, 410, height + 190), fill=(232, 207, 197, 75))
    return clouds.filter(ImageFilter.GaussianBlur(56))


def _orbit_corner(image: Image.Image, bottom: bool = False) -> None:
    # 在 2 倍局部画布上画细线，再缩小，避免细轨锯齿。
    overlay = Image.new("RGBA", (340, 180))
    draw = ImageDraw.Draw(overlay)
    def curve(points, color, width):
        samples = []
        for index in range(101):
            t = index / 100
            weights = ((1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3)
            samples.append(tuple(round(2 * sum(p[axis] * weight for p, weight in zip(points, weights))) for axis in (0, 1)))
        draw.line(samples, fill=color, width=width)
    curve(((0, 9), (90, 25), (155, 24), (167, 67)), (181, 150, 195, 180), 2)
    curve(((25, 0), (132, 17), (160, 28), (166, 46)), (181, 146, 102, 135), 1)
    draw.ellipse((312, 94, 322, 104), fill=GOLD, outline="#FFFDFB", width=2)
    overlay = overlay.resize((170, 90), Image.Resampling.LANCZOS)
    if bottom:
        overlay = overlay.transpose(Image.Transpose.ROTATE_180)
        image.alpha_composite(overlay, (5, image.height - 95))
    else:
        image.alpha_composite(overlay, (image.width - 175, 5))


def frame_card(body: Image.Image, platform: str, logo: Image.Image | None = None) -> Image.Image:
    margin, header, footer = 28, 68, 66
    width = body.width + margin * 2
    height = body.height + margin * 2 + header + footer
    image = Image.new("RGBA", (width, height), GROUND)
    image.alpha_composite(_clouds(image.size))
    sheet = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(sheet)
    draw.rounded_rectangle((margin, margin, width - margin - 1, height - margin - 1),
                           radius=24, fill=(255, 254, 253, 222), outline=(181, 150, 195, 98), width=1)
    image.alpha_composite(sheet)
    image.alpha_composite(body.convert("RGBA"), (margin, margin + header))
    draw = ImageDraw.Draw(image)
    x, right = margin + 36, width - margin - 36
    draw.text((x, margin + 25), "内容解析", font=font(23, 500), fill=ACCENT, anchor="lt")
    label_width = draw.textlength(platform, font=font(19))
    draw.text((right, margin + 28), platform, font=font(19), fill=MUTED, anchor="rt")
    if logo:
        logo = logo.copy()
        logo.thumbnail((48, 24), Image.Resampling.LANCZOS)
        image.alpha_composite(logo, (int(right - label_width - 12 - logo.width), margin + 26))
    draw.line((x, margin + header + 6, right, margin + header + 6), fill=RULE, width=1)
    y = margin + header + body.height
    draw.line((x, y, right, y), fill=RULE, width=1)
    draw.text((x, y + 22), "小普 · 普瑞赛斯", font=font(18), fill=MUTED, anchor="lt")
    draw.text((right, y + 22), "PRTS", font=font(17), fill=MUTED, anchor="rt")
    decoration = Image.new("RGBA", image.size)
    _orbit_corner(decoration)
    _orbit_corner(decoration, bottom=True)
    mask = Image.new("L", image.size, 255)
    ImageDraw.Draw(mask).rounded_rectangle((margin, margin, width - margin - 1, height - margin - 1), radius=24, fill=0)
    decoration.putalpha(ImageChops.multiply(decoration.getchannel("A"), mask))
    image.alpha_composite(decoration)
    return image.convert("RGB")
