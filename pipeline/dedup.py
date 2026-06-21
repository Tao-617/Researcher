"""阶段4：LLM 语义去重（任务目标 #3「借助 LLM 对结果去重」）。

search 已按 (platform, content_id) 精确去重，但抓不到「跨平台搬运 / 换皮同选题」。
这里给 LLM 一份 (编号, 平台, 标题) 清单，让它把指向同一内容/同一选题的归为一组，
每组只保留信息量最高的一条，其余记为被去重（保留痕迹便于复核）。

候选集通常已被硬筛选砍小，单次调用即可，成本低。
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm.llm_helper import call_llm_with_retry

logger = logging.getLogger(__name__)


def _validate_groups(data: dict, n: int) -> Optional[str]:
    groups = data.get("groups")
    if not isinstance(groups, list):
        return "groups 必须是数组"
    seen = set()
    for g in groups:
        if not isinstance(g, list) or not g:
            return "每个 group 必须是非空数组"
        for i in g:
            if not isinstance(i, int) or not (0 <= i < n):
                return f"编号 {i} 越界（应在 0-{n-1}）"
            seen.add(i)
    return None


async def llm_dedup(
    sources: List[Dict[str, Any]],
    llm_call: Callable, model: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """返回 (kept, dropped, cost)。kept 每条挂 dedup={group_id, merged_from:[case_id...]}。"""
    if len(sources) < 2:
        return list(sources), [], 0.0

    listing = "\n".join(
        f"{i}. [{s.get('platform')}] {((s.get('post', {}) or {}).get('title') or '')[:50]}"
        for i, s in enumerate(sources)
    )
    system = (
        "你是内容去重助手。下面每行是一条内容（编号. [平台] 标题）。"
        "把指向同一内容或同一选题（标题高度相似/搬运/换皮）的编号归到同一组；"
        "只出现一次、无重复的编号也各自单独成组。"
        "只输出 JSON：{\"groups\": [[0,3],[1],[2,5],...]}，每个编号必须且只出现一次。"
    )
    user = f"共 {len(sources)} 条：\n{listing}\n\n只输出 JSON。"

    data, cost = await call_llm_with_retry(
        llm_call=llm_call,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        model=model, temperature=0.0, max_tokens=2000,
        validate_fn=lambda d: _validate_groups(d, len(sources)), task_name="Dedup",
    )
    if not data:
        logger.warning("LLM 去重失败，跳过（保留全部）")
        return list(sources), [], cost

    # 补齐 LLM 漏掉的编号（各自单独成组），保证覆盖完整
    covered = {i for g in data["groups"] for i in g}
    groups = list(data["groups"]) + [[i] for i in range(len(sources)) if i not in covered]

    kept, dropped = [], []
    for gid, g in enumerate(groups):
        # 组内保留正文最长的一条（信息量代理）
        members = sorted(g, key=lambda i: len((sources[i].get("post", {}) or {}).get("body_text", "") or ""),
                         reverse=True)
        keep_i, merged = members[0], members[1:]
        s = sources[keep_i]
        s["dedup"] = {"group_id": gid,
                      "merged_from": [sources[i]["case_id"] for i in merged]}
        kept.append(s)
        for i in merged:
            dropped.append({"case_id": sources[i]["case_id"], "stage": "dedup",
                            "reason": f"与 {s['case_id']} 同组重复"})

    print(f"🔗 语义去重：{len(sources)} → 保留 {len(kept)} / 合并 {len(dropped)}")
    return kept, dropped, cost
