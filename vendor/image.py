"""
图片处理共享工具

提供批量读图、降采样、网格拼图等通用逻辑。供 read_images、content 工具族
等共享，避免代码重复。

核心函数：
- load_image: 从本地路径或 URL 加载为 PIL Image
- downscale: 等比降采样到指定最大边长
- build_image_grid: 将多张图片拼成带索引编号 + 标题的网格图
- encode_base64: PIL Image → base64 字符串（默认 JPEG 以节省 token）
"""

import asyncio
import base64
import io
import math
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFont


# ── 网格拼图默认参数 ──
DEFAULT_THUMB_SIZE = 250         # 每格缩略图边长
DEFAULT_TEXT_HEIGHT = 80          # 每格下方文字区高度
DEFAULT_GRID_COLS = 5             # 每行几格
DEFAULT_PADDING = 12
DEFAULT_BG_COLOR = (255, 255, 255)
DEFAULT_TEXT_COLOR = (30, 30, 30)
DEFAULT_INDEX_COLOR = (220, 60, 60)

# ── 字体候选（跨平台中文支持） ──
# 注意：macOS 的 PingFang.ttc 因为格式原因 PIL/FreeType 无法读取，
# 必须使用 Hiragino 或 STHeiti 等其他中文字体。
_FONT_CANDIDATES = [
    # macOS（按优先级）
    "/System/Library/Fonts/Hiragino Sans GB.ttc",   # 冬青黑体，macOS 自带
    "/System/Library/Fonts/STHeiti Medium.ttc",     # 华文黑体
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    # Windows
    "msyh.ttc",           # 微软雅黑
    "simhei.ttf",         # 黑体
    "simsun.ttc",         # 宋体
]


def _load_fonts(title_size: int = 16, index_size: int = 32):
    """加载中文字体，全部失败则退回默认字体"""
    for path in _FONT_CANDIDATES:
        try:
            return (
                ImageFont.truetype(path, title_size),
                ImageFont.truetype(path, index_size),
            )
        except Exception:
            continue
    default = ImageFont.load_default()
    return default, default


# ── 加载图片 ──

async def _load_image_from_url(client: httpx.AsyncClient, url: str) -> Optional[Image.Image]:
    """下载单张图片，失败返回 None"""
    try:
        resp = await client.get(url, timeout=15.0)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None


def _load_image_from_path(path: str) -> Optional[Image.Image]:
    """从本地路径加载图片，失败返回 None"""
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


async def load_image(source: str, client: Optional[httpx.AsyncClient] = None) -> Optional[Image.Image]:
    """
    通用图片加载：自动识别 URL 或本地路径。

    Args:
        source: HTTP(S) URL 或本地文件路径
        client: 可选的 httpx 客户端（URL 加载时复用连接）

    Returns:
        PIL Image 对象（RGB 模式），失败返回 None
    """
    if source.startswith(("http://", "https://")):
        if client is not None:
            return await _load_image_from_url(client, source)
        async with httpx.AsyncClient() as c:
            return await _load_image_from_url(c, source)
    else:
        # 本地路径：在 executor 中执行以避免阻塞事件循环
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _load_image_from_path, source)


async def load_images(sources: Sequence[str]) -> List[Tuple[str, Optional[Image.Image]]]:
    """
    并发批量加载图片。

    Returns:
        [(source, image_or_none), ...] — 保留原始顺序，失败项值为 None
    """
    async with httpx.AsyncClient() as client:
        tasks = [load_image(src, client) for src in sources]
        images = await asyncio.gather(*tasks)
    return list(zip(sources, images))


# ── 降采样 ──

def downscale(image: Image.Image, max_dimension: int) -> Image.Image:
    """
    等比降采样到最大边不超过 max_dimension。
    如果图片已经足够小则原样返回。
    """
    if max(image.width, image.height) <= max_dimension:
        return image
    scale = max_dimension / max(image.width, image.height)
    new_size = (int(image.width * scale), int(image.height * scale))
    return image.resize(new_size, Image.LANCZOS)


# ── 网格拼图 ──

