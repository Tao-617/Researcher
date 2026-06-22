"""阶段2：多平台 × 多关键词 并发搜索，按 (platform, content_id) 精确去重。

移植自 search_and_evaluate.py::search_all，去掉视频转写 / 图片下载 / 时间戳转换等
可选增强（researcher 原型先求通路；这些作为后续增强）。产出标准 source_dict 列表。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import platforms._backends  # noqa: F401  容错加载所有可用平台后端（含 MediaCrawler）
from platforms.registry import get_platform, all_platforms

logger = logging.getLogger(__name__)


def _post_cid(post: Dict[str, Any]) -> Optional[str]:
    cid = post.get("channel_content_id") or post.get("video_id")
    if cid:
        return str(cid)
    link = post.get("link") or post.get("url")
    return str(link) if link else None


async def _search_one(pdef, keyword: str, max_count: int, sem: asyncio.Semaphore):
    """单关键词搜索，返回 [(platform_id, keyword, posts)]。任何异常降级为空结果。"""
    async with sem:
        try:
            result = await pdef.search_impl(
                platform_id=pdef.id, keyword=keyword, max_count=max_count,
                cursor="", extras=None,
            )
        except Exception as e:
            logger.warning("search 失败 [%s/%s]: %s", pdef.id, keyword, e)
            print(f"   ❌ [{pdef.id}/{keyword}] 失败：{e}")
            return [(pdef.id, keyword, [])]
    if getattr(result, "error", None):
        logger.warning("search 返回错误 [%s/%s]: %s", pdef.id, keyword, result.error)
        print(f"   ❌ [{pdef.id}/{keyword}] {str(result.error)[:120]}")
        return [(pdef.id, keyword, [])]
    posts = (result.metadata or {}).get("posts", []) or []
    print(f"   ✅ [{pdef.id}/{keyword}] 搜到 {len(posts)} 条")
    return [(pdef.id, keyword, posts)]


async def _search_batch(pdef, queries: List[str], max_count: int, sem: asyncio.Semaphore):
    """批量搜索：多关键词一次调用（只开一次浏览器），按 source_keyword 归因。

    返回 [(platform_id, keyword, posts), ...]。失败降级为空。
    """
    async with sem:
        try:
            result = await pdef.search_batch_impl(
                platform_id=pdef.id, keywords=queries, max_count=max_count, extras=None,
            )
        except Exception as e:
            logger.warning("batch search 失败 [%s]: %s", pdef.id, e)
            print(f"   ❌ [{pdef.id}] 批量搜索失败：{e}")
            return [(pdef.id, queries[0], [])]
    if getattr(result, "error", None):
        logger.warning("batch search 返回错误 [%s]: %s", pdef.id, result.error)
        print(f"   ❌ [{pdef.id}] {str(result.error)[:120]}")
        return [(pdef.id, queries[0], [])]

    posts = (result.metadata or {}).get("posts", []) or []
    qset = set(queries)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for post in posts:
        sk = post.get("source_keyword")
        kw = sk if sk in qset else queries[0]   # 归因到具体关键词，兜底首词
        grouped.setdefault(kw, []).append(post)
    brief = "，".join(f"{k}:{len(v)}" for k, v in grouped.items()) or "0"
    print(f"   ✅ [{pdef.id}] 批量 {len(queries)} 词一次搜完，共 {len(posts)} 条（{brief}）")
    return [(pdef.id, kw, plist) for kw, plist in grouped.items()] or [(pdef.id, queries[0], [])]


async def search_all(
    platforms_: List[str], queries: List[str],
    max_count: int = 20, max_concurrent: int = 4,
) -> List[Dict[str, Any]]:
    """对所有 (platform × query) 组合并发搜索，去重后返回 source_dict 列表。

    每条 source_dict：
      case_id / platform / channel_content_id / source_url /
      post（平台原始详情）/ comments / found_by_queries（命中它的 query 列表）
    """
    pdefs = []
    for p in platforms_:
        pdef = get_platform(p.strip())
        if not pdef:
            avail = ", ".join(x.id for x in all_platforms())
            raise ValueError(f"未知平台 '{p}'。可用: {avail}")
        if not pdef.search_impl:
            raise ValueError(f"平台 '{p}' 不支持搜索")
        pdefs.append(pdef)

    sem = asyncio.Semaphore(max_concurrent)
    tasks = []
    for pdef in pdefs:
        if getattr(pdef, "search_batch_impl", None):
            tasks.append(_search_batch(pdef, queries, max_count, sem))  # 多词一次浏览器
        else:
            tasks.extend(_search_one(pdef, q, max_count, sem) for q in queries)
    print(f"🔎 搜索 {len(pdefs)} 平台 × {len(queries)} 关键词 = {len(tasks)} 次浏览器调用 (并发 {max_concurrent})")
    nested = await asyncio.gather(*tasks)
    results = [tup for group in nested for tup in group]   # 展平

    collected: Dict[tuple, Dict[str, Any]] = {}
    per_query: Dict[str, int] = {}
    for platform, query, posts in results:
        per_query[f"{platform}/{query}"] = len(posts)
        for post in posts:
            if not isinstance(post, dict):
                continue
            cid = _post_cid(post)
            if not cid:
                continue
            key = (platform, cid)
            if key in collected:
                if query not in collected[key]["found_by_queries"]:
                    collected[key]["found_by_queries"].append(query)
                continue
            link = post.get("link") or post.get("url") or ""
            collected[key] = {
                "case_id": f"{platform}_{cid}",
                "platform": platform,
                "channel_content_id": cid,
                "source_url": link,
                "post": post,
                "comments": post.get("author_comments", []) or [],
                "found_by_queries": [query],
            }

    print(f"   去重后唯一内容：{len(collected)} 条")
    return list(collected.values())
