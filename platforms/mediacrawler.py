"""MediaCrawler 平台后端（开源替代 aigc-channel）。

把外部开源项目 NanmiCoder/MediaCrawler 当作一个**子进程数据源**接进来：
  search() → 以 `--save_data_option json` 跑一次 MediaCrawler 关键词搜索
           → 读它产出的 data/<platform>/json/search_contents_*.json
           → 把各平台字段**映射**成 researcher 管线认的统一 post dict
           → 包成 ToolResult(metadata={"posts": [...]})，与 aigc_channel.search 完全同构。

为什么是子进程而不是 import：MediaCrawler 是 CLI/批处理工具（Playwright 浏览器自动化 +
落盘），不是可被同步调用、直接返回结果的库。子进程 + 读 JSON 是最稳的接法。

前置条件（运行环境需自备，不随本仓库发布）：
  1. 在别处 clone 并安装 MediaCrawler（`uv sync`），路径通过环境变量 MEDIACRAWLER_HOME 指定，
     缺省取本仓库同级的 ../MediaCrawler。
  2. 首次每个平台需登录一次（默认扫码 qrcode，浏览器可见）；登录态由 MediaCrawler 持久化
     （SAVE_LOGIN_STATE + <platform>_user_data_dir），之后复用。

可调环境变量：
  MEDIACRAWLER_HOME       MediaCrawler 仓库根目录（缺省 ../MediaCrawler）
  MEDIACRAWLER_GET_COMMENT 是否同时抓一级评论（"1"/"0"，缺省 "0"，开了更慢）
  MEDIACRAWLER_HEADLESS    无头模式（"1"/"0"，缺省 "0"；首次登录必须可见）
  MEDIACRAWLER_LOGIN_TYPE  登录方式（qrcode/phone/cookie，缺省 qrcode）
  MEDIACRAWLER_TIMEOUT     单次搜索子进程超时秒数（缺省 300）
"""

import asyncio
import glob
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from vendor.tool_result import ToolResult
from platforms.registry import PlatformDef, ParamSpec, register_platform

logger = logging.getLogger(__name__)

# 本仓库根（platforms/ 的上一级）
_REPO_ROOT = Path(__file__).resolve().parent.parent

# researcher 平台 id → MediaCrawler 平台代码
_PLATFORM_CODE = {
    "xhs": "xhs", "douyin": "dy", "kuaishou": "ks",
    "bili": "bili", "weibo": "wb", "zhihu": "zhihu", "tieba": "tieba",
}

# MediaCrawler 各平台 search_contents JSON 的字段名（来自其 store/<p> 与 model/）。
# 概念 → 实际 key；缺失填 None。研究依据见对应 store 源码。
_FIELD_MAP: Dict[str, Dict[str, Optional[str]]] = {
    "xhs":   dict(id="note_id",   title="title", body="desc",  nick="nickname",      uid="user_id", like="liked_count", comment="comment_count", share="share_count",       collect="collected_count",     time="time",         url="note_url",    images="image_list",      video="video_url"),
    "dy":    dict(id="aweme_id",  title="title", body="desc",  nick="nickname",      uid="user_id", like="liked_count", comment="comment_count", share="share_count",       collect="collected_count",     time="create_time",  url="aweme_url",   images=None,              video="video_download_url", cover="cover_url"),
    "ks":    dict(id="video_id",  title="title", body="desc",  nick="nickname",      uid="user_id", like="liked_count", comment=None,            share=None,                collect=None,                  time="create_time",  url="video_url",   images=None,              video="video_play_url",     cover="video_cover_url"),
    "bili":  dict(id="video_id",  title="title", body="desc",  nick="nickname",      uid="user_id", like="liked_count", comment="video_comment", share="video_share_count", collect="video_favorite_count", time="create_time", url="video_url",   images=None,              video=None,                 cover="video_cover_url"),
    "wb":    dict(id="note_id",   title=None,    body="content",nick="nickname",      uid="user_id", like="liked_count", comment="comments_count",share="shared_count",      collect=None,                  time="create_time",  url="note_url",    images=None,              video=None),
    "zhihu": dict(id="content_id",title="title", body="content_text", nick="user_nickname", uid="user_id", like="voteup_count", comment="comment_count", share=None,           collect=None,                  time="created_time", url="content_url", images=None,              video=None),
    "tieba": dict(id="note_id",   title="title", body="desc",  nick="user_nickname", uid="user_id", like=None,          comment="total_replay_num", share=None,             collect=None,                  time="publish_time", url="note_url",    images=None,              video=None),
}

