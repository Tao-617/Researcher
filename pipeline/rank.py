"""阶段5：LLM 逐条评估 + 加权排序 + LLM 精排 → Top N（任务目标 #3 的排序部分）。

三步（DESIGN §5.3）：
  1) LLM 对每条候选给 relevance(0-10) / quality(0-10) / 一句话 reason（并发）。
  2) 按 config 权重把 relevance/quality/归一化互动数 加权成 final_score，砍到一个小候选池
     （rerank_pool，略大于 top_n）。
  3) 让 LLM 对这个小池子做一次点对点 rerank/复核：纠正纯加权的偏差，给出最终顺序 +
     一句话推荐理由（覆盖逐条阶段的 reason）。

为什么这样分：加权快且稳定地把几百条砍到 5~8 条（廉价、可解释）；LLM 只在小集合上做精排，
既控成本又拿到"人能看懂的排序理由"。rerank 失败/关闭时回退纯加权顺序（fail-open）。
排序权重交给运营在 config 里调，不写死进 prompt（见 [[feedback_prompt_design]] 的思路）。
"""

import asyncio
import base64
import json
import logging
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from llm.llm_helper import call_llm_with_retry

logger = logging.getLogger(__name__)

# 各平台图片防盗链需要的 Referer（否则 CDN 返回 403/被拒）。
_IMG_REFERER = {
    "xhs": "https://www.xiaohongshu.com", "bili": "https://www.bilibili.com",
    "douyin": "https://www.douyin.com", "kuaishou": "https://www.kuaishou.com",
    "weibo": "https://weibo.com", "zhihu": "https://www.zhihu.com",
}
_IMG_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_IMG_MAX_BYTES = 4_000_000  # 单图上限，避免超大图撑爆请求


def _post_brief(post: Dict[str, Any], max_body: int = 600) -> str:
    """把帖子压成给 LLM 评估的纯文本摘要。"""
    title = post.get("title") or ""
    body = (post.get("body_text") or "")[:max_body]
    return f"标题：{title}\n正文：{body}".strip()


def _validate_score(data: dict) -> Optional[str]:
    for k in ("relevance", "quality"):
        v = data.get(k)
        if not isinstance(v, (int, float)) or not (0 <= v <= 10):
            return f"{k} 必须是 0-10 的数字"
    if not isinstance(data.get("reason"), str) or not data["reason"].strip():
        return "reason 必须是非空字符串"
    return None


def _post_images(post: Dict[str, Any], max_images: int) -> List[str]:
    """取前 max_images 张可用图片 URL（视频帖这里就是封面帧）。"""
    out = []
    for u in (post.get("images") or []):
        if isinstance(u, str) and u.startswith("http"):
            out.append(u)
        if len(out) >= max_images:
            break
    return out


async def _fetch_data_url(client: httpx.AsyncClient, url: str, platform: str) -> Optional[str]:
    """自己带 Referer 抓图 → base64 data URL（绕开防盗链；Gemini 无需再去抓远端 URL）。"""
    headers = {"User-Agent": _IMG_UA}
    ref = _IMG_REFERER.get(platform)
    if ref:
        headers["Referer"] = ref
    try:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        if len(r.content) > _IMG_MAX_BYTES:
            return None
        ct = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
        if not ct.startswith("image/"):
            ct = "image/jpeg"
        return f"data:{ct};base64," + base64.b64encode(r.content).decode()
    except Exception:
        return None


async def _images_as_data_urls(urls: List[str], platform: str) -> List[str]:
    if not urls:
        return []
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        results = await asyncio.gather(*[_fetch_data_url(client, u, platform) for u in urls])
    return [d for d in results if d]


