"""按可用性加载平台后端 —— 缺失的后端不阻塞导入。

默认只加载 **MediaCrawler**（开源、公开可用的 7 个平台）。
内部后端（aigc_channel / youtube / x，依赖公司内部接口 aigc-channel.aiddit.com /
crawler.aiddit.com，公开环境不可用、且已 .gitignore）只有在显式开启时才加载：
    环境变量 RESEARCHER_ENABLE_INTERNAL=1
这样默认 UI/CLI 只暴露真正能跑的平台，避免误选到内部后端而报错（502 / No module 'agent'）。

加载顺序即注册优先级（register_platform 按 id 覆盖）：内部后端先、MediaCrawler 后，
让重叠平台（xhs/douyin/bili/weibo/zhihu）由 MediaCrawler 覆盖为默认数据源。
"""

import importlib
import logging
import os

logger = logging.getLogger(__name__)


def _truthy(v):
    return bool(v) and v.strip().lower() in ("1", "true", "yes", "y", "t")


# 内部后端：默认不加载，RESEARCHER_ENABLE_INTERNAL=1 时才加载（顺序敏感，先于 MediaCrawler）
_INTERNAL = ["platforms.aigc_channel", "platforms.youtube", "platforms.x"]
# 开源后端：始终加载，覆盖重叠平台作默认
_PUBLIC = ["platforms.mediacrawler"]

_loaded: list = []


def load_backends() -> list:
    enable_internal = _truthy(os.getenv("RESEARCHER_ENABLE_INTERNAL"))
    backends = (_INTERNAL if enable_internal else []) + _PUBLIC
    for mod in backends:
        try:
            importlib.import_module(mod)
            _loaded.append(mod)
        except Exception as e:
            logger.info("平台后端 %s 未加载（跳过）: %s", mod, e)
    return _loaded


load_backends()