# 评论里指向母帖的 id 字段名（按平台），content 文本统一兜底取 content/text。
_COMMENT_PARENT_KEY = {
    "xhs": "note_id", "dy": "aweme_id", "ks": "video_id", "bili": "video_id",
    "wb": "note_id", "zhihu": "content_id", "tieba": "note_id",
}

# 同一时刻只允许一个 MediaCrawler 子进程：它用持久化 user_data_dir，
# 同平台并发会抢浏览器目录锁直接崩。串行化是必须的（浏览器自动化本就重，可接受）。
_MC_LOCK = asyncio.Lock()


def _bool_env(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "t")


def _resolve_home() -> Path:
    """定位 MediaCrawler：环境变量 > 子模块 external/MediaCrawler > 同级 ../MediaCrawler。"""
    home = os.getenv("MEDIACRAWLER_HOME")
    if home:
        return Path(home).expanduser().resolve()
    submodule = _REPO_ROOT / "external" / "MediaCrawler"
    if (submodule / "main.py").exists():
        return submodule.resolve()
    return (_REPO_ROOT.parent / "MediaCrawler").resolve()


def _to_int(v: Any) -> Optional[int]:
    """解析计数。兼容中文计数单位：'1.8万' / '2亿' / '3.2w' / '5k'，以及纯数字/带逗号。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "赞", "评论"):
        return None
    mult = 1
    if s and s[-1] in "万w":
        mult, s = 10_000, s[:-1]
    elif s and s[-1] in "亿":
        mult, s = 100_000_000, s[:-1]
    elif s and s[-1] in "kK":
        mult, s = 1_000, s[:-1]
    try:
        return int(float(s) * mult)
    except (ValueError, TypeError):
        return None


def _split_images(raw: Any) -> List[str]:
    """xhs 的 image_list 可能是列表或逗号分隔串；其它平台只有封面。统一成 url 列表。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
        return [p for p in parts if p.startswith("http")]
    return []


