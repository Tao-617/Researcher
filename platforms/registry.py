"""
内容平台注册表

定义所有支持的内容平台及其搜索参数 schema。
供 content_platforms / content_search / content_detail 路由使用。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

from vendor.tool_result import ToolResult


# ── 类型定义 ──

@dataclass
class ParamSpec:
    """平台专属参数的描述"""
    values: Optional[List[str]] = None   # 枚举值（None 表示自由文本）
    default: Optional[str] = None
    note: str = ""                       # 额外说明

    def to_dict(self) -> dict:
        d: dict = {}
        if self.values is not None:
            d["values"] = self.values
            d["default"] = self.default
        if self.note:
            d["note"] = self.note
        return d


# 平台实现函数的签名
SearchFunc = Callable[..., Coroutine[Any, Any, ToolResult]]
DetailFunc = Callable[..., Coroutine[Any, Any, ToolResult]]
SuggestFunc = Callable[..., Coroutine[Any, Any, ToolResult]]


@dataclass
class PlatformDef:
    """一个内容平台的完整定义"""
    id: str                                         # 唯一标识，如 "xhs"
    name: str                                       # 显示名，如 "小红书"
    aliases: List[str] = field(default_factory=list) # 模糊匹配别名，如 ["小红书", "RED"]
    search_params: Dict[str, ParamSpec] = field(default_factory=dict)
    detail_extras: Dict[str, ParamSpec] = field(default_factory=dict)
    supports_suggest: bool = False
    suggest_channels: Optional[List[str]] = None     # suggest API 的 channel 值（可能与 id 不同）

    # 平台实现函数（运行时由 platforms/ 模块设置）
    search_impl: Optional[SearchFunc] = None
    search_batch_impl: Optional[SearchFunc] = None  # 可选：多关键词一次性搜索（减少浏览器启动）
    detail_impl: Optional[DetailFunc] = None
    suggest_impl: Optional[SuggestFunc] = None

    def summary(self) -> dict:
        """概要信息（不含参数细节）"""
        d = {"id": self.id, "name": self.name}
        if self.search_params:
            d["has_search_params"] = True
        if self.detail_extras:
            d["has_detail_extras"] = True
        if self.supports_suggest:
            d["supports_suggest"] = True
        return d

    def detail(self) -> dict:
        """完整参数说明"""
        d = self.summary()
        if self.search_params:
            d["search_params"] = {k: v.to_dict() for k, v in self.search_params.items()}
        if self.detail_extras:
            d["detail_extras"] = {k: v.to_dict() for k, v in self.detail_extras.items()}
        return d


# ── 平台注册表 ──

_PLATFORMS: Dict[str, PlatformDef] = {}


def register_platform(p: PlatformDef) -> None:
    _PLATFORMS[p.id] = p


def get_platform(platform_id: str) -> Optional[PlatformDef]:
    return _PLATFORMS.get(platform_id)


def all_platforms() -> List[PlatformDef]:
    return list(_PLATFORMS.values())


def match_platforms(query: str) -> List[PlatformDef]:
    """
    模糊匹配平台：精确 ID > 别名包含 > token 交集。
    空 query 返回全部。
    """
    if not query:
        return all_platforms()

    q = query.strip().lower()

    # 1) 精确 ID 匹配
    if q in _PLATFORMS:
        return [_PLATFORMS[q]]

    # 2) 别名 / 名称包含匹配
    alias_hits = [
        p for p in _PLATFORMS.values()
        if q in p.name.lower() or any(q in a.lower() for a in p.aliases)
    ]
    if alias_hits:
        return alias_hits

    # 3) token 交集（把 query 拆成字符/词，看命中率）
    q_tokens = set(q.replace("_", " ").replace("-", " ").split())
    scored = []
    for p in _PLATFORMS.values():
        pool = {p.id, p.name.lower()} | {a.lower() for a in p.aliases}
        pool_text = " ".join(pool)
        hits = sum(1 for t in q_tokens if t in pool_text)
        if hits > 0:
            scored.append((hits, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored]
