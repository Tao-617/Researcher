"""
X (Twitter) 平台实现

后端：crawler.aiddit.com/crawler/x
"""

import json
from typing import Any, Dict, List, Optional

import httpx

from vendor.tool_result import ToolResult
from vendor.image import build_image_grid, encode_base64, load_images
from platforms.registry import PlatformDef, register_platform

CRAWLER_URL = "http://crawler.aiddit.com/crawler/x/keyword"
COMMENT_URL = "http://crawler.aiddit.com/crawler/x/comment"
DEFAULT_TIMEOUT = 60.0
AUTHOR_COMMENT_TOP_N = 10


async def search(
    platform_id: str,
    keyword: str,
    max_count: int = 20,
    cursor: str = "",
    extras: Optional[Dict[str, Any]] = None,
) -> ToolResult:
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(CRAWLER_URL, json={"keyword": keyword})
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            return ToolResult(title="X 搜索失败", output="", error=data.get("msg", "未知错误"))

        result_data = data.get("data", {})
        tweets = result_data.get("data", []) if isinstance(result_data, dict) else []

        # 动态导入评价模块
        try:
            from examples.process_pipeline.script.evaluate_source_quality import SourceQualityEvaluator
            evaluator = SourceQualityEvaluator()
        except ImportError:
            evaluator = None

        # 视频帖在评分前先并发探测 mp4 duration（HTTP Range，不下载视频流），
        # 让 evaluator 用真实时长替代 body 长度作为内容信号。
        if evaluator and tweets:
            try:
                from agent.tools.builtin.content.transcription import probe_durations_for_posts
                await probe_durations_for_posts("x", tweets[:max_count], concurrency=8)
            except Exception as e:
                import logging
                logging.getLogger(__name__).info("duration probe failed for x: %s", e)

        summary_list = []
        for idx, tweet in enumerate(tweets[:max_count], 1):
            text = tweet.get("body_text", "")

            score_info = {}
            if evaluator:
                try:
                    eval_res = evaluator.evaluate_post(tweet)
                    score_info = {
                        "quality_score": eval_res["total_score"],
                        "quality_grade": eval_res["grade"]
                    }
                    tweet["_quality_score"] = eval_res["total_score"]
                except Exception:
                    pass

            summary_item = {
                "index": idx,
                "author": tweet.get("channel_account_name", ""),
                "body_text": text[:100] + ("..." if len(text) > 100 else ""),
                "like_count": tweet.get("like_count"),
                "comment_count": tweet.get("comment_count"),
                "link": tweet.get("link"),
            }
            summary_item.update(score_info)
            summary_list.append(summary_item)

        # 拼图
        images = []
        collage_obj = await _build_tweet_collage(tweets[:max_count])
        if collage_obj:
            images.append(collage_obj)

        return ToolResult(
            title=f"X: {keyword}",
            output=json.dumps({"data": summary_list}, ensure_ascii=False, indent=2),
            long_term_memory=f"Searched X for '{keyword}', {len(tweets)} results.",
            images=images,
            metadata={"posts": tweets[:max_count]},
        )

    except Exception as e:
        return ToolResult(title="X 搜索异常", output="", error=str(e))


MAX_DETAIL_IMAGES = 10
KEEP_INDIVIDUAL = 8


async def _build_images_collage(urls: List[str]) -> Optional[Dict[str, Any]]:
    """将一组图片 URL 拼成单张网格图"""
    if not urls:
        return None

    loaded = await load_images(urls)
    valid_images = [img for (_, img) in loaded if img is not None]
    if not valid_images:
        return None

    grid = build_image_grid(images=valid_images, labels=None)
    import io
    buf = io.BytesIO()
    grid.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    try:
        from agent.tools.builtin.file.image_cdn import _upload_bytes_to_oss
        import hashlib

        md5_hash = hashlib.md5(img_bytes).hexdigest()[:12]
        filename = f"x_detail_collage_{md5_hash}.png"
        cdn_url = await _upload_bytes_to_oss(img_bytes, filename)
        return {"type": "url", "url": cdn_url}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to upload x detail collage to CDN: %s", e)
        b64, _ = encode_base64(grid, format="PNG")
        return {"type": "base64", "media_type": "image/png", "data": b64}


async def _fetch_author_comments(content_id: str, author_id: str) -> List[Dict[str, Any]]:
    """拉取该推文评论，仅保留原作者本人发布的回复，按点赞数降序取 Top N。"""
    if not content_id or not author_id:
        return []

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(COMMENT_URL, json={"content_id": content_id})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to fetch x comments for %s: %s", content_id, e)
        return []

    if payload.get("code") != 0:
        return []

    inner = payload.get("data", {})
    raw_comments = inner.get("data", []) if isinstance(inner, dict) else []

    author_id_str = str(author_id)
    author_comments = []
    for c in raw_comments:
        author = c.get("author") or {}
        if str(author.get("rest_id", "")) != author_id_str:
            continue
        author_comments.append({
            "text": c.get("display_text") or c.get("text", ""),
            "likes": c.get("likes", 0) or 0,
            "replies": c.get("replies", 0) or 0,
            "created_at": c.get("created_at", ""),
        })

    author_comments.sort(key=lambda x: x["likes"], reverse=True)
    return author_comments[:AUTHOR_COMMENT_TOP_N]


