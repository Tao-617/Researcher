"""阶段1：运营的自然语言 prompt → 一组可直接搜索的关键词。

可选用 LLM 扩展（覆盖同义表达 / 具体工具名 / 典型场景）。
逻辑移植自 search_and_evaluate.py::generate_queries，去掉外部依赖。
"""

import json
import logging
from typing import Callable, List, Optional, Tuple

from llm.llm_helper import call_llm_with_retry

logger = logging.getLogger(__name__)


def _validate_gen(data: dict) -> Optional[str]:
    qs = data.get("queries")
    if not isinstance(qs, list) or not qs:
        return "queries 必须是非空数组"
    if not all(isinstance(q, str) and q.strip() for q in qs):
        return "queries 每一项必须是非空字符串"
    return None


async def expand_queries(
    prompt: str,
    base_queries: List[str],
    llm_call: Callable,
    model: str,
    target_count: int = 8,
) -> Tuple[List[str], float]:
    """让 LLM 基于运营 prompt 扩展出一组搜索词。返回 (queries, cost)。失败回退 base。"""
    system = (
        "你是内容采集的搜索词优化器。基于运营的采集需求，产出一组适合在社媒/内容平台"
        "搜索框直接使用的关键词：覆盖同义表达、具体工具名、典型用法场景，去掉过宽或重复的词。"
        "只输出一个 JSON 对象，不要解释、不要 markdown。"
    )
    user = (
        f"【采集需求】\n{prompt}\n\n"
        f"【已有关键词（可为空）】\n{json.dumps(base_queries, ensure_ascii=False)}\n\n"
        f"【要求】产出约 {target_count} 个搜索词，输出：\n"
        '{"queries": ["词1", "词2", ...]}\n'
        "词要简短（适合搜索框）、彼此不同、贴合需求主题。只输出 JSON。"
    )
    data, cost = await call_llm_with_retry(
        llm_call=llm_call,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        model=model, temperature=0.5, max_tokens=1500,
        validate_fn=_validate_gen, task_name="ExpandQuery",
    )
    if not data:
        logger.warning("query 扩展失败，回退原始关键词")
        return list(base_queries), cost
    out, seen = [], set()
    for q in (base_queries + data["queries"]):
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out, cost
