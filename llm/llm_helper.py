"""
通用 LLM 调用 + JSON 校验 + 重试 helper

所有 Phase 2 workflow 脚本复用此模块，避免重复代码。

新增：支持 schema_name 参数，自动将 schema 传给 LLM 作为结构化输出约束。
"""

import json
import re
from typing import Any, Callable, Dict, Optional, Tuple


async def call_llm_with_retry(
    llm_call,
    messages: list,
    model: str,
    temperature: float = 0.1,
    max_tokens: int = 4000,
    max_retries: int = 3,
    validate_fn: Optional[Callable[[dict], Optional[str]]] = None,
    task_name: str = "",
    schema_name: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    调用 LLM 并自动校验 JSON 输出，失败时重试。

    Args:
        llm_call: LLM 调用函数
        messages: 初始消息列表
        model: 模型名称
        temperature: 温度
        max_tokens: 最大 token 数
        max_retries: 最大重试次数
        validate_fn: 可选的 schema 校验函数，接收 dict 返回 error string 或 None
        task_name: 任务名称（用于日志）
        schema_name: 可选的 schema 名称，如果提供则自动加载 schema 并传给 LLM

    Returns:
        (parsed_data, total_cost) — parsed_data 为 None 表示全部重试失败
    """
    total_cost = 0.0
    last_error = None

    # 如果提供了 schema_name，加载 schema 并自动设置 validate_fn
    response_schema = None
    if schema_name:
        try:
            from examples.process_pipeline.script.schema_manager import get_schema_manager, validate_with_schema
            manager = get_schema_manager()
            response_schema = manager.get_stripped_schema(schema_name)
            if response_schema and not validate_fn:
                # 自动设置校验函数
                validate_fn = lambda data: validate_with_schema(data, schema_name)
        except Exception as e:
            if task_name:
                print(f"   [{task_name}] Warning: Failed to load schema '{schema_name}': {e}")

    for attempt in range(max_retries):
        current_messages = list(messages)

        # 如果是重试，把上次的错误信息附加到消息中
        if attempt > 0 and last_error:
            if "JSON 解析失败" in last_error:
                fix_hint = (
                    f"你上次的输出存在 JSON 格式错误：{last_error}\n\n"
                    f"常见原因：字符串值中包含了未转义的英文双引号。\n"
                    f"修复方法：所有字符串值中的英文双引号必须转义为 \\\"，或改用中文引号「」。\n\n"
                    f"请重新输出完整且格式正确的 JSON，不要包含任何其他内容。"
                )
            else:
                fix_hint = (
                    f"你上次的输出未通过校验。错误：{last_error}\n\n"
                    f"请修正后重新输出完整的 JSON，不要包含其他内容。"
                )
            current_messages.append({"role": "user", "content": fix_hint})
            if task_name:
                print(f"   [{task_name}] Retry {attempt}/{max_retries-1}: {last_error[:80]}...")

        try:
            # 构建 LLM 调用参数
            call_kwargs = {
                "messages": current_messages,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            # 如果有 schema，传给 LLM（支持 response_schema 参数的 LLM 会使用）
            if response_schema:
                call_kwargs["response_schema"] = response_schema

            response = await llm_call(**call_kwargs)

            # 计算成本：优先用 provider 自带的准确 cost（qwen / openrouter 都按各自单价算过），
            # 没有才回退到粗略估算（Claude 单价 $3/$15 per M tokens）——避免按 Claude 单价高估 qwen。
            provider_cost = response.get("cost")
            if isinstance(provider_cost, (int, float)) and provider_cost > 0:
                total_cost += provider_cost
            else:
                usage = response.get("usage", {})
                if hasattr(usage, "__dict__"):
                    input_tokens = getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0)
                    output_tokens = getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0)
                else:
                    input_tokens = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                total_cost += (input_tokens / 1e6 * 3.0) + (output_tokens / 1e6 * 15.0)

            # 提取内容
            content = response.get("content", "")
            if isinstance(content, list):
                first = content[0] if content else ""
                content = first.get("text", "") if isinstance(first, dict) else str(first)
            elif not isinstance(content, str):
                content = str(content)

            # 尝试解析 JSON
            json_match = re.search(r"\{[\s\S]*\}", content)
            if not json_match:
                last_error = "LLM 输出中未找到有效的 JSON 对象"
                continue

            raw_json = json_match.group()
            try:
                parsed_data = json.loads(raw_json)
            except json.JSONDecodeError as e:
                # 尝试自动修复 JSON 语法错误
                try:
                    from examples.process_pipeline.script.fix_json_quotes import try_fix_and_parse
                    success, parsed_data, fix_desc = try_fix_and_parse(raw_json)
                    if not success:
                        last_error = f"JSON 解析失败且自动修复无效: {e}"
                        print(f"   [DEBUG] fix failed, raw_json:\n{raw_json}", flush=True)
                        continue
                    if task_name:
                        print(f"   [{task_name}] Auto-fixed JSON: {fix_desc}", flush=True)
                except ImportError:
                    last_error = f"JSON 解析失败: {e}"
                    continue

            # Schema 校验
            if validate_fn:
                schema_err = validate_fn(parsed_data)
                if schema_err:
                    last_error = f"Schema 校验失败: {schema_err}"
                    # 完整 dump LLM 输出（不截断）便于定位失败位置
                    if task_name:
                        print(f"   [{task_name}] === SCHEMA FAIL on attempt {attempt + 1} ===", flush=True)
                        print(f"   [{task_name}] error: {schema_err}", flush=True)
                        print(f"   [{task_name}] full LLM output ({len(content)} chars):", flush=True)
                        print(content, flush=True)
                        print(f"   [{task_name}] === end LLM output ===", flush=True)
                    continue

            # 全部通过
            return parsed_data, total_cost

        except Exception as e:
            last_error = f"LLM 调用异常: {type(e).__name__}: {e}"
            if task_name:
                print(f"   [{task_name}] Error: {last_error}")

    # 全部重试失败
    if task_name:
        print(f"   [{task_name}] All {max_retries} attempts failed. Last error: {last_error}")
    return None, total_cost
