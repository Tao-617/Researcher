"""阶段6：标准化结构化输出（任务目标 #4）。

把内部 source_dict 收敛成对外稳定的 result schema。运营/下游只认这份契约，
内部字段怎么变都不影响外部。
"""

from typing import Any, Dict, List, Optional


def _first(post: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        v = post.get(k)
        if v:
            return v
    return None


def _author(post: Dict[str, Any]) -> Dict[str, Any]:
    """防御式抽取账号信息（搜索摘要里常缺，detail 才全）。"""
    return {
        "id": _first(post, ["author_id", "user_id", "uid"]),
        "name": _first(post, ["author_name", "nickname", "author", "user_name"]),
        "follower_count": _first(post, ["follower_count", "fans_count"]),
    }


def _metrics(post: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "like": post.get("like_count"),
        "comment": post.get("comment_count"),
        "collect": post.get("collect_count"),
        "share": post.get("share_count"),
    }


def _images(post: Dict[str, Any], n: int) -> List[str]:
    """抽前 n 张图片 URL（兼容 str / {url|link} 两种元素）。n 给大就是"全部"。"""
    imgs = post.get("images") or []
    out = []
    for x in imgs:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            u = x.get("url") or x.get("link")
            if u:
                out.append(u)
        if len(out) >= n:
            break
    return out


def _cover_images(post: Dict[str, Any], n: int = 3) -> List[str]:
    return _images(post, n)


def _comments(source: Dict[str, Any], n: int = 20) -> List[str]:
    """把评论压成纯文本列表（评论 schema 各平台不一，按常见键防御式取文本）。"""
    out = []
    for c in (source.get("comments") or []):
        if isinstance(c, str):
            txt = c
        elif isinstance(c, dict):
            txt = c.get("content") or c.get("text") or c.get("comment") or c.get("body") or ""
        else:
            txt = ""
        txt = (txt or "").strip()
        if txt:
            out.append(txt[:200])
        if len(out) >= n:
            break
    return out


def _publish_time(post: Dict[str, Any]) -> Optional[Any]:
    return _first(post, ["publish_time", "publish_timestamp", "create_time", "time", "date"])


def _scores_out(scores: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "relevance": scores.get("relevance"),
        "quality": scores.get("quality"),
        "engagement": scores.get("engagement"),
        "final": scores.get("final"),
    }


def _post_detail(source: Dict[str, Any], stage: str,
                 drop_reason: Optional[str] = None) -> Dict[str, Any]:
    """单条原帖的完整详情（前端列表 + 详情页都用这一份）。

    stage: top | candidate | filtered | deduped —— 决定前端怎么标记/是否盖"已过滤"。
    保留原文正文 / 全部图片 / 评论，使前端能展示"帖子原文"。
    """
    post = source.get("post", {}) or {}
    scores = source.get("scores") or {}
    return {
        "case_id": source.get("case_id"),
        "platform": source.get("platform"),
        "title": post.get("title") or (post.get("body_text") or "")[:30],
        "source_url": source.get("source_url"),
        "author": _author(post),
        "metrics": _metrics(post),
        "publish_time": _publish_time(post),
        "found_by_queries": source.get("found_by_queries", []),
        "stage": stage,
        "rank": source.get("rank"),
        "scores": _scores_out(scores) if scores else None,
        "reason": scores.get("reason"),          # 评分/精排推荐理由（有评分才有）
        "drop_reason": drop_reason,              # 被过滤/去重的原因（被淘汰才有）
        "body_text": post.get("body_text") or "",
        "images": _images(post, 12),
        "comments": _comments(source),
        "dedup": source.get("dedup", {"group_id": None, "merged_from": []}),
    }


def _item(source: Dict[str, Any]) -> Dict[str, Any]:
    post = source.get("post", {}) or {}
    scores = source.get("scores", {}) or {}
    return {
        "rank": source.get("rank"),
        "case_id": source.get("case_id"),
        "platform": source.get("platform"),
        "title": post.get("title") or (post.get("body_text") or "")[:30],
        "source_url": source.get("source_url"),
        "author": _author(post),
        "metrics": _metrics(post),
        "scores": {
            "relevance": scores.get("relevance"),
            "quality": scores.get("quality"),
            "engagement": scores.get("engagement"),
            "final": scores.get("final"),
        },
        "reason": scores.get("reason"),
        "cover_images": _cover_images(post),
        "found_by_queries": source.get("found_by_queries", []),
        "dedup": source.get("dedup", {"group_id": None, "merged_from": []}),
    }


def build_result(
    query: str, requirement: str, config: Dict[str, Any],
    platforms: List[str], stats: Dict[str, int],
    top: List[Dict[str, Any]],
    dropped_filter: List[Dict[str, Any]], dropped_dedup: List[Dict[str, Any]],
    all_sources: List[Dict[str, Any]],
    eval_model: str, cost: float,
) -> Dict[str, Any]:
    """组装对外结果。

    posts: 每一条搜到的原帖（全文/图片/评论 + 它在管线里的归宿 stage），前端列表与详情页的数据源。
    top:   Top N 的精简卡片（向后兼容）。dropped: 被淘汰条目的 (case_id, stage, reason)。
    """
    filt_reason = {d["case_id"]: d["reason"] for d in dropped_filter}
    dedup_reason = {d["case_id"]: d["reason"] for d in dropped_dedup}
    top_ids = {s["case_id"] for s in top}

    posts = []
    for s in all_sources:
        cid = s.get("case_id")
        if cid in filt_reason:
            posts.append(_post_detail(s, "filtered", filt_reason[cid]))
        elif cid in dedup_reason:
            posts.append(_post_detail(s, "deduped", dedup_reason[cid]))
        elif cid in top_ids:
            posts.append(_post_detail(s, "top"))
        else:
            posts.append(_post_detail(s, "candidate"))

    # top 在前、candidate 次之、被淘汰的最后；同档按 rank/最终分降序
    order = {"top": 0, "candidate": 1, "deduped": 2, "filtered": 3}
    posts.sort(key=lambda p: (order.get(p["stage"], 9),
                              p["rank"] if p.get("rank") else 999,
                              -((p.get("scores") or {}).get("final") or 0)))

    return {
        "query": query,
        "requirement": requirement,
        "config_snapshot": {"filters": config.get("filters", []),
                            "rank": config.get("rank", {})},
        "platforms": platforms,
        "eval_model": eval_model,
        "cost": round(cost, 4),
        "stats": stats,
        "top": [_item(s) for s in top],
        "posts": posts,
        "dropped": dropped_filter + dropped_dedup,
    }
