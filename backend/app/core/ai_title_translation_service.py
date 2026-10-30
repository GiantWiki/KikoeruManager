"""AI 标题汉化服务。复用 LiteLLM 基础设施，将日文作品标题翻译为中文。"""
from __future__ import annotations


import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


import logging


from .log_sanitizer import mask_url_for_log, sanitize_text_for_log


logger = logging.getLogger(__name__)


MASKED_SECRET = "********"
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_proxy_lock = asyncio.Lock()

# 「思考」类字段：模型把推理过程写在这里，**不是翻译答案**。
# 它们可以进入诊断日志（便于排障），但绝不能当作标题/JSON 答案送去解析——
# 否则会拿大段思考文本去做 JSON 校验，报出与事实不符的「模型返回的 JSON 无法解析」。
_REASONING_FIELD_KEYS = ("reasoning_content", "reasoning", "thinking")

# 「答案」类字段的优先顺序：正文（content / text）优先。未知自定义字段（如
# answer_text）同样可作答案，只要它不是思考类字段——保持「不硬编码字段名」。
_ANSWER_FIELD_KEYS = ("content", "text")

# 作品标题的长度上界（字符）。超过即判「不是标题」——用于拒绝把整段思考写库。
# 实测最长的正常标题（含副标题 / 特典后缀）远小于此值。
_MAX_TITLE_CHARS = 300

# 思考文本里常见的分析性措辞：多行文本命中这些词即视为思考过程（拒绝写库）。
_REASONING_TEXT_MARKERS = (
    "我们需要回答用户",
    "用户要求",
    "需要翻译成",
    "逐条",
    "综上",
    "分析：",
    "可能输出",
    "需要决定",
    "保留原样",
    "指的是",
    "可以译为",
    "注意原文",
)

# 推理模型把答案也写在思考里时，答案行常见的引导别名（取第一个命中）。
_ANSWER_LINE_MARKERS = (
    "翻译结果：",
    "翻译结果:",
    "译文：",
    "译文:",
    "翻译：",
    "翻译:",
    "答案：",
    "答案:",
    "结果：",
    "结果:",
)




def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default




def _safe_text(value: Any) -> str:
    return str(value or "").strip()


_REFUSAL_PATTERNS = (
    "抱歉，我无法", "抱歉，我不能", "对不起，我无法", "对不起，我不能",
    "很抱歉，我无法", "很抱歉，我不能", "我无法协助", "我不能协助",
    "无法协助翻译", "无法为您提供", "不能为您提供",
    "i'm sorry, but i can't", "i am sorry, but i cannot",
    "i cannot assist", "i can't assist", "i cannot help with",
    "i can't help with", "i'm unable to assist", "sorry, i cannot",
)


def _is_refusal_text(text: str) -> bool:
    """检测模型拒答文案（如"抱歉，我无法协助翻译这段内容"）。

    拒答必须当失败处理：否则拒答文案会被当作翻译结果写入 work_metadata.ai_title
    并被用作文件夹新名字，污染 DB 和磁盘，后续 API 重命名还会因目标名冲突
    抛 WinError 5。限制长度上限，避免正常标题里碰巧含"我无法"被误杀。
    """
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    if len(normalized) > 120:
        return False
    return any(pattern in normalized for pattern in _REFUSAL_PATTERNS)


def _config_to_dict(config: Any) -> Dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, dict):
        return dict(config)
    if hasattr(config, "model_dump"):
        return dict(config.model_dump())
    return {
        key: getattr(config, key)
        for key in dir(config)
        if not key.startswith("_") and not callable(getattr(config, key, None))
    }




def _extract_litellm_content(
    response: Any,
    *,
    include_reasoning: bool = True,
) -> Tuple[str, Dict[str, int]]:
    usage_obj = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if usage_obj is None:
        usage = {}
    elif isinstance(usage_obj, dict):
        usage = usage_obj
    else:
        usage = {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0),
            "completion_tokens": getattr(usage_obj, "completion_tokens", 0),
            "total_tokens": getattr(usage_obj, "total_tokens", 0),
        }
    choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
    if not choices:
        return "", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    choice = choices[0]
    if isinstance(choice, dict):
        message = choice.get("message", {})
        content = message.get("content", "")
        if include_reasoning and not content:
            content = message.get("reasoning_content", "") or message.get("reasoning", "")
    else:
        message = getattr(choice, "message", None)
        content = getattr(message, "content", "") if message is not None else ""
        if include_reasoning and not content and message is not None:
            content = getattr(message, "reasoning_content", "") or getattr(message, "reasoning", "")
    if isinstance(content, list):
        content = "".join(
            str((part or {}).get("text") or (part or {}).get("content") or "")
            if isinstance(part, dict)
            else str(part or "")
            for part in content
        )
    return str(content or "").strip(), {
        "prompt_tokens": _safe_int(usage.get("prompt_tokens")),
        "completion_tokens": _safe_int(usage.get("completion_tokens")),
        "total_tokens": _safe_int(usage.get("total_tokens")),
    }




def _extract_finish_reason(response: Any) -> str:
    """提取 finish_reason，用于识别输出被 max_tokens 截断（length）的情况。"""
    choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
    if not choices:
        return ""
    choice = choices[0]
    if isinstance(choice, dict):
        return str(choice.get("finish_reason") or "").strip().lower()
    return str(getattr(choice, "finish_reason", "") or "").strip().lower()




def _litellm_chunk_finish_reason(chunk: Any) -> str:
    """安全读取流式分片的 finish_reason（不同 provider 字段形态不一）。"""
    try:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return ""
        return str(getattr(choices[0], "finish_reason", "") or "")
    except Exception:
        return ""


