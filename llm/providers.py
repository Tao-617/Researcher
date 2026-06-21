"""轻量 LLM provider（researcher 自依赖版）

OpenRouter 与 Qwen(DashScope) 都说 OpenAI 兼容的 /chat/completions 协议，
所以用一个通用 caller 覆盖两者。返回契约对齐 llm_helper.call_llm_with_retry 期望：
    {"content": str, "usage": dict, "cost": float}
OpenRouter 在 payload 里带 usage.include=True 时会回真实账单 cost，省得本地维护价表。

需要的环境变量（项目根 .env），按 backend 取用：
    GEMINI_API_KEY           —— Google AI Studio（OpenAI 兼容端点；当前唯一可用，故为默认）
    OPEN_ROUTER_API_KEY      —— OpenRouter
    QWEN_API_KEY / QWEN_BASE_URL —— 阿里云 DashScope（OpenAI 兼容端点）
"""

import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# 友好名 -> (backend, model_id)。
# 注：项目 .env 里 OpenRouter / Qwen 的 key 当前已失效（401），只有 GEMINI_API_KEY 可用，
# 故默认走 Google AI Studio 的 OpenAI 兼容端点。key 修好后切回 openrouter 即可。
MODELS: Dict[str, Tuple[str, str]] = {
    "gemini-flash-lite": ("gemini",     "gemini-flash-lite-latest"),  # 最快，大批量评估默认
    "gemini-flash":      ("gemini",     "gemini-2.5-flash"),
    "or-flash-lite":     ("openrouter", "google/gemini-3.1-flash-lite"),
    "sonnet":            ("openrouter", "claude-sonnet-4-6"),
    "qwen":              ("qwen",       "qwen3.5-plus"),
}
DEFAULT_MODEL = "gemini-flash-lite"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_TIMEOUT = 180.0


def _resolve(choice: str) -> Tuple[str, str]:
    """choice 是 MODELS 的 key 或直接的 model_id。返回 (backend, model_id)。"""
    if choice in MODELS:
        return MODELS[choice]
    low = choice.lower()
    if "qwen" in low:
        backend = "qwen"
    elif "gemini" in low:
        backend = "gemini"
    else:
        backend = "openrouter"
    return backend, choice


def _backend_endpoint(backend: str) -> Tuple[str, str]:
    """返回 (chat_completions_url, api_key)。缺 key 时抛错，避免静默空响应。"""
    if backend == "gemini":
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            raise ValueError("Gemini 需要 GEMINI_API_KEY")
        return "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", key
    if backend == "qwen":
        base = os.getenv("QWEN_BASE_URL", "").rstrip("/")
        key = os.getenv("QWEN_API_KEY", "")
        if not base or not key:
            raise ValueError("Qwen 需要 QWEN_BASE_URL 和 QWEN_API_KEY")
        return f"{base}/chat/completions", key
    key = os.getenv("OPEN_ROUTER_API_KEY", "")
    if not key:
        raise ValueError("OpenRouter 需要 OPEN_ROUTER_API_KEY")
    return "https://openrouter.ai/api/v1/chat/completions", key


async def _chat_completions(
    endpoint: str, api_key: str,
    messages: List[Dict[str, Any]], model: str,
    temperature: float, max_tokens: int,
    backend: str = "openrouter",
    **kwargs,
) -> Dict[str, Any]:
    """OpenAI 兼容 chat/completions 调用，带 429/5xx 重试。返回 {content, usage, cost}。"""
    import asyncio

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if backend == "openrouter":
        payload["usage"] = {"include": True}  # OpenRouter 专属：回真实账单 cost；其它后端会拒绝未知字段
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                result = resp.json()
            break
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in _RETRYABLE_STATUS and attempt < 2:
                await asyncio.sleep(2 ** attempt * 2)
                last_exc = e
                continue
            raise RuntimeError(f"LLM HTTP {status}: {e.response.text[:300]}") from e
        except (httpx.TransportError, httpx.TimeoutException) as e:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt * 2)
                last_exc = e
                continue
            raise
    else:
        raise last_exc  # type: ignore[misc]

    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(f"LLM 200 但 body 含 error: {result['error']}")

    choice = (result.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content", "") or ""
    usage = result.get("usage", {}) or {}
    cost = usage.get("cost")
    return {"content": content, "usage": usage, "cost": cost if isinstance(cost, (int, float)) else 0.0}


def build_llm_call(choice: str = DEFAULT_MODEL) -> Tuple[Callable, str]:
    """返回 (llm_call, model_id)。llm_call 签名兼容 call_llm_with_retry。"""
    backend, model_id = _resolve(choice)
    endpoint, api_key = _backend_endpoint(backend)

    async def llm_call(
        messages: List[Dict[str, Any]],
        model: str = model_id,
        temperature: float = 0.1,
        max_tokens: int = 4000,
        **kwargs,
    ) -> Dict[str, Any]:
        return await _chat_completions(endpoint, api_key, messages, model,
                                       temperature, max_tokens, backend=backend, **kwargs)

    return llm_call, model_id


if __name__ == "__main__":
    # 冒烟：PYTHONIOENCODING=utf-8 python -m llm.providers
    import asyncio
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    async def _t():
        call, mid = build_llm_call()
        print(f"model={mid}")
        r = await call(messages=[{"role": "user", "content": "只回一个字：好"}], max_tokens=10)
        print("content:", r["content"], "| cost:", r["cost"])

    asyncio.run(_t())
