"""Download a post's source video, extract audio, transcribe via Deepgram.

Used by platform detail() implementations whose posts ship raw video URLs
(X, sph, douyin) and don't already supply captions. YouTube has its own
captions endpoint and bypasses this module.

Pipeline per video:
  1. extract_video_url(platform, post)  -> source url (page or direct)
  2. download to %TEMP%/content_transcribe/<platform>/<stem>.mp4
     - X     : yt-dlp on the page URL (most robust against rotating video URLs)
     - douyin: httpx + Referer https://www.douyin.com/
     - sph   : httpx + Referer https://channels.weixin.qq.com/
  3. ffmpeg -> 16kHz mono AAC 64kbps m4a (~3% the size of the source mp4)
  4. POST to Deepgram /v1/listen, model=whisper-large by default
  5. Strip spaces inserted by Deepgram between consecutive CJK characters

Returns transcript text on success, None on any failure (silent fallback).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL_DEFAULT = "whisper-large"
DEEPGRAM_REQUEST_TIMEOUT = 600.0
DOWNLOAD_TIMEOUT = 300
FFMPEG_TIMEOUT = 600
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 仓库根 / .cache / content_videos —— 不用系统 %TEMP%，避免被 Windows 偶发清理。
# parents[1]: transcription.py → pipeline/ → 仓库根
_CACHE_ROOT = Path(__file__).resolve().parents[1] / ".cache" / "content_videos"
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Zero-width lookbehind/lookahead: remove whitespace strictly between CJK chars,
# preserve CJK<->ASCII boundaries (e.g. "Remotion 是工具" stays intact).
_CJK_SPACE_RE = re.compile(r"(?<=[一-鿿])\s+(?=[一-鿿])")

# Referer headers required by some CDNs for ffprobe / yt-dlp / httpx to access video URLs.
_PLATFORM_REFERERS = {
    "douyin": "https://www.douyin.com/",
    "sph": "https://channels.weixin.qq.com/",
    "xhs": "https://www.xiaohongshu.com/",
    "bili": "https://www.bilibili.com/",
    "weibo": "https://weibo.com/",
}
_DURATION_PROBE_TIMEOUT = 15


def extract_video_url(platform: str, post: dict[str, Any]) -> Optional[str]:
    """Pluck a video URL (page or direct) out of a platform's raw post dict."""
    if platform == "x":
        vlist = post.get("video_url_list") or []
        if vlist:
            head = vlist[0]
            return head.get("video_url") if isinstance(head, dict) else head
        return None
    if platform == "youtube":
        vid = post.get("video_id") or post.get("content_id")
        return f"https://www.youtube.com/watch?v={vid}" if vid else None
    # Generic: aigc-channel platforms (xhs / gzh / sph / douyin / bili / zhihu /
    # weibo / toutiao / github) all expose video URLs under `videos[0]`.
    videos = post.get("videos") or []
    if videos:
        return videos[0]
    # MediaCrawler 搜索结果里 bili 等无视频直链，但 link 是视频页，交给 yt-dlp 处理。
    if post.get("content_type") == "video" and post.get("link"):
        return post["link"]
    return None


def _safe_stem(platform: str, post: dict[str, Any]) -> str:
    raw_id = (
        post.get("channel_content_id")
        or post.get("video_id")
        or post.get("content_id")
        or "item"
    )
    return f"{platform}_{_SAFE_RE.sub('_', str(raw_id))[:60]}"