async def _score_one(
    source: Dict[str, Any], requirement: str,
    llm_call: Callable, model: str, sem: asyncio.Semaphore,
    multimodal: bool = False, max_images: int = 3,
) -> Tuple[Dict[str, Any], float]:
    """评一条，返回 (scores, cost)。失败给 0 分并标 error。

    multimodal=True 且该帖有图时，把封面/图片一并发给 LLM（让它"看"视觉内容——
    视频帖发的是封面帧）。多模态调用失败时自动回退纯文本重评，不让视频/图文帖丢分。
    """
    post = source.get("post", {}) or {}
    is_video = bool(post.get("videos"))
    # 自抓图转 base64（带 Referer 绕防盗链）。抓不到就当无图，走纯文本。
    imgs = (await _images_as_data_urls(_post_images(post, max_images), source.get("platform", ""))
            if multimodal else [])

    system = (
        "你是内容运营的选题评审。针对运营的采集需求，判断单条内容的两点："
        "relevance=与需求的相关程度，quality=作为选题参考的内容质量（信息量/可操作性/清晰度）。"
        "若附了图片（视频帖为其封面帧），请结合画面质量/信息量综合判断 quality。"
        "各 0-10。只输出 JSON：{\"relevance\":n,\"quality\":n,\"reason\":\"一句话理由\"}"
    )
    kind = "视频" if is_video else "图文"
    user_text = (
        f"【采集需求】\n{requirement}\n\n"
        f"【平台】{source.get('platform')}　【命中关键词】{source.get('found_by_queries')}\n"
        f"【内容{'｜' + kind + '，下附' + ('封面帧' if is_video else '图片') if imgs else ''}】\n"
        f"{_post_brief(post)}\n\n只输出 JSON。"
    )

    async def _call(with_imgs: bool) -> Tuple[Optional[dict], float]:
        if with_imgs and imgs:
            content: Any = [{"type": "text", "text": user_text}] + \
                [{"type": "image_url", "image_url": {"url": u}} for u in imgs]
        else:
            content = user_text
        async with sem:
            return await call_llm_with_retry(
                llm_call=llm_call,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": content}],
                model=model, temperature=0.1, max_tokens=400,
                validate_fn=_validate_score, task_name="Score",
            )

    data, cost = await _call(with_imgs=bool(imgs))
    if not data and imgs:                     # 多模态失败（如图片防盗链拉取失败）→ 回退纯文本
        logger.info("多模态评分失败，回退纯文本：%s", source.get("case_id"))
        data2, cost2 = await _call(with_imgs=False)
        cost += cost2
        data = data2 or data

    if not data:
        return {"relevance": 0, "quality": 0, "reason": "评估失败", "_error": True}, cost
    return {"relevance": float(data["relevance"]), "quality": float(data["quality"]),
            "reason": data["reason"].strip()}, cost


