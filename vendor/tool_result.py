"""
ToolResult —— 工具执行结果（从 agent/tools/models.py 原样裁剪）

researcher 自依赖：平台 search_impl / detail_impl 返回此类型。
只保留 researcher 用到的字段，去掉 browser/context 等无关项。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolResult:
    """工具执行结果。命中帖子在 metadata['posts']。"""

    title: str
    output: str

    long_term_memory: Optional[str] = None
    include_output_only_once: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    error: Optional[str] = None
    attachments: List[str] = field(default_factory=list)
    images: List[Dict[str, Any]] = field(default_factory=list)
    tool_usage: Optional[Dict[str, Any]] = None