async def detail(post: Dict[str, Any], extras: Optional[Dict[str, Any]] = None) -> ToolResult:
    """X 的详情直接从缓存的搜索结果取完整数据，并补拉作者本人的热门补充评论。"""
    author = post.get("channel_account_name", "")
    author_id = post.get("channel_account_id", "")
    content_id = post.get("channel_content_id", "")
    text = post.get("body_text", "")[:30]

    img_urls = []
    for img_item in post.get("image_url_list", []):
        url = img_item.get("image_url") if isinstance(img_item, dict) else img_item
        if url:
            img_urls.append(url)

    all_images = []
    if len(img_urls) > MAX_DETAIL_IMAGES:
        for u in img_urls[:KEEP_INDIVIDUAL]:
            all_images.append({"type": "url", "url": u})
        collage = await _build_images_collage(img_urls[KEEP_INDIVIDUAL:])
        if collage:
            all_images.append(collage)
    else:
        for u in img_urls:
            all_images.append({"type": "url", "url": u})

    author_comments = await _fetch_author_comments(content_id, author_id)

    extras_d = extras or {}
    trace_id = extras_d.get("__trace_id__")
    if not trace_id:
        import os as _os
        trace_id = _os.getenv("TRACE_ID")

    # 把作者评论写回 cache，让下游离线流程（如 extract_sources）也能拿到
    if author_comments:
        from agent.tools.builtin.content import cache as _cache
        if trace_id and content_id:
            _cache.update_post_field(trace_id, "x", content_id, "author_comments", author_comments)

    # 视频字幕：检测到 video_url_list 时通过 Deepgram 转写 (default on, opt-out via extras)
    transcript_text: Optional[str] = post.get("video_transcript")  # cache hit reuse
    if not transcript_text and extras_d.get("include_transcript", True):
        from agent.tools.builtin.content.transcription import transcribe_video_from_post
        transcript_text = await transcribe_video_from_post("x", post)
        if transcript_text:
            post["video_transcript"] = transcript_text
            from agent.tools.builtin.content import cache as _cache
            if trace_id and content_id:
                _cache.update_post_field(trace_id, "x", content_id, "video_transcript", transcript_text)

    output_json = json.dumps(post, ensure_ascii=False, indent=2)

    sections = [output_json]
    if author_comments:
        lines = [f"=== 作者 @{author} 在评论区的补充（按点赞 Top {len(author_comments)}） ==="]
        for i, c in enumerate(author_comments, 1):
            lines.append(f"{i}. [赞{c['likes']} · 回复{c['replies']}] {c['text']}")
        sections.append("\n".join(lines))
    # transcript already embedded as post["video_transcript"] inside output_json above;
    # no need to repeat as a separate section.
    output_text = "\n\n".join(sections)

    memory_extras = []
    if author_comments:
        memory_extras.append(f"{len(author_comments)} author replies")
    if transcript_text:
        memory_extras.append("+transcript")
    memory_suffix = " + " + ", ".join(memory_extras) if memory_extras else ""
    return ToolResult(
        title=f"X 详情: @{author}",
        output=output_text,
        long_term_memory=f"Viewed X post by @{author}: {text}{memory_suffix}",
        images=all_images,
    )


async def _build_tweet_collage(tweets: List[Dict[str, Any]]) -> Optional[str]:
    urls, titles = [], []
    for tweet in tweets:
        thumb = None
        for img_item in tweet.get("image_url_list", []):
            url = img_item.get("image_url") if isinstance(img_item, dict) else img_item
            if url:
                thumb = url
                break
        if not thumb:
            thumb = tweet.get("cover_url")
        if thumb:
            urls.append(thumb)
            base_title = f"@{tweet.get('channel_account_name', '')}"
            score = tweet.get("_quality_score")
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
        filename = f"x_collage_{md5_hash}.png"
        cdn_url = await _upload_bytes_to_oss(img_bytes, filename)
        return {"type": "url", "url": cdn_url}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to upload x collage to CDN: %s", e)
        b64, _ = encode_base64(grid, format="PNG")
        return {"type": "base64", "media_type": "image/png", "data": b64}


# ── 注册 ──

_X = PlatformDef(
    id="x",
    name="X (Twitter)",
    aliases=["twitter", "推特"],
)
_X.search_impl = search
_X.detail_impl = detail
register_platform(_X)
