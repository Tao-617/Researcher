"""
YouTube 平台实现

后端：crawler.aiddit.com/crawler/youtube
"""

import json
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from vendor.tool_result import ToolResult
from vendor.image import build_image_grid, encode_base64, load_images
from platforms.registry import (
    PlatformDef, ParamSpec, register_platform,
)

CRAWLER_BASE_URL = "http://crawler.aiddit.com/crawler"
DEFAULT_TIMEOUT = 60.0


# ── 字段 normalization：YouTube 后端字段名跟 evaluator/其他平台不一致 ──
#
# evaluator 期待的字段     | YouTube 后端返回的字段
# ------------------------+---------------------------
# channel_content_id      | video_id
# body_text               | description_snippet
# like_count (int)        | view_count ("130,461 views")
# publish_timestamp (ms)  | published_time ("6 months ago")
# link                    | url
# duration_sec (float)    | duration ("6:15" or "1:23:45")
# images (list[str])      | thumbnails (list[dict])
# content_type=="video"   | (缺失)
# videos                  | (缺失)
#
# 不做 normalization 的话 evaluator 会走 article 路径 + 8 个字段全找不到，
# 视频拿 15 分 F。


def _parse_duration(s: Any) -> Optional[float]:
    """Parse 'MM:SS' or 'HH:MM:SS' to seconds (float)."""
    if not isinstance(s, str):
        return None
    parts = s.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return float(nums[0] * 60 + nums[1])
    if len(nums) == 3:
        return float(nums[0] * 3600 + nums[1] * 60 + nums[2])
    return None