def _yt_dlp_download(url: str, target: Path) -> Optional[Path]:
    if target.exists() and target.stat().st_size > 0:
        return target
    # Format chain: 优先 muxed mp4（YouTube/X/douyin 通常命中，最快），
    # fallback 到 bestvideo+bestaudio + ffmpeg merge（bili 等 DASH-only 平台），
    # 最后兜底 best。
    cmd = ["yt-dlp", "-f", "best[ext=mp4]/bestvideo+bestaudio/best",
           "-o", str(target),
           "--no-playlist", "--quiet", "--no-warnings", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("yt-dlp failed for %s: %s", url, e)
        return None
    if r.returncode != 0:
        logger.warning("yt-dlp non-zero for %s: %s", url, (r.stderr or r.stdout)[:200])
        return None
    if target.exists() and target.stat().st_size > 0:
        return target
    # yt-dlp may have written with a different extension
    for f in target.parent.glob(target.stem + ".*"):
        if f.is_file() and f.stat().st_size > 0:
            return f
    return None


async def _httpx_download(url: str, target: Path, referer: Optional[str] = None) -> Optional[Path]:
    if target.exists() and target.stat().st_size > 0:
        return target
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    try:
        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT, follow_redirects=True, headers=headers
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.warning("download HTTP %s for %s", resp.status_code, url)
                    return None
                with target.open("wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        f.write(chunk)
    except Exception as e:
        logger.warning("httpx download failed for %s: %s", url, e)
        return None
    return target if target.exists() and target.stat().st_size > 0 else None


async def _download_video(
    platform: str, post: dict[str, Any], video_url: str, target: Path
) -> Optional[Path]:
    """Dispatch to the right downloader per platform.

    Per-platform strategies:
      x      : yt-dlp on the tweet page URL (video URLs are signed/rotating)
      douyin : httpx direct with douyin.com Referer (video URL is a play API)
      sph    : httpx direct with channels.weixin.qq.com Referer (stodownload link)
      youtube: yt-dlp on the watch URL

    For everything else (xhs / bili / weibo / zhihu / gzh / toutiao / github / ...):
    try yt-dlp on the post's page URL first (yt-dlp supports 1000+ sites including
    most aigc-channel platforms via cookies-free extractors), and fall back to
    plain httpx on `videos[0]` if yt-dlp can't handle it.
    """
    if platform == "x":
        page_url = post.get("link") or video_url
        return await asyncio.to_thread(_yt_dlp_download, page_url, target)
    if platform == "douyin":
        return await _httpx_download(video_url, target, referer="https://www.douyin.com/")
    if platform == "sph":
        return await _httpx_download(video_url, target, referer="https://channels.weixin.qq.com/")
    if platform == "youtube":
        return await asyncio.to_thread(_yt_dlp_download, video_url, target)

    # Generic two-step fallback for any other platform with a `videos` field.
    page_url = post.get("link")
    if page_url:
        result = await asyncio.to_thread(_yt_dlp_download, page_url, target)
        if result:
            return result
        logger.info("yt-dlp didn't handle %s page URL; falling back to httpx", platform)
    return await _httpx_download(video_url, target)


def _extract_m4a(video_path: Path, audio_path: Path) -> bool:
    """ffmpeg: video -> 16kHz mono AAC 64kbps m4a. Returns True if file written."""
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    if audio_path.exists() and audio_path.stat().st_size > 0:
        return True
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video_path),
           "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "aac", "-b:a", "64k",
           str(audio_path)]
    try:
        subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT,
                       capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("ffmpeg failed for %s: %s", video_path, e)
        return False
    return audio_path.exists() and audio_path.stat().st_size > 0


async def _transcribe_deepgram(
    audio_path: Path,
    api_key: str,
    model: str = DEEPGRAM_MODEL_DEFAULT,
    language: Optional[str] = None,
) -> Optional[str]:
    params: dict[str, str] = {
        "model": model,
        "smart_format": "true",
        "punctuate": "true",
    }
    if language:
        params["language"] = language
    else:
        params["detect_language"] = "true"
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/mp4",
    }
    try:
        audio_bytes = audio_path.read_bytes()
        async with httpx.AsyncClient(timeout=DEEPGRAM_REQUEST_TIMEOUT) as client:
            r = await client.post(DEEPGRAM_URL, params=params, headers=headers,
                                  content=audio_bytes)
    except Exception as e:
        logger.warning("Deepgram request failed for %s: %s", audio_path.name, e)
        return None
    if r.status_code != 200:
        logger.warning("Deepgram HTTP %s: %s", r.status_code, r.text[:200])
        return None
    try:
        data = r.json()
        alt = data["results"]["channels"][0]["alternatives"][0]
        return alt.get("transcript") or None
    except (KeyError, IndexError, ValueError) as e:
        logger.warning("Deepgram response malformed: %s", e)
        return None


def _clean_chinese_spaces(text: str) -> str:
    """Drop whitespace strictly between two CJK characters."""
    return _CJK_SPACE_RE.sub("", text)


