"""账号聚合视图（任务目标扩展：内容 → 候选账号）。

把全量原帖按 (platform, user_id) 归并成「候选账号」：统计发帖数、累计互动、命中关键词、
平均相关性/质量分、有多少条进了 Top N。让运营除了看单条内容，也能发现"值得长期关注的账号"。

数据来自搜索结果里的作者字段（nickname/user_id）。注意：关键词搜索摘要里通常**没有粉丝数**
（follower_count 多为空，要走平台的 creator 详情抓取才有），所以该字段可能为 None。
"""

from typing import Any, Dict, List, Optional, Set


def _n(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def aggregate_by_author(
    all_sources: List[Dict[str, Any]],
    top_ids: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """按作者归并，返回候选账号列表（按 top命中数 → 发帖数 → 互动 排序）。"""
    top_ids = top_ids or set()
    groups: Dict[tuple, Dict[str, Any]] = {}

    for s in all_sources:
        post = s.get("post", {}) or {}
        uid = post.get("user_id") or post.get("author_id") or post.get("nickname")
        if not uid:
            continue
        platform = s.get("platform")
        key = (platform, str(uid))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "author_id": str(uid),
                "author_name": post.get("nickname") or post.get("author_name") or str(uid),
                "platform": platform,
                "follower_count": None,
                "post_count": 0,
                "like_sum": 0, "comment_sum": 0, "collect_sum": 0,
                "top_count": 0,
                "queries": set(),
                "_score_sum": 0.0, "_scored": 0,
                "posts": [],
            }
        g["post_count"] += 1
        fc = post.get("follower_count") or post.get("fans_count")
        if fc and not g["follower_count"]:
            g["follower_count"] = fc
        g["like_sum"] += _n(post.get("like_count"))
        g["comment_sum"] += _n(post.get("comment_count"))
        g["collect_sum"] += _n(post.get("collect_count"))
        for q in (s.get("found_by_queries") or []):
            g["queries"].add(q)

        scores = s.get("scores") or {}
        final = scores.get("final")
        if isinstance(final, (int, float)):
            g["_score_sum"] += final
            g["_scored"] += 1
        is_top = s.get("case_id") in top_ids
        if is_top:
            g["top_count"] += 1
        g["posts"].append({
            "case_id": s.get("case_id"),
            "title": post.get("title") or (post.get("body_text") or "")[:30],
            "source_url": s.get("source_url"),
            "like": int(_n(post.get("like_count"))),
            "final": final,
            "is_top": is_top,
            "rank": s.get("rank"),
        })

    accounts: List[Dict[str, Any]] = []
    for g in groups.values():
        g["engagement_sum"] = int(g["like_sum"] + g["comment_sum"] + g["collect_sum"])
        g["avg_score"] = round(g["_score_sum"] / g["_scored"], 4) if g["_scored"] else None
        g["queries"] = sorted(g["queries"])
        g["posts"].sort(key=lambda p: ((p["final"] or 0), p["like"]), reverse=True)
        for k in ("_score_sum", "_scored"):
            g.pop(k, None)
        accounts.append(g)

    accounts.sort(
        key=lambda a: (a["top_count"], a["post_count"], a["engagement_sum"]),
        reverse=True,
    )
    return accounts