def _merge_stream_usage(current: Dict[str, int], incoming: Dict[str, Any]) -> Dict[str, int]:
    """按字段合并 usage，避免后到的分片把已有 token 计数整体覆盖掉。

    服务端可能只在部分分片返回 usage、且各分片字段不全；整块覆盖会让日志里
    ``usage={}`` 或全 0，正好丢掉「模型确实输出了 12,304 token」这类关键证据。
    """
    merged = dict(current or {})
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        try:
            value = int((incoming or {}).get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            merged[key] = value
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        merged.setdefault(key, 0)
    return merged


# 重试必然同样失败的「确定性」错误特征：认证、配置、响应结构本身有问题。
# ⚠️ 严禁把「截断 / max_tokens」相关词加入这里——截断可通过调大上限或重试改善，
# 且既有测试 test_truncation_retries_then_succeeds 锁定了「截断要重试」的语义。
_NON_RETRYABLE_MARKERS = (
    "missing_config",
    "authentication",
    "unauthorized",
    "invalid_api_key",
    "invalid api key",
    "api key is invalid",
    "insufficient",
    "quota",
    "model_not_found",
    "model not found",
    "无法解析或不完整",
    "不完整的 json",
    "没有可用翻译结果",
    "模型拒答",
)


def _is_non_retryable_error(exc: Exception) -> bool:
    """该错误是否属于「重试必然同样失败」的确定性错误。

    减少确定性错误的白等（实测一次失败要空转 16 秒）。网络抖动 / 超时 /
    限流仍然重试。
    """
    raw = str(exc or "").lower()
    if not raw:
        return False
    return any(marker in raw for marker in _NON_RETRYABLE_MARKERS)


def _collect_non_stream_text_fields(response: Any) -> Dict[str, str]:
    """非流式响应里 choice.message 的全部文本字段（通用收集，同样不硬编码）。

    与流式的 ``extract_litellm_stream_delta_verbose`` 口径一致：标题翻译、
    字幕配对、连接测试三处共用同一实现，日志可互相印证。
    """
    from .ai_subtitle_match_service import _collect_text_fields

    choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
    if not choices:
        return {}
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else getattr(choice, "message", None)
    return _collect_text_fields(message)


def _log_non_stream_response_fields(
    request_label: str, response: Any, collected: Dict[str, str]
) -> None:
    """非流式路径的响应字段诊断日志（与流式路径同格式）。"""
    from .ai_subtitle_match_service import log_collected_text_fields

    finish_reason = _extract_finish_reason(response)
    logger.info(
        "[AI标题] %s 非流式响应汇总: finish_reason=%s 字段长度=%s",
        request_label,
        finish_reason or "(无)",
        {key: len(value) for key, value in (collected or {}).items()} or "{}",
    )
    log_collected_text_fields("[AI标题]", collected, label=request_label)
    if not any(collected.get(key) for key in ("content", "text")) and collected:
        logger.warning(
            "[AI标题] %s 正文(content)为空，但模型实际返回了其他文本字段: %s",
            request_label,
            {key: len(value) for key, value in collected.items()},
        )


def _pick_fallback_text(collected: Dict[str, str]) -> Tuple[str, str]:
    """正文为空时，从已收集字段里挑一个可用的文本作为翻译结果。

    返回 ``(字段名, 文本)``；无可用字段返回 ``("", "")``。

    挑选顺序：先 ``reasoning_content``（推理模型把正文写在思考字段时的典型位置），
    再其余任意非空字段。**不做模型名判断**——任何 provider 任何字段名都适用。
    """
    if not collected:
        return "", ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        text = (collected.get(key) or "").strip()
        if text:
            return key, text
    # 其余字段按插入顺序取第一个非空（排除纯结构字段由收集阶段已过滤）。
    for key, value in collected.items():
        text = (value or "").strip()
        if text:
            return key, text
    return "", ""


def _scan_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    """在任意文本里**扫描**出第一个可解析的 JSON 对象（成功返回 dict，否则 None）。

    与「首 ``{`` 到末 ``}`` 切片再 ``json.loads``」的区别：切片会跨越文本里
    多个不相邻的 ``{}`` 片段拼成一个非法串（典型报错 ``Extra data``）；本函数用
    ``JSONDecoder.raw_decode`` 从每个 ``{`` 位置独立尝试，命中即返回，不跨界拼接。

    只接受 dict（翻译映射的形态）。
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)
    while index < length:
        start = text.find("{", index)
        if start < 0:
            return None
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            return obj
        index = end if end > start else start + 1
    return None


def _looks_like_reasoning_text(text: str) -> bool:
    """判断一段文本是否像「思考过程」而不是「翻译答案」。

    用于兜底拒绝：思考文本动辄数千字、含大量分析性措辞与多行结构，
    绝不能作为作品标题写库 / 改名。

    注意与 v2.6.45 的兼容：**短的单行文本一律不算思考**——推理模型常把最终答案
    直接写在 reasoning_content 末尾（如「圣职者姐妹」16 字），那种必须照常接受。
    """
    stripped = str(text or "").strip()
    if not stripped:
        return False
    # 单行且不超长 → 不是思考过程（v2.6.45 的典型形态）。
    if "\n" not in stripped and len(stripped) <= _MAX_TITLE_CHARS:
        return False
    if len(stripped) > _MAX_TITLE_CHARS:
        return True
    # 多行且命中分析性措辞 → 视为思考。
    if any(marker in stripped for marker in _REASONING_TEXT_MARKERS):
        return True
    # 多行且行数较多 → 视为思考（标题不会是多行段落）。
    return len([line for line in stripped.splitlines() if line.strip()]) > 3


def _extract_answer_from_reasoning(text: str) -> str:
    """从思考文本里「抠出」翻译答案行；抠不到返回空串。

    推理模型把正文也写在 ``reasoning_content`` 时，答案通常出现在
    ``翻译：``/``翻译结果：``/``答案：`` 之类别名之后（用户实测形态）。这里按行
    扫描取**第一条**该类标记后的内容；没有标记则返回空串（**不猜**）。

    返回结果仍受长度闸门约束，超长视为失败（由调用方处理）。
    """
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        for marker in _ANSWER_LINE_MARKERS:
            # 只接受答案行行首的标记。模型复述提示词时，行内也会出现
            # “value 为翻译结果：……”，用 find() 会把后面的提示词片段当标题。
            if not stripped.startswith(marker):
                continue
            candidate = stripped[len(marker):].strip()
            candidate = candidate.strip('"').strip("'").strip("「").strip("」").strip()
            if candidate and len(candidate) <= _MAX_TITLE_CHARS:
                return candidate
            # 标记后为空 → 答案在下一行
            for follow in lines[index + 1:]:
                follow_stripped = follow.strip().strip('"').strip("'").strip("「").strip("」").strip()
                if not follow_stripped:
                    continue
                if len(follow_stripped) <= _MAX_TITLE_CHARS:
                    return follow_stripped
                break
    return ""


def _extract_translation_text(
    content: str,
    *,
    reasoning_fallback: bool = False,
    request_label: str = "",
) -> str:
    """把模型输出清成「翻译文本」，并处理 JSON 映射形态。

    参数：
    - ``reasoning_fallback``：``content`` 是否来自**思考字段回退**。思考文本里的
      ``{}`` 只是推理片段，不是答案；而整段思考更是**绝不能当标题写库**（用户实测：
      v2.6.50 曾把 9638 字的思考过程直接当成标题改名，污染了文件夹名）。

    规则（未回退时与历史行为保持一致，避免影响普通模型）：
    1. 正文本身是 JSON 映射 → 单值取值、多值重新 dump（原逻辑）。
    2. 有 ``{`` 无 ``}`` 且未回退 → 判截断残骸（原逻辑，防垃圾名写库）。
    3. 无 ``{`` → 纯文本清洗。

    回退（思考文本）路径：
    a. 先按 ``翻译：`` 之类别名抠出答案行，避免把前面的草稿 JSON 当答案；
    b. 没有答案标记时才扫描 JSON 对象；
    c. **仍取不到、或取到的东西像思考过程（多行 / 超长）→ 明确失败**，
       绝不把整段思考当标题返回。
    """
    text = str(content or "").strip()
    if not text:
        return text

    if reasoning_fallback:
        # 显式答案标记优先于草稿 JSON。
        answer = _extract_answer_from_reasoning(text)
        if answer:
            return _reject_if_reasoning_like(answer, request_label=request_label)
        parsed = _scan_first_json_object(text)
        if isinstance(parsed, dict):
            values = [v.strip() for v in parsed.values() if isinstance(v, str) and v.strip()]
            if len(values) == 1:
                return _reject_if_reasoning_like(values[0], request_label=request_label)
            if len(values) > 1:
                return json.dumps(parsed, ensure_ascii=False)
        # c. 无标记：**短的单行文本**可直接当答案（v2.6.45 兼容——推理模型常把
        #    最终答案直接写在推理字段里，如「圣职者姐妹」），只需过一遍长度/形态闸门。
        if not _looks_like_reasoning_text(text):
            return text.strip().strip('"').strip("'").strip("「").strip("」").strip()
        # d. 长/多行的思考文本且抠不到答案：绝不返回整段思考。
        preview = sanitize_text_for_log(text[:200])
        if request_label:
            logger.error(
                "[AI标题] %s 思考文本回退无法提取翻译答案（长度=%s），拒绝写库 | 预览=%s",
                request_label,
                len(text),
                preview,
            )
        raise ValueError(
            "模型把正文写在了思考字段(reasoning_content)，且其中没有可识别的翻译结果；"
            "为避免把整段思考当成标题，本次判失败。建议改用非推理模型，或在提示词中明确"
            "要求「只输出 JSON、不要输出分析过程」"
        )

    json_start = text.find("{")
    json_end = text.rfind("}") + 1
    has_brace_span = json_start >= 0 and json_end > json_start

    if has_brace_span:
        try:
            parsed = json.loads(text[json_start:json_end])
        except json.JSONDecodeError as exc:
            # 严格路径解析失败：再给一次「扫描取首个对象」的机会（应对正文里
            # 意外夹带多个 {} 的情况），仍失败才报错，并把诊断信息带上。
            scanned = _scan_first_json_object(text)
            if isinstance(scanned, dict):
                values = [v.strip() for v in scanned.values() if isinstance(v, str) and v.strip()]
                if len(values) == 1:
                    return values[0]
                if len(values) > 1:
                    return json.dumps(scanned, ensure_ascii=False)
            preview = sanitize_text_for_log(text[:200])
            if request_label:
                logger.error(
                    "[AI标题] %s JSON 解析失败（解析对象=正文，非思考回退）: %s | 预览=%s",
                    request_label,
                    exc,
                    preview,
                )
            # JSON 残骸不能当翻译结果传下去，否则会被直接用作文件夹 / 文件新名字
            raise ValueError(
                f"模型返回的 JSON 无法解析或不完整: {exc}（解析对象为模型正文，"
                f"预览：{preview}）"
            ) from exc
        if isinstance(parsed, dict):
            values = [v.strip() for v in parsed.values() if isinstance(v, str) and v.strip()]
            if len(values) == 1:
                return values[0]
            if len(values) > 1:
                return json.dumps(parsed, ensure_ascii=False)
            raise ValueError("模型返回的 JSON 没有可用翻译结果")
    elif json_start >= 0:
        # 有 '{' 没有 '}'：典型的输出截断残骸，绝不能当标题使用
        raise ValueError("模型返回了不完整的 JSON（缺少结束括号），输出可能被截断")
    cleaned = text.strip().strip('"').strip("'").strip("「").strip("」").strip()
    return _reject_if_reasoning_like(cleaned, request_label=request_label)


def _reject_if_reasoning_like(candidate: str, *, request_label: str = "") -> str:
    """候选标题若「像思考过程」则判失败，避免垃圾名写库 / 改名。"""
    if _looks_like_reasoning_text(candidate):
        preview = sanitize_text_for_log(str(candidate)[:200])
        if request_label:
            logger.error(
                "[AI标题] %s 候选标题像思考过程（长度=%s），拒绝写库 | 预览=%s",
                request_label,
                len(str(candidate)),
                preview,
            )
        raise ValueError(
            f"模型返回的内容像思考过程而非翻译结果（长度={len(str(candidate))}），"
            f"为避免污染标题已判失败。预览：{preview}"
        )
    return candidate


def _describe_empty_response(
    usage: Dict[str, int], finish_reason: str, collected: Dict[str, str]
) -> str:
    """正文为空时给出可诊断的失败原因（而不是笼统的「模型返回为空」）。

    三种形态必须区分开，否则日志无法定位是「模型没输出」还是「字段没读到」：

    1. 有其他文本字段 → 模型**有输出**，只是没落在 content 上（推理模型典型）；
       此时**回退使用该字段**，等同成功，不再报错。
    2. 无任何文本字段但 completion_tokens > 0 或 finish_reason 非空 →
       服务端确实生成过内容，但本端未解析到任何字段（响应结构/字段名问题）。
    3. 全 0 且 finish_reason 为空 → 真的没输出。
    """
    lengths = {key: len(value) for key, value in (collected or {}).items()}
    completion_tokens = int((usage or {}).get("completion_tokens") or 0)
    total_tokens = int((usage or {}).get("total_tokens") or 0)
    if collected:
        # 形态 1：有输出，交给调用方回退使用（不该走到这里，留作文案兜底）。
        return (
            f"模型未返回标准正文(content)，但返回了其他文本字段 {lengths}，"
            f"（usage={usage}, finish_reason={finish_reason or '无'}）"
        )
    if completion_tokens > 0 or total_tokens > 0 or finish_reason:
        return (
            f"模型有输出（completion_tokens={completion_tokens}, "
            f"total_tokens={total_tokens}, finish_reason={finish_reason or '无'}）"
            f"但未解析到任何文本字段，可能是响应结构不受支持"
        )
    return (
        f"模型返回为空（未收到任何内容，usage={usage}, "
        f"finish_reason={finish_reason or '无'}）"
    )


async def _consume_litellm_stream(
    stream: Any, *, request_label: str = ""
) -> Tuple[str, Dict[str, int], str, Dict[str, str]]:
    """消费 litellm 流式响应，返回 ``(文本, usage, finish_reason, 字段原文汇总)``。

    流式下 ``timeout`` 是「两次数据块之间的停滞超时」而不是总时长上限：慢速模型
    / 长响应不会再因为整体耗时超过 timeout 被误杀（这正是标题翻译超时失败的根因）。

    第 4 个返回值是**诊断用**的字段汇总 ``{字段名: 完整原文}``：推理模型会把输出
    全花在 ``reasoning_content`` 上导致 ``content`` 为空，只有拿到每个字段的真实
    内容才能区分「模型有输出但字段不对」与「模型真的没输出」。这里用通用收集
    （``extract_litellm_stream_delta_verbose``）而不是硬编码字段名，不为任何具体
    模型写特判。

    ⚠️ **正文只由「答案类」字段（content / text / 未知自定义字段）拼成**：
    ``reasoning_content`` 等思考字段只进诊断汇总，**绝不进入返回值文本**。
    历史缺陷（用户实测「推理模型翻译必失败，报 JSON 无法解析 / Extra data」）：
    思考字段曾参与主文本选择，导致 1 万字的思考被当答案，再被 JSON 首末切片校验
    报出与事实不符的解析错误。
    """
    from .ai_subtitle_match_service import (
        extract_litellm_stream_delta_verbose,
        log_collected_text_fields,
        summarize_collected_text_fields,
    )

    parts: List[str] = []
    usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    finish_reason = ""
    chunk_count = 0
    saw_any_fields = False
    # 每个字段的原文按到达顺序累积；不同 chunk 的同名字段拼接。
    accumulated: Dict[str, str] = {}
    async for chunk in stream:
        chunk_count += 1
        delta, collected, chunk_usage, chunk_finish = extract_litellm_stream_delta_verbose(chunk)
        if collected:
            saw_any_fields = True
            for key, text in collected.items():
                accumulated[key] = accumulated.get(key, "") + text
        # ★ 只接受「答案类」字段进入正文；思考类字段（reasoning_content 等）只进
        # 诊断汇总。`delta` 是 verbose 变体选出的「主文本」，它可能来自思考字段，
        # 因此这里**不用 delta**，改为从 collected 自行判定。
        answer_piece = _pick_answer_piece(collected)
        if answer_piece:
            parts.append(answer_piece)
        usage = _merge_stream_usage(usage, chunk_usage)
        if chunk_finish:
            finish_reason = chunk_finish

    content = "".join(parts)
    if request_label:
        lengths = summarize_collected_text_fields(accumulated)
        logger.info(
            "[AI标题] %s 流式响应汇总: chunks=%s finish_reason=%s usage=%s 字段长度=%s",
            request_label,
            chunk_count,
            finish_reason or "(无)",
            usage,
            lengths or "{}",
        )
        log_collected_text_fields("[AI标题]", accumulated, label=request_label)
        if not content and accumulated:
            # 决定性诊断：模型有输出、只是没落在正文（content）字段上。典型是
            # 推理模型把输出全花在 reasoning_content——注意这**不是翻译答案**，
            # 后续会以「回退」方式处理并明确标记。
            logger.warning(
                "[AI标题] %s 正文(content)为空，但模型实际返回了其他文本字段: %s",
                request_label,
                lengths,
            )
        elif not content and not accumulated and saw_any_fields:
            logger.warning(
                "[AI标题] %s 收到 %s 个分片但所有文本字段均为空（模型确实未输出内容）",
                request_label,
                chunk_count,
            )
    return content, usage, finish_reason, accumulated


def _pick_answer_piece(collected: Dict[str, str]) -> str:
    """从单个分片的已收集字段里挑出**属于答案**的那部分文本（拼接用）。

    规则：
    - 思考类字段（``reasoning_content`` / ``reasoning`` / ``thinking``）**一律跳过**；
    - 优先 ``content`` / ``text``；
    - 其余未知自定义字段（如 ``answer_text``）按插入顺序取第一个，保持通用性。

    思考字段被排除是本函数存在的唯一理由：它承载的是模型的推理过程，
    不是翻译结果。
    """
    if not collected:
        return ""
    for key in _ANSWER_FIELD_KEYS:
        text = collected.get(key)
        if text:
            return text
    for key, text in collected.items():
        if key in _REASONING_FIELD_KEYS:
            continue
        if text:
            return text
    return ""


def _is_azure_config(config: Dict[str, Any]) -> bool:
    base_url = _safe_text(config.get("api_base")).lower()
    api_version = _safe_text(config.get("api_version"))
    return bool(api_version and ("azure" in base_url or ".openai.azure.com" in base_url))




def _normalize_error(exc: Exception) -> Dict[str, str]:
    raw = str(exc or "")
    lowered = raw.lower()
    exc_name = exc.__class__.__name__.lower()
    code = "unknown"
    title = "未知错误"
    suggestion = "请检查日志获取详细信息"
    if "model_not_found" in lowered or "model" in lowered and "not found" in lowered:
        code, title, suggestion = "model_not_found", "模型不存在", "检查模型名是否有效"
    elif "proxy" in lowered:
        code, title, suggestion = "proxy_error", "代理不可用", "检查代理地址格式"
    elif "authentication" in lowered or "unauthorized" in lowered or "invalid_api_key" in exc_name:
        code, title, suggestion = "auth_error", "认证失败", "检查 API Key 是否有效"
    elif "rate" in lowered and "limit" in lowered:
        code, title, suggestion = "rate_limit", "请求频率限制", "请稍后重试"
    elif "timeout" in lowered:
        code, title, suggestion = "timeout", "请求超时", "检查网络连接或增加超时时间"
    return {"code": code, "title": title, "suggestion": suggestion}




@contextlib.asynccontextmanager
async def _temporary_proxy(proxy_url: str):
    proxy = _safe_text(proxy_url)
    if not proxy:
        yield
        return
    saved = {}
    async with _proxy_lock:
        for key in _PROXY_ENV_KEYS:
            saved[key] = os.environ.get(key, "")
            os.environ[key] = proxy
        try:
            yield
        finally:
            for key in _PROXY_ENV_KEYS:
                if saved.get(key):
                    os.environ[key] = saved[key]
                else:
                    os.environ.pop(key, None)




class AITitleTranslationService:
    """AI 标题翻译服务。将日文作品标题翻译为中文。"""

    def __init__(self):
        # 防止任务中心重复点击导致同一份快照被并发回退。
        self._rollback_locks: Dict[str, asyncio.Lock] = {}

    @staticmethod
    def _validate_rollback_snapshot(metadata: Dict[str, Any]) -> None:
        snapshot_keys = ("ai_translation", "renamed_files", "folder_renamed")
        if not all(isinstance(metadata.get(key), list) for key in snapshot_keys):
            raise ValueError("缺少完整回退快照，无法安全回退")

        for entry in metadata.get("ai_translation") or []:
            if not isinstance(entry, dict):
                raise ValueError("缺少完整回退快照，无法安全回退")
            legacy_title_write = "write_status" not in entry and bool(_safe_text(entry.get("translated_title")))
            if entry.get("write_status") == "success" or legacy_title_write:
                if (
                    not _safe_text(entry.get("rjcode"))
                    or "original_ai_title" not in entry
                    or not _safe_text(entry.get("written_ai_title"))
                ):
                    raise ValueError("缺少完整回退快照，无法安全回退")

        for entry in metadata.get("renamed_files") or []:
            if not isinstance(entry, dict):
                raise ValueError("缺少完整回退快照，无法安全回退")
            if entry.get("status") == "ok" and not all(
                _safe_text(entry.get(key)) for key in ("library_id", "old_name", "old_path", "new_path")
            ):
                raise ValueError("缺少完整回退快照，无法安全回退")

        for entry in metadata.get("folder_renamed") or []:
            if not isinstance(entry, dict):
                raise ValueError("缺少完整回退快照，无法安全回退")
            if entry.get("success") and not all(
                _safe_text(entry.get(key)) for key in ("library_id", "old_name", "old_path", "new_path")
            ):
                raise ValueError("缺少完整回退快照，无法安全回退")

    @staticmethod
    async def _restore_rename_snapshot(entry: Dict[str, Any], library_manager: Any) -> str:
        if entry.get("rollback_status") == "restored":
            return "already"

        current_path = _safe_text(entry.get("new_path"))
        original_path = _safe_text(entry.get("old_path"))
        original_name = _safe_text(entry.get("old_name"))
        library_id = _safe_text(entry.get("library_id"))
        current_key = os.path.normcase(os.path.abspath(current_path))
        original_key = os.path.normcase(os.path.abspath(original_path))
        if current_key == original_key:
            entry["rollback_status"] = "restored"
            entry["rollback_error"] = ""
            return "already"

        current_exists = os.path.exists(current_path)
        original_exists = os.path.exists(original_path)
        if not current_exists and original_exists:
            # 兼容上次重命名已成功、但任务快照尚未来得及落盘的崩溃窗口。
            entry["rollback_status"] = "restored"
            entry["rollback_error"] = ""
            return "already"
        if not current_exists:
            entry["rollback_status"] = "skipped"
            entry["rollback_error"] = "任务写入后的路径已不存在，可能已被后续移动或删除"
            return "skipped"
        if original_exists:
            entry["rollback_status"] = "skipped"
            entry["rollback_error"] = "原始路径已存在，为避免覆盖后续内容而跳过"
            return "skipped"

        try:
            rename_result = await library_manager.rename(
                library_id,
                current_path,
                original_name,
                skip_index_mutation=False,
                sync_index_mutation=False,
            )
            entry["rollback_status"] = "restored"
            entry["rollback_error"] = ""
            entry["rollback_path"] = _safe_text((rename_result or {}).get("new_path")) or original_path
            return "restored"
        except Exception as exc:
            entry["rollback_status"] = "failed"
            entry["rollback_error"] = str(exc)
            return "failed"

    @staticmethod
    def _rollback_title_entry(entry: Dict[str, Any], db: Any, sql_text: Any) -> str:
        if entry.get("rollback_status") == "restored":
            return "already"
        try:
            row = db.execute(
                sql_text("SELECT ai_title FROM work_metadata WHERE rjcode = :rjcode"),
                {"rjcode": entry["rjcode"]},
            ).fetchone()
            if not row:
                entry["rollback_status"] = "skipped"
                entry["rollback_error"] = "作品元数据已不存在"
                return "skipped"
            if row[0] != entry.get("written_ai_title"):
                entry["rollback_status"] = "skipped"
                entry["rollback_error"] = "当前 AI 标题已被后续修改，为避免覆盖而跳过"
                return "skipped"
            db.execute(
                sql_text("UPDATE work_metadata SET ai_title = :ai_title WHERE rjcode = :rjcode"),
                {"ai_title": entry.get("original_ai_title"), "rjcode": entry["rjcode"]},
            )
            db.commit()
            entry["rollback_status"] = "restored"
            entry["rollback_error"] = ""
            return "restored"
        except Exception as exc:
            db.rollback()
            entry["rollback_status"] = "failed"
            entry["rollback_error"] = str(exc)
            return "failed"

    async def rollback_task_changes(
        self,
        task: Any,
        *,
        library_manager: Any = None,
        session_factory: Any = None,
    ) -> Dict[str, Any]:
        """按任务快照安全回退 AI 标题、内部文件和项目目录的修改。"""
        lock_key = _safe_text(getattr(task, "id", "")) or str(id(task))
        lock = self._rollback_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._rollback_task_changes_locked(
                task,
                library_manager=library_manager,
                session_factory=session_factory,
            )

    async def _rollback_task_changes_locked(
        self,
        task: Any,
        *,
        library_manager: Any = None,
        session_factory: Any = None,
    ) -> Dict[str, Any]:
        metadata = getattr(task, "task_metadata", None)
        if not isinstance(metadata, dict):
            raise ValueError("缺少完整回退快照，无法安全回退")
        self._validate_rollback_snapshot(metadata)

        if library_manager is None:
            from .library_manager import get_library_manager

            library_manager = get_library_manager()

        outcomes: List[str] = []
        # 文件快照产生于目录重命名之前；必须先还目录，文件的新路径才重新成立。
        for entry in metadata.get("folder_renamed") or []:
            if entry.get("success"):
                outcomes.append(await self._restore_rename_snapshot(entry, library_manager))
        for entry in metadata.get("renamed_files") or []:
            if entry.get("status") == "ok":
                outcomes.append(await self._restore_rename_snapshot(entry, library_manager))

        title_entries = [
            entry for entry in (metadata.get("ai_translation") or [])
            if entry.get("write_status") == "success"
        ]
        if title_entries:
            if session_factory is None:
                from ..models.database import SessionLocal

                session_factory = SessionLocal
            from sqlalchemy import text as sql_text

            db = session_factory()
            try:
                for entry in title_entries:
                    outcomes.append(self._rollback_title_entry(entry, db, sql_text))
            finally:
                db.close()

        restored_count = outcomes.count("restored")
        skipped_count = outcomes.count("skipped")
        failed_count = outcomes.count("failed")
        already_restored = not outcomes or all(outcome == "already" for outcome in outcomes)
        if failed_count or skipped_count:
            message = (
                f"回退完成：恢复 {restored_count} 项，跳过 {skipped_count} 项，"
                f"失败 {failed_count} 项；未覆盖冲突或后续修改"
            )
        elif already_restored:
            message = "没有需要回退的修改"
        else:
            message = f"已安全回退 {restored_count} 项 AI 汉化修改"

        result = {
            "success": failed_count == 0,
            "message": message,
            "restored_count": restored_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "already_restored": already_restored,
            "partial": bool(skipped_count or failed_count),
            "completed_at": datetime.now().isoformat(),
        }
        metadata["ai_title_rollback"] = dict(result)
        touch_metadata = getattr(task, "touch_metadata", None)
        if callable(touch_metadata):
            touch_metadata("ai_title_rollback")
        return result

    def _normalize_runtime_config(self, raw_config: Any, *, saved_api_key: str = "") -> Dict[str, Any]:
        config = _config_to_dict(raw_config)
        api_key = _safe_text(config.get("api_key"))
        if api_key == MASKED_SECRET:
            config["api_key"] = saved_api_key or ""
        return config


    def _build_messages(self, config: Dict[str, Any], work_name: str) -> List[Dict[str, str]]:
        logger.info("[AI标题] 构建翻译 prompt: work_name=%s", work_name)
        prompt_template = config.get("prompt_template") or "请将以下日文作品标题翻译成中文，只输出翻译结果：\n{work_name}"
        prompt = prompt_template.replace("{work_name}", work_name)
        logger.info("[AI标题] prompt 替换后内容: %s", prompt[:200])
        return [
            {"role": "system", "content": "你是一个专业的日文标题翻译助手。请根据用户提供的标题进行翻译。"},
            {"role": "user", "content": prompt},
        ]


    # 发现不到运营商上限时的绝对兜底：任何 OpenAI 兼容服务端都不至于连 8192 都拒绝，
    # 避免用户手填一个超大值直接把请求打成 400。
    _FALLBACK_MAX_OUTPUT_TOKENS = 8192

    def _resolve_max_tokens(self, config: Dict[str, Any]) -> int:
        """决定本次请求实际使用的 ``max_tokens``（**不硬编码模型名**）。

        规则：
        1. 用户填的值作为「期望上限」，取不到时默认 4096（历史行为，防 JSON 截断）。
        2. 向运营商自报的额度看齐（``/models`` 的 ``max_output_tokens`` 缓存，由
           ``list_models`` 写入）：用户值**超过**运营商上限时**软夹取**到运营商上限，
           既不报错也不会被服务端截断。
        3. 运营商额度未知（中转不返回该字段 / 用户没点过「获取模型」）时，夹取到
           保守兜底 ``_FALLBACK_MAX_OUTPUT_TOKENS``，防止越界 400。
        4. 下限恒为 200（防截断）。
        """
        requested = max(200, _safe_int(config.get("max_tokens"), 4096))
        api_base = _safe_text(config.get("api_base"))
        model = _safe_text(config.get("model"))
        discovered = 0
        if api_base and model:
            try:
                from .ai_subtitle_match_service import lookup_model_output_limit

                discovered = lookup_model_output_limit(api_base, model)
            except Exception:
                discovered = 0
        ceiling = discovered if discovered > 0 else self._FALLBACK_MAX_OUTPUT_TOKENS
        if requested > ceiling:
            logger.info(
                "[AI标题] max_tokens 按运营商上限夹取: 用户值=%s -> 实际=%s（%s） model=%s",
                requested,
                ceiling,
                "运营商自报" if discovered > 0 else "保守兜底",
                model or "(未设置)",
            )
            return max(200, ceiling)
        return requested


    def _completion_kwargs(
        self,
        config: Dict[str, Any],
        messages: List[Dict[str, str]],
        *,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": _safe_text(config.get("model")),
            "messages": messages,
            "temperature": float(config.get("temperature", 0.1)),
            # 文件级重命名会返回「作品标题 + 全部文件名」的 JSON 映射，200 token 必被截断，
            # 截断的 JSON 残骸曾被当成翻译结果直接把项目文件夹重命名成垃圾名，这里默认放宽到 4096。
            "max_tokens": self._resolve_max_tokens(config),
        }
        api_key = _safe_text(config.get("api_key"))
        if api_key:
            kwargs["api_key"] = api_key
        api_base = _safe_text(config.get("api_base"))
        if api_base:
            kwargs["api_base"] = api_base.rstrip("/")
        if api_base and not _is_azure_config(config):
            kwargs["custom_llm_provider"] = "openai"
        api_version = _safe_text(config.get("api_version"))
        if api_version:
            kwargs["api_version"] = api_version
        organization = _safe_text(config.get("organization"))
        if organization:
            kwargs["organization"] = organization
        kwargs["timeout"] = float(timeout_seconds or _safe_int(config.get("timeout_seconds"), 30))
        return kwargs


    async def _call_model(self, config: Dict[str, Any], work_name: str) -> Tuple[str, Dict[str, int]]:
        """调用 LLM 翻译标题。返回 (翻译文本, usage)。"""
        if not config.get("model"):
            raise ValueError("missing_config: model 不能为空")
        if not config.get("api_key"):
            raise ValueError("missing_config: api_key 不能为空")


        try:
            import litellm
        except Exception as exc:
            raise RuntimeError(f"missing_config: 后端未安装 litellm: {exc}") from exc


        request_label = f"标题翻译[{uuid.uuid4().hex[:8]}]"
        messages = self._build_messages(config, work_name)
        kwargs = self._completion_kwargs(config, messages)
        max_retries = _safe_int(config.get("max_retries"), 2)
        # 流式优先（与连接测试 / AI 字幕配对一致）：流式下 timeout 只是「数据块之间的
        # 停滞超时」，翻译大响应（整棵文件树）不会再被固定总时长误杀；同时关闭 litellm
        # 自带重试，避免「单次 30s × litellm 3 次 ≈ 91s」把一次尝试放大三倍。
        stream_kwargs = dict(kwargs)
        stream_kwargs["stream"] = True
        stream_kwargs["num_retries"] = 0
        hard_timeout = max(120.0, float(kwargs.get("timeout") or 30.0) * 6)


        last_error: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                # 本轮响应里模型「实际返回了哪些字段」——用于空正文时给出可诊断原因。
                collected_fields: Dict[str, str] = {}
                async with _temporary_proxy(config.get("proxy_url", "")):
                    try:
                        stream = await litellm.acompletion(**stream_kwargs)
                        if hasattr(stream, "__aiter__"):
                            content, usage, finish_reason, collected_fields = await asyncio.wait_for(
                                _consume_litellm_stream(stream, request_label=request_label),
                                timeout=hard_timeout,
                            )
                        else:
                            # 服务端/代理忽略了 stream=True，直接返回完整响应：按非流式解析。
                            content, usage = _extract_litellm_content(stream, include_reasoning=False)
                            finish_reason = _extract_finish_reason(stream)
                            collected_fields = _collect_non_stream_text_fields(stream)
                            _log_non_stream_response_fields(request_label, stream, collected_fields)
                    except (asyncio.TimeoutError, TimeoutError):
                        raise
                    except Exception as stream_exc:
                        from .ai_subtitle_match_service import _is_stream_unsupported_error

                        if not _is_stream_unsupported_error(stream_exc):
                            raise
                        logger.warning(
                            "[AI标题] %s 流式不可用，回退非流式: %s", request_label, stream_exc
                        )
                        response = await asyncio.wait_for(
                            litellm.acompletion(**{**kwargs, "num_retries": 0}),
                            timeout=hard_timeout,
                        )
                        content, usage = _extract_litellm_content(response, include_reasoning=False)
                        finish_reason = _extract_finish_reason(response)
                        collected_fields = _collect_non_stream_text_fields(response)
                        _log_non_stream_response_fields(request_label, response, collected_fields)
                reasoning_fallback = False
                if not content and collected_fields:
                    # 模型有输出、但正文没落在 content 字段上（推理模型把思考写在
                    # reasoning_content 是典型形态）。在**不改动原字段语义**的前提下
                    # 回退使用已收集到的文本，避免「有输出却被判为空响应」。
                    # ⚠️ 不走「推理模型特判」：任何非空文本字段都可回退，通用有效。
                    fallback_key, fallback_text = _pick_fallback_text(collected_fields)
                    if fallback_text:
                        reasoning_fallback = fallback_key in _REASONING_FIELD_KEYS
                        logger.warning(
                            "[AI标题] %s 正文为空，回退使用字段[%s]（%s 字符）作为翻译结果%s",
                            request_label,
                            fallback_key,
                            len(fallback_text),
                            "；该字段是思考文本，将按宽松规则提取，不做严格 JSON 校验"
                            if reasoning_fallback
                            else "",
                        )
                        content = fallback_text
                        if not usage.get("completion_tokens") and not usage.get("total_tokens"):
                            usage["completion_tokens"] = len(fallback_text)
                if not content:
                    raise ValueError(_describe_empty_response(usage, finish_reason, collected_fields))
                if finish_reason == "length":
                    raise ValueError(
                        f"模型输出达到 max_tokens 上限（{kwargs.get('max_tokens')}）被截断，"
                        "请在 AI 标题汉化设置中调大输出 token 上限"
                    )
                # 清理输出：尝试提取 JSON，如果 AI 返回了 {key: value} 格式则解析
                content = _extract_translation_text(
                    content,
                    reasoning_fallback=reasoning_fallback,
                    request_label=request_label,
                )
                if _is_refusal_text(content):
                    # 模型拒答（如"抱歉，我无法协助翻译这段内容"）必须当失败处理，
                    # 否则拒答文案会被写进 ai_title 并用作文件夹新名字。
                    raise ValueError(f"模型拒答: {content[:80]}")
                logger.info(
                    "[AI标题] %s 翻译成功: work_name=%s -> %s tokens=%s",
                    request_label,
                    work_name,
                    content,
                    usage.get("total_tokens", 0),
                )
                return content, usage
            except Exception as exc:
                last_error = exc
                if _is_non_retryable_error(exc):
                    # 认证失败 / 配置缺失 / 响应结构错误：重试只会白等同样结果。
                    logger.error(
                        "[AI标题] %s 确定性错误，不再重试: %s", request_label, exc
                    )
                    break
                if attempt < max_retries:
                    wait = 1.0 * (attempt + 1)
                    logger.warning(
                        "[AI标题] %s 第%s次重试: %s", request_label, attempt + 1, exc
                    )
                    await asyncio.sleep(wait)
                continue


        raise last_error or RuntimeError("翻译失败")


    async def translate_single(
        self,
        work_name: str,
        config: Any,
        *,
        saved_api_key: str = "",
    ) -> Dict[str, Any]:
        """翻译单个作品标题。"""
        cfg = self._normalize_runtime_config(config, saved_api_key=saved_api_key)
        if not cfg.get("enabled", False):
            return {"success": False, "status": "disabled", "translated_title": None}


        started = time.perf_counter()
        try:
            translated, usage = await self._call_model(cfg, work_name)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return {
                "success": True,
                "translated_title": translated,
                "original_title": work_name,
                "duration_ms": duration_ms,
                "tokens": usage.get("total_tokens", 0),
                "model": cfg.get("model", ""),
            }
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            error_info = _normalize_error(exc)
            logger.warning("[AI标题] 翻译失败: work_name=%s error=%s", work_name, exc)
            return {
                "success": False,
                "status": "failed",
                "translated_title": None,
                "original_title": work_name,
                "error": error_info,
                "duration_ms": duration_ms,
            }


    async def translate_batch(
        self,
        items: List[Dict[str, str]],
        config: Any,
        *,
        saved_api_key: str = "",
    ) -> List[Dict[str, Any]]:
        """批量翻译多个作品标题。
        items: [{"rjcode": "RJ123456", "work_name": "..."}, ...]
        """
        cfg = self._normalize_runtime_config(config, saved_api_key=saved_api_key)
        if not cfg.get("enabled", False):
            return [{"success": False, "status": "disabled", "rjcode": item.get("rjcode"), "translated_title": None} for item in items]


        batch_size = _safe_int(cfg.get("batch_size"), 5)
        results = []
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            batch_tasks = []
            for item in batch:
                work_name = _safe_text(item.get("work_name", ""))
                if not work_name:
                    results.append({
                        "success": False,
                        "status": "skipped_empty",
                        "rjcode": item.get("rjcode"),
                        "translated_title": None,
                    })
                    continue
                batch_tasks.append(self.translate_single(work_name, config, saved_api_key=saved_api_key))


            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            for j, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    results.append({
                        "success": False,
                        "status": "error",
                        "rjcode": batch[j].get("rjcode"),
                        "translated_title": None,
                        "error": str(result),
                    })
                else:
                    result["rjcode"] = batch[j].get("rjcode")
                    results.append(result)


        return results


    async def test_connection(self, raw_config: Any, *, saved_api_key: str = "") -> Dict[str, Any]:
        """测试 AI 模型连接。"""
        config = self._normalize_runtime_config(raw_config, saved_api_key=saved_api_key)
        started = time.perf_counter()
        try:
            import litellm
        except Exception as exc:
            return {"success": False, "error": f"缺少 litellm 依赖: {exc}"}


        if not config.get("model"):
            return {"success": False, "error": "model 不能为空"}
        if not config.get("api_key"):
            return {"success": False, "error": "api_key 不能为空"}


        messages = [
            {"role": "user", "content": "你好，请回复\"OK\"表示连接正常。"}
        ]
        # 与 AI 字幕配对保持一致：流式优先。流式下 timeout 是“两次数据块之间的停滞超时”，
        # 首包到达即证明链路连通，因此直接尊重用户配置的超时（最长 300 秒），
        # 慢速推理模型 / 高延迟代理不再被固定的 15 秒上限误杀；wait_for 只做总时长兜底。
        probe_timeout = max(5, min(_safe_int(config.get("timeout_seconds"), 30), 300))
        kwargs = self._completion_kwargs(config, messages, timeout_seconds=probe_timeout)
        from .ai_subtitle_match_service import (
            _extract_litellm_stream_delta,
            _is_stream_unsupported_error,
            _litellm_stream_chunk_seen,
            extract_litellm_stream_delta_verbose,
            log_collected_text_fields,
            summarize_collected_text_fields,
        )

        saw_chunk_ref = {"seen": False}
        # 连接测试同样需要诊断能力：否则「模型返回为空」在探测侧依旧是黑盒。
        # 与标题翻译共用同一个 verbose 收集函数，保证两处日志口径一致。
        probe_fields_ref: Dict[str, str] = {}


        async def _stream_probe() -> Tuple[str, Dict[str, int]]:
            stream_kwargs = dict(kwargs)
            stream_kwargs["stream"] = True
            parts: List[str] = []
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            stream = await litellm.acompletion(**stream_kwargs)
            async for chunk in stream:
                if _litellm_stream_chunk_seen(chunk):
                    saw_chunk_ref["seen"] = True
                delta, chunk_usage = _extract_litellm_stream_delta(chunk)
                if delta:
                    parts.append(delta)
                usage = _merge_stream_usage(usage, chunk_usage)
                _had_delta, probe_collected, _probe_usage, _ = (
                    extract_litellm_stream_delta_verbose(chunk)
                )
                for key, text in (probe_collected or {}).items():
                    probe_fields_ref[key] = probe_fields_ref.get(key, "") + text
            return "".join(parts), usage


        hard_timeout = probe_timeout + 5
        try:
            async with _temporary_proxy(config.get("proxy_url", "")):
                try:
                    content, usage = await asyncio.wait_for(_stream_probe(), timeout=hard_timeout)
                except (asyncio.TimeoutError, TimeoutError):
                    raise
                except Exception as exc:
                    if not _is_stream_unsupported_error(exc):
                        raise
                    logger.warning("[AI标题] 连接测试流式不可用，回退非流式: %s", exc)
                    response = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=hard_timeout)
                    content, usage = _extract_litellm_content(response)
            if not _safe_text(content):
                # 诊断：模型到底返回了哪些字段（连接测试侧原来完全看不到）。
                logger.info(
                    "[AI标题] 连接测试响应汇总: saw_chunk=%s usage=%s 字段长度=%s",
                    saw_chunk_ref["seen"],
                    usage,
                    summarize_collected_text_fields(probe_fields_ref) or "{}",
                )
                log_collected_text_fields("[AI标题]", probe_fields_ref, label="连接测试")
                if not content:
                    # 与翻译主链路同口径：正文为空但有其他文本字段时回退使用。
                    _probe_key, _probe_text = _pick_fallback_text(probe_fields_ref)
                    if _probe_text:
                        logger.warning(
                            "[AI标题] 连接测试正文为空，回退使用字段[%s]（%s 字符）",
                            _probe_key,
                            len(_probe_text),
                        )
                total_tokens = _safe_int((usage or {}).get("total_tokens"))
                completion_tokens = _safe_int((usage or {}).get("completion_tokens"))
                if saw_chunk_ref["seen"]:
                    # 收到过带 choices 的流式响应即证明链路连通：推理模型（如智谱
                    # GLM 思考版）在小 max_tokens 下思考占满额度、正文为空，且
                    # 部分服务（智谱流式）不返回 usage——不能判失败。
                    content = "（模型服务有响应但未输出正文，可能是推理模型的思考占满了输出额度：连接正常）"
                elif total_tokens <= 0 and completion_tokens <= 0:
                    raise ValueError("empty_response: 模型未返回内容")
                else:
                    # 推理模型常见：思考消耗了全部输出额度导致正文为空，但服务确实有响应，判定连接正常。
                    content = "（模型服务有响应但未输出正文，可能是推理模型的思考占满了输出额度：连接正常）"
            duration_ms = int((time.perf_counter() - started) * 1000)
            return {
                "success": True,
                "duration_ms": duration_ms,
                "model": config.get("model", ""),
                "response": content[:200],
                "tokens": usage.get("total_tokens", 0),
            }
        except (asyncio.TimeoutError, TimeoutError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            return {
                "success": False,
                "error": {
                    "code": "timeout",
                    "title": "请求超时",
                    "suggestion": "调大超时时间，或检查代理/模型服务响应速度",
                },
                "duration_ms": duration_ms,
                "model": config.get("model", ""),
            }
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            error_info = _normalize_error(exc)
            return {
                "success": False,
                "error": error_info,
                "duration_ms": duration_ms,
                "model": config.get("model", ""),
            }




_ai_title_translation_service: Optional[AITitleTranslationService] = None




def get_ai_title_translation_service() -> AITitleTranslationService:
    global _ai_title_translation_service
    if _ai_title_translation_service is None:
        _ai_title_translation_service = AITitleTranslationService()
    return _ai_title_translation_service