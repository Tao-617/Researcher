"""阶段3：配置化硬筛选（任务目标 #2「筛选规则支持配置」）。

声明式规则：每条规则 = {field, op, value}。在送 LLM 之前先做廉价、确定性、可解释的过滤，
省钱且每条淘汰都带原因。主观相关性判断留给 LLM（见 rank.py），两者职责不混。

字段用点号路径取值，如 post.like_count / post.body_text / found_by_queries。

算子：
  gte / lte / eq            数值或可比较
  within_days               时间戳/秒在最近 N 天内
  min_len / max_len         字符串/列表长度
  contains_any              字符串含任一关键词（列表）
  not_contains_any          字符串不含任何关键词（列表）
  regex                     正则匹配
  nonempty                  非空
"""

import re
import time
from typing import Any, Dict, List, Tuple


def _get(obj: Any, path: str) -> Any:
    """点号路径取值，缺失返回 None。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _to_epoch(v: Any) -> float:
    """把时间戳归一到「秒」。支持毫秒(>1e12)、秒、可读字符串(YYYY-MM-DD ...)。"""
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1_000_000_000_000 else float(v)
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return time.mktime(time.strptime(v[:len(fmt) + 2].strip(), fmt))
            except Exception:
                continue
    return 0.0


def _check(value: Any, op: str, target: Any) -> bool:
    """单条规则判定。无法判定（类型不符）按未通过处理，调用方据此给原因。"""
    if op == "nonempty":
        return bool(value)
    if value is None:
        return False

    if op in ("gte", "lte", "eq"):
        try:
            v, t = float(value), float(target)
        except (TypeError, ValueError):
            return False
        return v >= t if op == "gte" else v <= t if op == "lte" else v == t

    if op == "within_days":
        if not value:
            return False
        return (time.time() - _to_epoch(value)) <= float(target) * 86400

    if op in ("min_len", "max_len"):
        try:
            n = len(value)
        except TypeError:
            return False
        return n >= int(target) if op == "min_len" else n <= int(target)

    if op == "contains_any":
        s = str(value)
        return any(str(k) in s for k in target)

    if op == "not_contains_any":
        s = str(value)
        return not any(str(k) in s for k in target)

    if op == "regex":
        return re.search(str(target), str(value)) is not None

    raise ValueError(f"未知算子: {op}")


def _reason(rule: Dict[str, Any], value: Any) -> str:
    f, op, val = rule["field"], rule["op"], rule.get("value")
    shown = (str(value)[:30] + "…") if isinstance(value, str) and len(str(value)) > 30 else value
    return f"{f} ({shown!r}) 未满足 {op} {val!r}"


def apply_filters(
    sources: List[Dict[str, Any]], rules: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """对每条 source 顺序跑全部规则，全部通过才保留。

    返回 (kept, dropped)。dropped 每条形如：
      {"case_id": ..., "stage": "filter", "reason": "like_count (32) 未满足 gte 100"}
    """
    if not rules:
        return list(sources), []

    kept, dropped = [], []
    for s in sources:
        fail = None
        for rule in rules:
            value = _get(s, rule["field"])
            if not _check(value, rule["op"], rule.get("value")):
                fail = _reason(rule, value)
                break
        if fail:
            dropped.append({"case_id": s.get("case_id"), "stage": "filter", "reason": fail})
        else:
            kept.append(s)

    print(f"🧹 硬筛选：{len(sources)} → 保留 {len(kept)} / 淘汰 {len(dropped)}")
    return kept, dropped