def _engagement(post: Dict[str, Any]) -> float:
    """互动热度原始值（赞+评+藏），log 压缩前。"""
    def _n(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0
    return _n(post.get("like_count")) + _n(post.get("comment_count")) + _n(post.get("collect_count"))


def _validate_rerank(data: dict, pool_n: int) -> Optional[str]:
    order = data.get("ranking")
    if not isinstance(order, list) or not order:
        return "ranking 必须是非空数组"
    seen = set()
    for item in order:
        if not isinstance(item, dict):
            return "ranking 每项必须是对象 {index, reason}"
        idx = item.get("index")
        if not isinstance(idx, int) or not (0 <= idx < pool_n):
            return f"index {idx} 越界（应在 0-{pool_n - 1}）"
        if idx in seen:
            return f"index {idx} 重复"
        seen.add(idx)
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            return "每项必须有非空 reason"
    return None


async def _llm_rerank(
    pool: List[Dict[str, Any]], requirement: str,
    llm_call: Callable, model: str, top_n: int,
) -> Tuple[List[Tuple[int, str]], float]:
    """对候选池做点对点精排。返回 ([(pool下标, 推荐理由), ...] 最多 top_n 条, cost)。

    失败回退空列表，调用方据此沿用加权顺序（fail-open）。
    """
    listing = "\n".join(
        f"{i}. [{s.get('platform')}] 加权分={s['scores']['final']} "
        f"rel={s['scores']['relevance']} q={s['scores']['quality']} "
        f"eng={s['scores']['engagement']}\n   {_post_brief(s.get('post', {}) or {}, max_body=200)}"
        for i, s in enumerate(pool)
    )
    system = (
        "你是内容运营的选题终审。下面是已按加权分粗排的候选（编号. [平台] 各项分数 + 摘要）。"
        "请通盘对比、纠正纯加权可能的偏差（如高赞但跑题、低赞但极契合需求），"
        f"挑出最值得参考的前 {top_n} 条并排出最终顺序。"
        "只输出 JSON：{\"ranking\": [{\"index\": 编号, \"reason\": \"一句话推荐理由\"}, ...]}，"
        "按推荐度从高到低，编号不重复。"
    )
    user = (
        f"【采集需求】\n{requirement}\n\n"
        f"【候选（共 {len(pool)} 条）】\n{listing}\n\n"
        f"请输出最终前 {min(top_n, len(pool))} 名。只输出 JSON。"
    )
    data, cost = await call_llm_with_retry(
        llm_call=llm_call,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        model=model, temperature=0.1, max_tokens=1200,
        validate_fn=lambda d: _validate_rerank(d, len(pool)), task_name="Rerank",
    )
    if not data:
        logger.warning("LLM 精排失败，沿用加权顺序")
        return [], cost
    return [(item["index"], item["reason"].strip()) for item in data["ranking"][:top_n]], cost


async def rank_top(
    sources: List[Dict[str, Any]], requirement: str,
    llm_call: Callable, model: str,
    weights: Dict[str, float], top_n: int = 5,
    max_concurrent: int = 4,
    rerank: bool = True, rerank_pool: Optional[int] = None,
    multimodal: bool = False, max_images: int = 3,
) -> Tuple[List[Dict[str, Any]], float]:
    """评分 + 加权排序 + LLM 精排，返回 (ranked_sources_top_n, total_cost)。

    每条 source 会被挂上 'scores' = {relevance, quality, engagement, final, reason}。
    rerank=True 时多走一步 LLM 精排（reason 改为推荐理由，原逐条理由存 eval_reason）。
    rerank_pool 是送 LLM 精排的候选数（缺省 top_n+3），略大于 top_n 才能让精排"提拔"被加权低估的候选。
    """
    if not sources:
        return [], 0.0

    sem = asyncio.Semaphore(max_concurrent)
    mm = " +多模态" if multimodal else ""
    print(f"🧠 LLM 评分 {len(sources)} 条 (并发 {max_concurrent}{mm}) ...")
    results = await asyncio.gather(*[
        _score_one(s, requirement, llm_call, model, sem, multimodal, max_images)
        for s in sources
    ])

    total_cost = sum(c for _, c in results)
    # 互动数归一化：log1p 后除以本批最大值，压到 0-1，避免头部爆款碾压一切。
    raw_eng = [_engagement(s.get("post", {}) or {}) for s in sources]
    log_eng = [math.log1p(e) for e in raw_eng]
    max_log = max(log_eng) or 1.0

    w_rel = weights.get("relevance", 0.5)
    w_qual = weights.get("quality", 0.3)
    w_eng = weights.get("engagement", 0.2)

    for s, (score, _), le in zip(sources, results, log_eng):
        eng_norm = le / max_log
        final = (score["relevance"] / 10 * w_rel
                 + score["quality"] / 10 * w_qual
                 + eng_norm * w_eng)
        score["engagement"] = round(eng_norm, 3)
        score["final"] = round(final, 4)
        s["scores"] = score

    ranked = sorted(sources, key=lambda x: x["scores"]["final"], reverse=True)

    # 候选池：略大于 top_n，给 LLM 精排"提拔"被加权低估者的空间。
    pool_size = min(len(ranked), max(top_n, rerank_pool if rerank_pool else top_n + 3))
    pool = ranked[:pool_size]

    if rerank and len(pool) > 1:
        print(f"🎯 LLM 精排 {len(pool)} 进 {top_n} ...")
        order, c = await _llm_rerank(pool, requirement, llm_call, model, top_n)
        total_cost += c
        if order:
            top, used = [], set()
            for idx, reason in order:
                s = pool[idx]
                s["scores"]["eval_reason"] = s["scores"].get("reason")
                s["scores"]["reason"] = reason       # reason 改为精排推荐理由
                s["scores"]["reranked"] = True
                top.append(s)
                used.add(idx)
            # LLM 给少了就用加权顺序补满 top_n
            for i, s in enumerate(pool):
                if len(top) >= top_n:
                    break
                if i not in used:
                    top.append(s)
        else:
            top = pool[:top_n]   # 精排失败，回退加权
    else:
        top = pool[:top_n]

    for i, s in enumerate(top, 1):
        s["rank"] = i
        sc = s["scores"]
        flag = "✦" if sc.get("reranked") else " "
        print(f"  {flag}#{i} final={sc['final']} rel={sc['relevance']} q={sc['quality']} "
              f"eng={sc['engagement']} [{s['platform']}] {(s.get('post', {}) or {}).get('title', '')[:24]}")
    print(f"   评分+精排成本 ${total_cost:.4f}")
    return top, total_cost