def _map_post(code: str, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一条 MediaCrawler 记录映射成 researcher 管线认的 post dict。"""
    fm = _FIELD_MAP[code]
    cid = rec.get(fm["id"]) if fm["id"] else None
    if not cid:
        return None

    images: List[str] = []
    if fm.get("images"):
        images = _split_images(rec.get(fm["images"]))
    if not images and fm.get("cover") and rec.get(fm["cover"]):
        images = [str(rec[fm["cover"]])]

    videos: List[str] = []
    if fm.get("video") and rec.get(fm["video"]):
        videos = [str(rec[fm["video"]])]

    body = rec.get(fm["body"]) if fm["body"] else ""
    title = (rec.get(fm["title"]) if fm["title"] else "") or (body or "")[:30]

    return {
        # researcher 管线/前端消费的统一字段（见 pipeline/schema.py、search.py、config.yaml）
        "channel_content_id": str(cid),
        "channel": code,
        "title": title,
        "body_text": body or "",
        "link": rec.get(fm["url"]) if fm["url"] else "",
        "like_count": _to_int(rec.get(fm["like"])) if fm["like"] else None,
        "comment_count": _to_int(rec.get(fm["comment"])) if fm["comment"] else None,
        "share_count": _to_int(rec.get(fm["share"])) if fm["share"] else None,
        "collect_count": _to_int(rec.get(fm["collect"])) if fm["collect"] else None,
        "user_id": rec.get(fm["uid"]) if fm["uid"] else None,
        "nickname": rec.get(fm["nick"]) if fm["nick"] else None,
        "publish_time": rec.get(fm["time"]) if fm["time"] else None,
        "images": images,
        "videos": videos,
        "content_type": "video" if videos else "图文",
        "author_comments": [],          # 评论稍后按 id 回填
        "_raw": rec,                    # 保留原始记录，便于排查
    }


def _attach_comments(code: str, posts: List[Dict[str, Any]], out_dir: Path) -> None:
    """读 search_comments_*.json，按母帖 id 归并到 post['author_comments']。"""
    files = glob.glob(str(out_dir / code / "json" / "*comments*.json"))
    if not files:
        return
    parent_key = _COMMENT_PARENT_KEY.get(code)
    by_id: Dict[str, List[Dict[str, Any]]] = {}
    for fp in files:
        try:
            data = json.loads(Path(fp).read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in data if isinstance(data, list) else [data]:
            if not isinstance(c, dict):
                continue
            pid = str(c.get(parent_key) or "")
            text = c.get("content") or c.get("comment_text") or c.get("text") or ""
            if pid and text:
                by_id.setdefault(pid, []).append({"content": text})
    for p in posts:
        cid = p.get("channel_content_id")
        if cid in by_id:
            p["author_comments"] = by_id[cid]


def _read_contents(code: str, out_dir: Path) -> List[Dict[str, Any]]:
    files = glob.glob(str(out_dir / code / "json" / "*contents*.json"))
    records: List[Dict[str, Any]] = []
    for fp in sorted(files):
        try:
            data = json.loads(Path(fp).read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("读 MediaCrawler 输出失败 %s: %s", fp, e)
            continue
        records.extend(data if isinstance(data, list) else [data])
    return records


def _run_blocking(cmd: List[str], home: Path, timeout: int) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.run(
        cmd, cwd=str(home), env=env, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )


async def search(
    platform_id: str,
    keyword: str,
    max_count: int = 20,
    cursor: str = "",
    extras: Optional[Dict[str, Any]] = None,
) -> ToolResult:
    """跑一次 MediaCrawler 关键词搜索，返回与 aigc_channel.search 同构的 ToolResult。"""
    code = _PLATFORM_CODE.get(platform_id)
    if not code:
        return ToolResult(title="搜索失败", output="", error=f"MediaCrawler 不支持平台 {platform_id}")

    home = _resolve_home()
    if not (home / "main.py").exists():
        return ToolResult(
            title="搜索失败", output="",
            error=f"未找到 MediaCrawler（{home}）。请 clone 并 `uv sync`，或设环境变量 MEDIACRAWLER_HOME。",
        )

    uv = shutil.which("uv")
    if not uv:
        return ToolResult(title="搜索失败", output="", error="未找到 uv，可执行 `pip install uv` 或装 uv。")

    get_comment = _bool_env("MEDIACRAWLER_GET_COMMENT", False)
    headless = _bool_env("MEDIACRAWLER_HEADLESS", False)
    login_type = os.getenv("MEDIACRAWLER_LOGIN_TYPE", "qrcode")
    timeout = int(os.getenv("MEDIACRAWLER_TIMEOUT", "300"))

    out_dir = Path(tempfile.mkdtemp(prefix=f"mc_{code}_"))
    cmd = [
        uv, "run", "main.py",
        "--platform", code,
        "--lt", login_type,
        "--type", "search",
        "--keywords", keyword,
        "--save_data_option", "json",
        "--save_data_path", str(out_dir),
        "--crawler_max_notes_count", str(max_count),
        "--get_comment", "true" if get_comment else "false",
        "--get_sub_comment", "false",
        "--headless", "true" if headless else "false",
    ]

    try:
        async with _MC_LOCK:  # 串行化：避免持久化 user_data_dir 抢锁
            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(None, _run_blocking, cmd, home, timeout)

        if proc.returncode != 0:
            tail = (proc.stdout or "")[-800:]
            return ToolResult(title="搜索失败", output="",
                              error=f"MediaCrawler 退出码 {proc.returncode}: {tail}")

        records = _read_contents(code, out_dir)
        posts: List[Dict[str, Any]] = []
        for rec in records:
            p = _map_post(code, rec)
            if p:
                posts.append(p)
        if get_comment:
            _attach_comments(code, posts, out_dir)

        return ToolResult(
            title=f"搜索: {keyword} ({platform_id})",
            output=json.dumps({"data_count": len(posts)}, ensure_ascii=False),
            long_term_memory=f"MediaCrawler searched '{keyword}' on {platform_id}, {len(posts)} results.",
            metadata={"posts": posts},
        )
    except subprocess.TimeoutExpired:
        return ToolResult(title="搜索失败", output="",
                          error=f"MediaCrawler 超时（{timeout}s）。首次登录请先手动跑一次完成扫码。")
    except Exception as e:
        return ToolResult(title="搜索失败", output="", error=f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ── 平台注册 ──

_MC_PLATFORMS = [
    PlatformDef(id="xhs",      name="小红书", aliases=["RED", "xiaohongshu"]),
    PlatformDef(id="douyin",   name="抖音",   aliases=["dy", "TikTok"]),
    PlatformDef(id="kuaishou", name="快手",   aliases=["ks"]),
    PlatformDef(id="bili",     name="B站",    aliases=["bilibili", "哔哩哔哩"]),
    PlatformDef(id="weibo",    name="微博",   aliases=["wb", "sina"]),
    PlatformDef(id="zhihu",    name="知乎",   aliases=[]),
    PlatformDef(id="tieba",    name="贴吧",   aliases=["百度贴吧"]),
]


def _register_all():
    for p in _MC_PLATFORMS:
        p.search_impl = search
        register_platform(p)


_register_all()