def _parse_view_count(s: Any) -> Optional[int]:
    """Parse '130,461 views' (or '1.2M views') to int."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    # "1.2M views" / "3.5K views"
    m = re.match(r"([\d.]+)\s*([KMBkmb])\b", s)
    if m:
        try:
            num = float(m.group(1))
        except ValueError:
            return None
        mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()]
        return int(num * mult)
    # "130,461 views"
    m = re.search(r"([\d,]+)", s)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


_RELATIVE_TIME_RE = re.compile(
    r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE
)
_SECONDS_PER = {
    "minute": 60, "hour": 3600, "day": 86400,
    "week": 86400 * 7, "month": 86400 * 30, "year": 86400 * 365,
}


def _parse_relative_time(s: Any) -> Optional[int]:
    """Parse '6 months ago' -> UTC milliseconds timestamp."""
    if not isinstance(s, str):
        return None
    m = _RELATIVE_TIME_RE.search(s.lower())
    if not m:
        return None
    n = int(m.group(1))
    delta = n * _SECONDS_PER.get(m.group(2).lower(), 0)
    if not delta:
        return None
    return int((time.time() - delta) * 1000)


def _normalize_youtube_post(post: Dict[str, Any]) -> None:
    """In-place: rewrite YouTube post fields onto the schema evaluator/transcription expect.

    Idempotent — only fills missing fields, never overwrites existing values.
    """
    if not isinstance(post, dict):
        return

    if post.get("video_id") and not post.get("channel_content_id"):
        post["channel_content_id"] = post["video_id"]

    if post.get("description_snippet") and not post.get("body_text"):
        post["body_text"] = post["description_snippet"]

    if post.get("view_count") and not isinstance(post.get("like_count"), (int, float)):
        n = _parse_view_count(post["view_count"])
        if n is not None:
            post["like_count"] = n

    if post.get("published_time") and not post.get("publish_timestamp"):
        ts = _parse_relative_time(post["published_time"])
        if ts:
            post["publish_timestamp"] = ts

    if post.get("url") and not post.get("link"):
        post["link"] = post["url"]

    if post.get("duration") and not isinstance(post.get("duration_sec"), (int, float)):
        sec = _parse_duration(post["duration"])
        if sec:
            post["duration_sec"] = sec

    if post.get("thumbnails") and not post.get("images"):
        imgs = []
        for t in post["thumbnails"]:
            if isinstance(t, dict) and t.get("url"):
                imgs.append(t["url"])
        if imgs:
            post["images"] = imgs

    if not post.get("content_type"):
        post["content_type"] = "video"

    if not post.get("videos"):
        # transcription.extract_video_url for "youtube" uses video_id directly,
        # so this `videos` field is just for evaluator.is_video detection.
        url = post.get("url")
        if url:
            post["videos"] = [url]


# ── 搜索 ──

async def search(
    platform_id: str,
    keyword: str,
    max_count: int = 20,
    cursor: str = "",
    extras: Optional[Dict[str, Any]] = None,
) -> ToolResult:
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(
                f"{CRAWLER_BASE_URL}/youtube/keyword",
                json={"keyword": keyword},
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            return ToolResult(title="YouTube 搜索失败", output="", error=data.get("msg", "未知错误"))

        result_data = data.get("data", {})
        videos = result_data.get("data", []) if isinstance(result_data, dict) else []

        # YouTube 字段名跟其他平台不一致，先 normalize 让 evaluator 能正确评分
        # （并且让 duration_sec / publish_timestamp 等被解析出来，复用 video-mode 评分）
        for v in videos:
            _normalize_youtube_post(v)

        # 动态导入评价模块
        try:
            from examples.process_pipeline.script.evaluate_source_quality import SourceQualityEvaluator
            evaluator = SourceQualityEvaluator()
        except ImportError:
            evaluator = None

        # 概览
        summary_list = []
        for idx, video in enumerate(videos[:max_count], 1):
            score_info = {}
            if evaluator:
                try:
                    eval_res = evaluator.evaluate_post(video)
                    score_info = {
                        "quality_score": eval_res["total_score"],
                        "quality_grade": eval_res["grade"]
                    }
                    video["_quality_score"] = eval_res["total_score"]
                    video["_quality_grade"] = eval_res["grade"]
                except Exception:
                    pass
            
            summary_item = {
                "index": idx,
                "title": video.get("title", ""),
                "author": video.get("author", ""),
                "video_id": video.get("video_id", ""),
            }
            summary_item.update(score_info)
            summary_list.append(summary_item)

        # 拼图
        images = []
        collage_obj = await _build_video_collage(videos[:max_count])
        if collage_obj:
            images.append(collage_obj)

        return ToolResult(
            title=f"YouTube: {keyword}",
            output=json.dumps({"data": summary_list}, ensure_ascii=False, indent=2),
            long_term_memory=f"Searched YouTube for '{keyword}', {len(videos)} results.",
            images=images,
            metadata={"posts": videos[:max_count]},
        )

    except Exception as e:
        return ToolResult(title="YouTube 搜索异常", output="", error=str(e))


# ── 详情 ──

async def detail(post: Dict[str, Any], extras: Optional[Dict[str, Any]] = None) -> ToolResult:
    """
    YouTube 详情：需要额外 HTTP 调用获取字幕/下载等。
    post 来自搜索缓存，extras 支持 include_captions / download_video。

    Graceful degrade: 三条数据通路（/youtube/detail 增强元数据、/youtube/captions 官方字幕、
    Deepgram 自研转写）独立进行，任何一条失败都不影响其他。特别是 Deepgram 走的是
    yt-dlp 下载 watch URL → ffmpeg → Deepgram API，跟 crawler.aiddit.com 后端无关，
    后端宕机时仍应自动跑 transcript。
    """
    extras = extras or {}
    content_id = post.get("video_id") or post.get("channel_content_id", "")
    include_captions = extras.get("include_captions", True)
    download_video = extras.get("download_video", False)
    include_transcript = extras.get("include_transcript", True)

    # ── 1) /youtube/detail：拿增强元数据（标题/描述/点赞等）。失败时用 search post 兜底 ──
    video_info: Dict[str, Any] = {}
    detail_error: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                f"{CRAWLER_BASE_URL}/youtube/detail",
                json={"content_id": content_id},
            )
            resp.raise_for_status()
            detail_data = resp.json()
        if detail_data.get("code") == 0:
            result_data = detail_data.get("data", {})
            video_info = result_data.get("data", {}) if isinstance(result_data, dict) else {}
        else:
            detail_error = detail_data.get("msg") or "未知错误"
    except Exception as e:
        detail_error = str(e)

    # ── 2) /youtube/captions：官方字幕（也走 crawler 后端，同样可能挂） ──
    captions_text: Optional[str] = None
    if include_captions or download_video:
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                cap_resp = await client.post(
                    f"{CRAWLER_BASE_URL}/youtube/captions",
                    json={"content_id": content_id},
                )
                cap_resp.raise_for_status()
                cap_data = cap_resp.json()
                if cap_data.get("code") == 0:
                    inner = cap_data.get("data", {})
                    if isinstance(inner, dict):
                        inner2 = inner.get("data", {})
                        if isinstance(inner2, dict):
                            captions_text = inner2.get("content")
        except Exception:
            pass

    # ── 3) 视频文件下载（用户显式 extras.download_video=True 时才跑） ──
    video_path = None
    video_outline = None
    if download_video:
        import asyncio
        try:
            from agent.tools.builtin.content.media import download_youtube_video, parse_srt_to_outline
            video_path = await asyncio.to_thread(download_youtube_video, content_id)
            if captions_text:
                video_outline = parse_srt_to_outline(captions_text)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("youtube download_video failed: %s", e)

    # ── 4) Deepgram 转写：独立于 1)/2)，走 yt-dlp+Deepgram，不依赖 crawler 后端 ──
    #
    # 三态语义（跟 extract_sources / aigc_channel.detail 对齐）：
    #   字段缺失     → 没尝试过，跑 Deepgram
    #   字段 = ""    → 尝试过但失败，跳过（保护 Deepgram 额度）
    #   字段 = text  → 已成功，复用
    transcript_text: Optional[str] = post.get("video_transcript") or None
    field_present = "video_transcript" in post
    transcribe_error: Optional[str] = None
    if not field_present and include_transcript:
        from agent.tools.builtin.content.transcription import transcribe_video_from_post
        if not post.get("video_id"):
            post["video_id"] = content_id
        try:
            transcript_text = await transcribe_video_from_post("youtube", post)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("youtube transcribe failed: %s", e)
            transcript_text = None
            transcribe_error = f"{type(e).__name__}: {e}"

        # 三态写回：成功 = text；失败/None = "" 作为"已尝试"标记
        final_value = transcript_text or ""
        post["video_transcript"] = final_value
        if not final_value:
            post["_transcribe_error"] = (
                transcribe_error
                or "transcribe returned None (yt-dlp/Deepgram 任一步失败，见 logger.warning)"
            )

        # cache writeback 失败的 "" 也写，下次 cache hit 短路
        import os as _os
        from agent.tools.builtin.content import cache as _cache
        trace_id = extras.get("__trace_id__") or _os.getenv("TRACE_ID")
        if trace_id and content_id:
            _cache.update_post_field(trace_id, "youtube", content_id, "video_transcript", final_value)

    # ── 5) 组装输出：detail 接口的字段优先，缺失时用 search post 兜底 ──
    output_data = {
        "video_id": content_id,
        "title": video_info.get("title") or post.get("title", ""),
        "channel": video_info.get("channel_account_name") or post.get("author", ""),
        "description": (
            video_info.get("body_text")
            or post.get("body_text")
            or post.get("description_snippet", "")
        ),
        "like_count": (
            video_info.get("like_count")
            if video_info.get("like_count") is not None
            else post.get("like_count")
        ),
        "comment_count": video_info.get("comment_count"),
        "content_link": video_info.get("content_link") or post.get("link", ""),
        "captions": captions_text,           # YouTube 官方字幕（可能为空）
        # Deepgram 转写：读 post 字段，三态语义自然透出（"" = 已尝试失败）
        "video_transcript": post.get("video_transcript", ""),
    }
    if detail_error:
        # 显式标记 graceful degrade 状态，让上层知道这次走的是 fallback
        output_data["_detail_backend_error"] = detail_error
    if post.get("_transcribe_error"):
        # Deepgram 这一路失败原因透到 output，方便 agent/用户判断要不要重试
        output_data["_transcribe_error"] = post["_transcribe_error"]
    if download_video:
        output_data["video_path"] = video_path
        output_data["video_outline"] = video_outline

    output_text = json.dumps(output_data, ensure_ascii=False, indent=2)

    memory_parts = []
    if captions_text:
        memory_parts.append("captions")
    if transcript_text and transcript_text != captions_text:
        memory_parts.append("transcript")
    if detail_error:
        memory_parts.append(f"degraded(detail backend down)")
    memory_extra = f" with {'+'.join(memory_parts)}" if memory_parts else ""

    title = video_info.get("title") or post.get("title") or content_id
    return ToolResult(
        title=f"YouTube 详情: {title}",
        output=output_text,
        long_term_memory=f"YouTube detail for {content_id}{memory_extra}",
    )


# ── 拼图 ──

async def _build_video_collage(videos: List[Dict[str, Any]]) -> Optional[str]:
    urls, titles = [], []
    for video in videos:
        thumb = None
        if "thumbnails" in video and isinstance(video["thumbnails"], list) and video["thumbnails"]:
            thumb = video["thumbnails"][0].get("url")
        elif "thumbnail" in video:
            thumb = video.get("thumbnail")
        elif "cover_url" in video:
            thumb = video.get("cover_url")

        if thumb:
            urls.append(thumb)
            base_title = video.get("title", "")
            score = video.get("_quality_score")
            if score is not None:
                title_with_score = f"[{score}分] {base_title}"
            else:
                title_with_score = base_title
            titles.append(title_with_score)

    if not urls:
        return None

    loaded = await load_images(urls)
    valid_images, valid_labels = [], []
    for (_, img), title in zip(loaded, titles):
        if img is not None:
            valid_images.append(img)
            valid_labels.append(title)

    if not valid_images:
        return None

    grid = build_image_grid(images=valid_images, labels=valid_labels)
    import io
    buf = io.BytesIO()
    grid.save(buf, format="PNG")
    img_bytes = buf.getvalue()
    
    try:
        from agent.tools.builtin.file.image_cdn import _upload_bytes_to_oss
        import hashlib
        
        md5_hash = hashlib.md5(img_bytes).hexdigest()[:12]
        filename = f"youtube_collage_{md5_hash}.png"
        cdn_url = await _upload_bytes_to_oss(img_bytes, filename)
        return {"type": "url", "url": cdn_url}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to upload youtube collage to CDN: %s", e)
        b64, _ = encode_base64(grid, format="PNG")
        return {"type": "base64", "media_type": "image/png", "data": b64}


# ── 注册 ──

_YOUTUBE = PlatformDef(
    id="youtube",
    name="YouTube",
    aliases=["yt", "油管"],
    detail_extras={
        "include_captions": ParamSpec(note="是否获取字幕，默认 True"),
        "download_video": ParamSpec(note="是否下载视频到本地，默认 False"),
    },
)
_YOUTUBE.search_impl = search
_YOUTUBE.detail_impl = detail
register_platform(_YOUTUBE)