def _ffprobe_duration_sync(video_url: str, referer: Optional[str] = None) -> Optional[float]:
    """Read mp4 moov box over HTTP Range; returns duration (seconds) or None.

    Does NOT download the video stream — typically pulls only a few KB even for
    multi-GB files. Designed to be called from search() to enrich posts with
    duration before scoring, without paying the cost of a full download.
    """
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1"]
    if referer:
        cmd += ["-headers", f"Referer: {referer}\r\n"]
    cmd += [video_url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_DURATION_PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.info("ffprobe duration probe failed for %s: %s", video_url[:80], e)
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    try:
        d = float(out)
    except ValueError:
        return None
    return d if d > 0 else None


async def probe_video_duration(
    video_url: str, platform: Optional[str] = None
) -> Optional[float]:
    """Async wrapper. Probes mp4 duration via HTTP Range; returns seconds or None.

    Pass `platform` to auto-inject the right Referer header (douyin / sph / xhs / bili
    require it). Safe to call concurrently — uses asyncio.to_thread so subprocesses
    don't block the event loop. Each call is one ffprobe subprocess; cap parallelism
    at the call site if probing many URLs.
    """
    if not video_url:
        return None
    referer = _PLATFORM_REFERERS.get(platform) if platform else None
    return await asyncio.to_thread(_ffprobe_duration_sync, video_url, referer)


async def probe_durations_for_posts(
    platform: str, posts: list, concurrency: int = 8
) -> None:
    """In-place: probe each post's video URL and set post["duration_sec"] if found.

    Skips posts with no video URL (image-only posts). Probes happen concurrently
    bounded by `concurrency` to avoid spawning a flood of ffprobe subprocesses.
    Failures are silent (post just won't have duration_sec — evaluator handles).
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(post: dict) -> None:
        url = extract_video_url(platform, post)
        if not url:
            return
        async with sem:
            d = await probe_video_duration(url, platform=platform)
        if d is not None:
            post["duration_sec"] = d

    await asyncio.gather(*[_one(p) for p in posts if isinstance(p, dict)])


def _get_api_key() -> Optional[str]:
    key = os.environ.get("DEEPGRAM_KEY") or os.environ.get("DEEPGRAM_API_KEY")
    if key:
        return key
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        return None
    return os.environ.get("DEEPGRAM_KEY") or os.environ.get("DEEPGRAM_API_KEY")


async def transcribe_video_from_post(
    platform: str,
    post: dict[str, Any],
    *,
    model: str = DEEPGRAM_MODEL_DEFAULT,
    language: Optional[str] = None,
) -> Optional[str]:
    """End-to-end: locate video, download, extract m4a, STT, clean spaces.

    Returns transcript text or None if any step fails (logged at WARNING level).
    Caller can safely ignore None and fall back to whatever body text it has.
    """
    url = extract_video_url(platform, post)
    if not url:
        return None
    api_key = _get_api_key()
    if not api_key:
        logger.warning("DEEPGRAM_KEY not set; skipping transcription for %s", platform)
        return None

    stem = _safe_stem(platform, post)
    work_dir = _CACHE_ROOT / platform
    work_dir.mkdir(parents=True, exist_ok=True)
    video_path = work_dir / f"{stem}.mp4"
    audio_path = work_dir / f"{stem}.m4a"

    video = await _download_video(platform, post, url, video_path)
    if not video:
        return None

    if not await asyncio.to_thread(_extract_m4a, video, audio_path):
        return None

    transcript = await _transcribe_deepgram(audio_path, api_key, model=model, language=language)
    if not transcript:
        return None
    return _clean_chinese_spaces(transcript).strip()


async def enrich_with_transcripts(
    sources: list,
    concurrency: int = 2,
    max_videos: int = 15,
) -> int:
    """对 sources 里的视频帖转写字幕并**原地**插入 post.body_text，返回成功条数。

    放在去重后、评分前调用：只转活下来的候选（最小集合），避免对几百条原帖全转。
    每条：下载视频 → ffmpeg 抽音 → Deepgram → 字幕拼进 body_text（评分/多模态都能用到）。
    失败静默跳过（保留原 body_text）。max_videos 上限控成本/时间。
    """
    def _is_video(post: dict) -> bool:
        return bool(post.get("videos")) or post.get("content_type") == "video"

    targets = [
        s for s in sources
        if _is_video(s.get("post") or {}) and not (s.get("post") or {}).get("video_transcript")
    ]
    if max_videos:
        targets = targets[:max_videos]
    if not targets:
        return 0

    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def _one(s: dict) -> None:
        nonlocal done
        post = s.get("post") or {}
        platform = s.get("platform", "")
        async with sem:
            try:
                txt = await transcribe_video_from_post(platform, post)
            except Exception as e:
                logger.warning("transcribe failed %s: %s", s.get("case_id"), e)
                txt = None
        if txt:
            post["video_transcript"] = txt
            body = (post.get("body_text") or "").strip()
            post["body_text"] = (body + "\n【视频字幕】" + txt).strip()
            done += 1

    await asyncio.gather(*[_one(s) for s in targets])
    return done
