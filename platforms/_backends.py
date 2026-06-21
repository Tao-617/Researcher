"""按可用性加载平台后端 —— 缺失的后端不阻塞导入。

为什么需要它：`aigc_channel.py` 是公司内部接口、被 .gitignore 排除，公开 repo 里并不存在；
若像以前那样在 search.py / server.py 顶层硬 `import platforms.aigc_channel`，公开环境一导入就崩。
这里统一容错加载：有哪个后端就注册哪个。

加载顺序即注册优先级（register_platform 按 id 覆盖）：
  aigc_channel 先 → mediacrawler 后。两者在 xhs/douyin/bili/weibo/zhihu 上重叠，
  让 MediaCrawler 后注册胜出（成为这些平台的默认数据源），而 aigc 独有的
  gzh/sph/toutiao/github 仍保留可用（仅本地、有该文件时）。
"""

import importlib
import logging

logger = logging.getLogger(__name__)

# 顺序敏感：靠后的覆盖靠前的同名平台
_BACKENDS = [
    "platforms.aigc_channel",   # 内部后端（gitignore，公开环境通常缺失）
    "platforms.youtube",
    "platforms.x",
    "platforms.mediacrawler",   # 开源后端：覆盖重叠平台，作默认
]

_loaded: list[str] = []


def load_backends() -> list[str]:
    for mod in _BACKENDS:
        try:
            importlib.import_module(mod)
            _loaded.append(mod)
        except Exception as e:
            logger.info("平台后端 %s 未加载（跳过）: %s", mod, e)
    return _loaded


load_backends()
