"""researcher 采集模块编排器：prompt → 搜索 → 硬筛选 → LLM去重 → 评分排序 → 标准化输出。

CLI:
  PYTHONIOENCODING=utf-8 python -m pipeline.run \
      --query "AI 健身 增肌 计划" --platforms xhs --output-dir runs/demo

也可被 server.py 直接 import 调用 run_pipeline(...)。
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parents[1]   # researcher/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from llm.providers import build_llm_call, DEFAULT_MODEL
from pipeline.query import expand_queries
from pipeline.search import search_all
from pipeline.filters import apply_filters
from pipeline.dedup import llm_dedup
from pipeline.rank import rank_top
from pipeline.schema import build_result

logger = logging.getLogger(__name__)


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg_path = Path(path) if path else (_HERE / "config.yaml")
    if not cfg_path.exists():
        return {}
    import yaml
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _slug(text: str) -> str:
    return re.sub(r"\s+", "_", text.strip())[:40] or "run"


async def run_pipeline(
    query: str,
    requirement: str = "",
    config: Optional[Dict[str, Any]] = None,
    platforms: Optional[List[str]] = None,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """跑完整管线，返回 result dict（同时写 result.json 到 output_dir）。"""
    config = config or {}
    requirement = requirement or query
    platforms = platforms or config.get("platforms", ["xhs"])
    eval_model = config.get("eval_model", DEFAULT_MODEL)
    rank_cfg = config.get("rank", {}) or {}
    weights = rank_cfg.get("weights", {"relevance": 0.5, "quality": 0.3, "engagement": 0.2})
    top_n = int(rank_cfg.get("top_n", 5))

    llm_call, model_id = build_llm_call(eval_model)
    total_cost = 0.0

    print(f"📋 需求: {requirement[:80]}")
    print(f"📡 平台: {platforms} | 模型: {model_id}")

    # 1. prompt → 关键词
    if config.get("expand_query", True):
        queries, c = await expand_queries(query, [query], llm_call, model_id,
                                          config.get("expand_count", 8))
        total_cost += c
        print(f"✍️  扩展关键词 ({len(queries)}): {queries}")
    else:
        queries = [query]

    # 2. 多平台搜索 + 精确去重（all_sources 保留全量原帖，供前端展示原文）
    all_sources = await search_all(platforms, queries,
                                   max_count=int(config.get("max_count_per_query", 20)))
    n_searched = len(all_sources)

    # 3. 配置化硬筛选（kept 与 all_sources 共享同一批 dict 对象，后续阶段原地标注）
    kept, dropped_filter = apply_filters(all_sources, config.get("filters", []))
    n_after_filter = len(kept)

    # 4. LLM 语义去重
    kept, dropped_dedup, c = await llm_dedup(kept, llm_call, model_id)
    total_cost += c
    n_after_dedup = len(kept)

    # 5. 评分 + 加权排序 + LLM 精排 → Top N（评分并发偏小：Gemini 端在高并发下会掐连接）
    top, c = await rank_top(kept, requirement, llm_call, model_id, weights, top_n,
                            max_concurrent=int(config.get("eval_concurrent", 3)),
                            rerank=rank_cfg.get("rerank", True),
                            rerank_pool=int(rank_cfg.get("rerank_pool", top_n + 3)))
    total_cost += c

    # 6. 标准化输出
    stats = {"searched": n_searched, "after_filter": n_after_filter,
             "after_dedup": n_after_dedup, "top_n": len(top)}
    result = build_result(query, requirement, config, platforms, stats,
                          top, dropped_filter, dropped_dedup, all_sources,
                          model_id, total_cost)

    out_dir = Path(output_dir) if output_dir else (_HERE / "runs" / _slug(query))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "result.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"💾 result.json → {out_file}")
    print(f"💰 累计成本: ${total_cost:.4f}")
    return result


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="researcher 采集模块")
    ap.add_argument("--query", required=True, help="运营的自然语言采集 prompt")
    ap.add_argument("--requirement", default="", help="评估用需求描述（缺省取 query）")
    ap.add_argument("--platforms", default="", help="逗号分隔覆盖配置，如 xhs,douyin")
    ap.add_argument("--config", default="", help="config.yaml 路径（默认 researcher/config.yaml）")
    ap.add_argument("--output-dir", default="", help="输出目录（默认 runs/<query>）")
    ap.add_argument("--no-expand", action="store_true", help="不做 LLM 关键词扩展")
    ap.add_argument("--no-rerank", action="store_true", help="不做 LLM 精排（只加权排序，省钱）")
    args = ap.parse_args()

    config = load_config(args.config or None)
    if args.no_expand:
        config["expand_query"] = False
    if args.no_rerank:
        config.setdefault("rank", {})["rerank"] = False
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()] or None

    asyncio.run(run_pipeline(
        query=args.query, requirement=args.requirement, config=config,
        platforms=platforms, output_dir=args.output_dir or None,
    ))


if __name__ == "__main__":
    main()