def build_image_grid(
    images: Sequence[Image.Image],
    labels: Optional[Sequence[str]] = None,
    columns: int = DEFAULT_GRID_COLS,
    thumb_size: int = DEFAULT_THUMB_SIZE,
    text_height: int = DEFAULT_TEXT_HEIGHT,
    padding: int = DEFAULT_PADDING,
    show_index: bool = True,
) -> Image.Image:
    """
    将多张图片拼成带索引编号 + 标题的网格图。

    每个单元格包含：
      - 左上角红底白字的序号（1, 2, 3...）
      - 等比缩放居中的缩略图
      - 下方的标题文字（可选，自动按像素宽度换行）

    Args:
        images: 待拼接的 PIL Image 列表
        labels: 每张图的标题（与 images 等长）；None 则不显示标题
        columns: 每行几格
        thumb_size: 每个缩略图格子的边长
        text_height: 每格下方文字区高度（labels 为 None 时自动置 0）
        padding: 格子间距和画布边距
        show_index: 是否显示左上角序号

    Returns:
        拼接后的 PIL Image
    """
    if not images:
        raise ValueError("images 不能为空")

    if labels is None:
        labels = [""] * len(images)
        text_height = 0
    elif len(labels) != len(images):
        raise ValueError(f"labels 长度 {len(labels)} 与 images {len(images)} 不匹配")

    count = len(images)
    cols = min(columns, count)
    rows = math.ceil(count / cols)

    cell_w = thumb_size + padding
    cell_h = thumb_size + text_height + padding
    canvas_w = cols * cell_w + padding
    canvas_h = rows * cell_h + padding

    canvas = Image.new("RGB", (canvas_w, canvas_h), DEFAULT_BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # 索引框按 thumb_size 比例缩放，保证视觉比例恒定（约 20% 占比）
    index_box_size = max(40, thumb_size // 5)
    index_font_size = int(index_box_size * 0.65)
    # 标题字体略与 thumb_size 相关，但下限保证小图时可读
    title_font_size = max(14, thumb_size // 18)
    font_title, font_index = _load_fonts(
        title_size=title_font_size,
        index_size=index_font_size,
    )

    for idx, (img, label) in enumerate(zip(images, labels), start=1):
        col = (idx - 1) % cols
        row = (idx - 1) // cols
        x = padding + col * cell_w
        y = padding + row * cell_h

        # 等比缩放居中
        scale = min(thumb_size / img.width, thumb_size / img.height)
        new_w = int(img.width * scale)
        new_h = int(img.height * scale)
        thumb = img.resize((new_w, new_h), Image.LANCZOS)
        offset_x = x + (thumb_size - new_w) // 2
        offset_y = y + (thumb_size - new_h) // 2
        canvas.paste(thumb, (offset_x, offset_y))

        # 左上角序号（跟随实际缩略图位置，大小按比例）
        if show_index:
            index_text = str(idx)
            idx_x = offset_x
            idx_y = offset_y
            draw.rectangle(
                [idx_x, idx_y, idx_x + index_box_size, idx_y + index_box_size],
                fill=DEFAULT_INDEX_COLOR,
            )
            bbox = draw.textbbox((0, 0), index_text, font=font_index)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            # 文本垂直对齐用 bbox 的 top 偏移修正（font bbox 的 top 可能不为 0）
            text_x = idx_x + (index_box_size - tw) // 2 - bbox[0]
            text_y = idx_y + (index_box_size - th) // 2 - bbox[1]
            draw.text((text_x, text_y), index_text, fill=(255, 255, 255), font=font_index)

        # 下方标题（自动按像素宽度换行）
        if label and text_height > 0:
            lines = _wrap_text_by_pixel(label, font_title, thumb_size, draw)
            for line_i, line in enumerate(lines):
                draw.text(
                    (x, y + thumb_size + 6 + line_i * 22),
                    line,
                    fill=DEFAULT_TEXT_COLOR,
                    font=font_title,
                )

    return canvas


def _wrap_text_by_pixel(text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> List[str]:
    """按像素宽度自动换行，兼容中英文混排（逐字符判断）"""
    lines = []
    current = ""
    for ch in text:
        test = current + ch
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width:
            if current:
                lines.append(current)
            current = ch
        else:
            current = test
    if current:
        lines.append(current)
    return lines


# ── 编码为 base64 ──

def encode_base64(image: Image.Image, format: str = "JPEG", quality: int = 75) -> Tuple[str, str]:
    """
    将 PIL Image 编码为 base64 字符串。

    Args:
        image: PIL Image 对象
        format: "JPEG" 或 "PNG"。JPEG 体积更小，推荐用于多模态 LLM 输入
        quality: JPEG 质量（1-100），PNG 忽略此参数

    Returns:
        (base64_data, media_type) 元组，如 ("iVBOR...", "image/png")
    """
    buf = io.BytesIO()
    save_kwargs = {"format": format}
    if format.upper() == "JPEG":
        # JPEG 不支持透明通道
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGB")
        save_kwargs["quality"] = quality
        save_kwargs["optimize"] = True
    image.save(buf, **save_kwargs)

    data = base64.b64encode(buf.getvalue()).decode("utf-8")
    media_type = f"image/{format.lower()}"
    if format.upper() == "JPEG":
        media_type = "image/jpeg"
    return data, media_type
