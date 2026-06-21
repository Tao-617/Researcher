"""冒烟测试：跑通一次真实搜索，验证 (1) 自依赖 import 链 (2) 远端后端可达。

用法（在仓库根目录执行）：
  PYTHONIOENCODING=utf-8 python tests/smoke_search.py [platform] [keyword]
默认 xhs / 增肌。
"""

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent  # tests/ 的上一级 = 仓库根
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))  # 让 `from vendor...` / `from platforms...` 解析到 researcher 自己

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")  # 项目根 .env（xhs 后端无需 key，但其它平台/评估会用到）
except Exception:
    pass

import platforms._backends  # noqa: F401  容错加载所有可用平台后端
from platforms.registry import get_platform, all_platforms


async def main() -> None:
    platform = sys.argv[1] if len(sys.argv) > 1 else "xhs"
    keyword = sys.argv[2] if len(sys.argv) > 2 else "增肌"

    print(f"已注册平台: {[p.id for p in all_platforms()]}")
    pdef = get_platform(platform)
    if not pdef or not pdef.search_impl:
        print(f"❌ 平台 {platform} 不可搜索"); return

    print(f"🔎 搜索 {platform} / '{keyword}' ...")
    res = await pdef.search_impl(platform_id=platform, keyword=keyword,
                                 max_count=5, cursor="", extras=None)
    if res.error:
        print(f"❌ 搜索返回错误: {res.error}"); return

    posts = (res.metadata or {}).get("posts", []) or []
    print(f"✅ 命中 {len(posts)} 条")
    for i, p in enumerate(posts[:3], 1):
        title = (p.get("title") or p.get("body_text") or "")[:40]
        print(f"  {i}. [{p.get('channel')}] like={p.get('like_count')} {title}")
    if posts:
        print(f"\n首条 post 字段: {sorted(posts[0].keys())}")


if __name__ == "__main__":
    asyncio.run(main())
