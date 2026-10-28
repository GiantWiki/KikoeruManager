# -*- coding: utf-8 -*-
"""AI 标题汉化服务的输出健壮性回归测试。

历史事故：max_tokens 硬编码 200，文件级重命名返回「标题 + 全部文件名」的 JSON
映射时输出被截断成半个 JSON；旧清理逻辑把 JSON 残骸当翻译成功结果传下去，
项目文件夹被重命名成 `RJxxxx {` 之类的垃圾名。这里固定回归：
- finish_reason=length（截断）必须判失败并进入重试；
- 有 `{` 无 `}` 的不完整 JSON 必须判失败；
- 完整 JSON 映射、纯文本翻译仍按原逻辑接受。
"""
from __future__ import annotations

import sys
import types

import pytest

from app.core.ai_title_translation_service import AITitleTranslationService


def _make_response(content: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _install_fake_litellm(monkeypatch: pytest.MonkeyPatch, responses: list) -> None:
    """把假 litellm 塞进 sys.modules，acompletion 依次弹出 responses。"""
    queue = list(responses)

    async def acompletion(**kwargs):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))


def _config(**overrides) -> dict:
    base = {
        "enabled": True,
        "model": "openai/gpt-4o-mini",
        "api_key": "sk-test",
        "max_retries": 0,
        "temperature": 0.1,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_truncated_json_finish_reason_length_is_failure(monkeypatch):
    _install_fake_litellm(monkeypatch, [_make_response('{"作品": "标题", "01文件": "01翻', finish_reason="length")])
    service = AITitleTranslationService()
    result = await service.translate_single("作品", _config())
    assert result["success"] is False
    assert result["translated_title"] is None
    assert "截断" in str(result.get("error", {}).get("suggestion", "")) or result.get("status") == "failed"


@pytest.mark.asyncio
async def test_incomplete_json_without_closing_brace_is_failure(monkeypatch):
    _install_fake_litellm(monkeypatch, [_make_response('{\n  "作品": "标题",\n  "01文件": "01翻译', finish_reason="stop")])
    service = AITitleTranslationService()
    result = await service.translate_single("作品", _config())
    assert result["success"] is False
    assert result["translated_title"] is None


@pytest.mark.asyncio
async def test_truncation_retries_then_succeeds(monkeypatch):
    _install_fake_litellm(monkeypatch, [
        _make_response('{"作品": "标题", "01文件": "01翻', finish_reason="length"),
        _make_response('{"作品": "标题翻译", "01文件": "01翻译"}', finish_reason="stop"),
    ])
    service = AITitleTranslationService()
    result = await service.translate_single("作品", _config(max_retries=1))
    assert result["success"] is True
    assert "标题翻译" in result["translated_title"]
    assert "01翻译" in result["translated_title"]


@pytest.mark.asyncio
async def test_complete_json_mapping_returns_redumped_json(monkeypatch):
    payload = '{"作品": "标题翻译", "01文件": "01翻译"}'
    _install_fake_litellm(monkeypatch, [_make_response(payload)])
    service = AITitleTranslationService()
    result = await service.translate_single("作品", _config())
    assert result["success"] is True
    assert "标题翻译" in result["translated_title"]
    assert "01翻译" in result["translated_title"]


@pytest.mark.asyncio
async def test_plain_text_translation_still_accepted(monkeypatch):
    _install_fake_litellm(monkeypatch, [_make_response("这是翻译后的标题")])
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config())
    assert result["success"] is True
    assert result["translated_title"] == "这是翻译后的标题"


def test_completion_kwargs_uses_configured_max_tokens():
    service = AITitleTranslationService()
    assert service._completion_kwargs(_config(max_tokens=8192), [])["max_tokens"] == 8192
    assert service._completion_kwargs(_config(), [])["max_tokens"] == 4096


# ---------------------------------------------------------------------------
# 推理模型「正文为空」回归（v2.6.45）
#
# 用户实测：服务商控制台显示输出 12,304 token，本地日志却是 error=模型返回为空。
# 根因：推理模型把输出全花在 reasoning_content，标准 content 为空，而流式解析
# 只读 content 字段，且没有任何诊断日志可看出「模型其实返回了什么」。
# 这里固定三件事：
#   1. 流式路径要能从非 content 字段回退取到正文（不报空响应）；
#   2. 通用收集不硬编码字段名（未知自定义字段也要收到）；
#   3. usage 按字段 merge，不能被后续分片整体覆盖丢证据。
# ---------------------------------------------------------------------------

class _FakeStream:
    """带 __aiter__ 的假流式响应，让 _call_model 走流式分支。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _delta_chunk(delta: dict, *, finish_reason: str = "", usage: dict | None = None) -> dict:
    chunk: dict = {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _install_fake_litellm_stream(monkeypatch: pytest.MonkeyPatch, chunks: list) -> None:
    async def acompletion(**kwargs):
        return _FakeStream(chunks)

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))


class _FakePydanticExtraDelta:
    """复刻 litellm 1.106 Delta：正文在 extra，__dict__ 只有值为 None 的声明字段。"""

    def __init__(self, **extra):
        self.reasoning_content = None
        self.thinking_blocks = None
        self.reasoning_items = None
        self.provider_specific_fields = None
        self.__pydantic_extra__ = dict(extra)

    def __getattr__(self, name):
        # pydantic v2 在常规属性查找失败后，从 __pydantic_extra__ 取扩展字段。
        extra = object.__getattribute__(self, "__pydantic_extra__")
        try:
            return extra[name]
        except KeyError:
            raise AttributeError(name) from None


def _object_delta_chunk(delta, *, finish_reason: str = "") -> dict:
    return {"choices": [{"delta": delta, "finish_reason": finish_reason}]}


def test_extract_verbose_picks_reasoning_when_content_empty():
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    chunk = _delta_chunk({"content": "", "reasoning_content": "让我分析这个标题…"})
    primary, collected, _usage, _reason = extract_litellm_stream_delta_verbose(chunk)
    assert primary == "让我分析这个标题…"
    assert collected["reasoning_content"] == "让我分析这个标题…"
    assert "content" not in collected  # 空内容不入收集


def test_extract_verbose_collects_unknown_custom_field():
    """通用性：未预置的字段名也要能收到，不为具体模型写特判。"""
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    chunk = _delta_chunk({"content": "", "answer_text": "自定义字段正文"})
    primary, collected, _usage, _reason = extract_litellm_stream_delta_verbose(chunk)
    assert collected.get("answer_text") == "自定义字段正文"
    assert primary == "自定义字段正文"


def test_extract_verbose_prefers_content_over_reasoning():
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    chunk = _delta_chunk({"content": "正文", "reasoning_content": "思考"})
    primary, collected, _usage, _reason = extract_litellm_stream_delta_verbose(chunk)
    assert primary == "正文"
    assert collected["reasoning_content"] == "思考"


def test_extract_verbose_reads_content_from_pydantic_extra():
    """litellm 1.106 的 content 不在 __dict__，不能只遍历声明字段。"""
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    delta = _FakePydanticExtraDelta(content="[繁體中文版] 標題", role="assistant")
    primary, collected, _usage, reason = extract_litellm_stream_delta_verbose(
        _object_delta_chunk(delta, finish_reason="stop")
    )
    assert primary == "[繁體中文版] 標題"
    assert collected == {"content": "[繁體中文版] 標題"}
    assert reason == "stop"


def test_extract_verbose_reads_unknown_custom_field_from_pydantic_extra():
    """extra 中的自定义字段也要保留，不能退化成只探测固定字段名。"""
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    delta = _FakePydanticExtraDelta(answer_text="自定义 extra 正文")
    primary, collected, _usage, _reason = extract_litellm_stream_delta_verbose(
        _object_delta_chunk(delta)
    )
    assert primary == "自定义 extra 正文"
    assert collected == {"answer_text": "自定义 extra 正文"}


@pytest.mark.asyncio
async def test_stream_translation_accepts_pydantic_extra_content(monkeypatch):
    """完整流式链路不能再把 extra 中的正文误判成「未解析到任何文本字段」。"""
    _install_fake_litellm_stream(monkeypatch, [
        _object_delta_chunk(_FakePydanticExtraDelta(content="翻译后的")),
        _object_delta_chunk(_FakePydanticExtraDelta(content="标题")),
        _object_delta_chunk(_FakePydanticExtraDelta(), finish_reason="stop"),
    ])
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config())
    assert result["success"] is True, result
    assert result["translated_title"] == "翻译后的标题"


def test_exclude_structural_fields_from_text_collection():
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    chunk = _delta_chunk({"role": "assistant", "content": "x", "tool_calls": [{"id": "1"}]})
    _primary, collected, _usage, _reason = extract_litellm_stream_delta_verbose(chunk)
    assert "role" not in collected
    assert "tool_calls" not in collected
    assert collected["content"] == "x"


def test_verbose_returns_usage_and_finish_reason():
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    chunk = _delta_chunk({}, finish_reason="stop", usage={"completion_tokens": 12304})
    _primary, _collected, usage, reason = extract_litellm_stream_delta_verbose(chunk)
    assert usage["completion_tokens"] == 12304
    assert reason == "stop"


@pytest.mark.asyncio
async def test_stream_reasoning_only_content_is_used_not_failed(monkeypatch):
    """核心回归：推理模型只在 reasoning_content 输出时，必须取到正文而非判失败。"""
    _install_fake_litellm_stream(monkeypatch, [
        _delta_chunk({"content": "", "reasoning_content": "（思考）先分析日文标题"},
                    usage={"prompt_tokens": 10}),
        _delta_chunk({"content": "", "reasoning_content": "圣职者姐妹"}, usage={"completion_tokens": 12304}),
        _delta_chunk({}, finish_reason="stop"),
    ])
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config())
    assert result["success"] is True, result
    assert "圣职者姐妹" in result["translated_title"]


@pytest.mark.asyncio
async def test_stream_usage_merged_across_chunks(monkeypatch):
    """usage 必须按字段 merge：后面的分片不能把前面的 token 计数覆盖掉。"""
    captured: dict = {}

    async def acompletion(**kwargs):
        return _FakeStream([
            _delta_chunk({"content": "a"}, usage={"prompt_tokens": 10}),
            _delta_chunk({"content": "b"}, usage={"completion_tokens": 12304}),
            _delta_chunk({}, finish_reason="stop"),
        ])

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))
    service = AITitleTranslationService()

    original = service._call_model

    async def spy(config, work_name):
        content, usage = await original(config, work_name)
        captured["usage"] = usage
        return content, usage

    monkeypatch.setattr(service, "_call_model", spy)
    result = await service.translate_single("タイトル", _config())
    assert result["success"] is True, result
    # 两个字段分别来自不同分片，merge 后都要在。
    assert captured["usage"]["prompt_tokens"] == 10
    assert captured["usage"]["completion_tokens"] == 12304


def test_non_retryable_error_does_not_include_truncation():
    """⚠️ 截断必须保持可重试（既有 test_truncation_retries_then_succeeds 依赖）。"""
    from app.core.ai_title_translation_service import _is_non_retryable_error

    assert _is_non_retryable_error(ValueError(
        "模型输出达到 max_tokens 上限（200）被截断，请在 AI 标题汉化设置中调大输出 token 上限"
    )) is False
    assert _is_non_retryable_error(ValueError("模型返回了不完整的 JSON（缺少结束括号），输出可能被截断")) is True
    assert _is_non_retryable_error(ValueError("Authentication Fails, Your api key is invalid")) is True
    assert _is_non_retryable_error(TimeoutError("read timeout")) is False
    assert _is_non_retryable_error(ConnectionError("connection reset by peer")) is False


@pytest.mark.asyncio
async def test_auth_error_does_not_retry(monkeypatch):
    """认证失败属确定性错误：不应重试（实测一次失败空转 16 秒）。"""
    calls = {"n": 0}

    async def acompletion(**kwargs):
        calls["n"] += 1
        raise RuntimeError("Authentication Fails, Your api key: ****jRhL is invalid")

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config(max_retries=3))
    assert result["success"] is False
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# /api/ai-title-translation/test 接口回归
#
# v2.5.26 用户实测：设置页"AI 标题汉化 → 测试连接"点击无反应。双重根因：
#   1. 前端 <stateful-button :click="..."> 传了不存在的 prop，组件只认 @click；
#   2. 后端 /api/ai-title-translation/test 接口根本不存在（404）。
# 这里固定后端接口行为：普通模式用表单草稿参数 + 掩码 Key 回退；
# use_ai_subtitle_api=true 时复用字幕配对连接配置。
# ---------------------------------------------------------------------------

def _install_fake_litellm_capture(monkeypatch: pytest.MonkeyPatch, capture: dict):
    async def acompletion(**kwargs):
        capture["kwargs"] = kwargs
        return _make_response('{"テスト": "测试"}')

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))


def test_test_endpoint_exists_and_translates(client, monkeypatch):
    capture: dict = {}
    _install_fake_litellm_capture(monkeypatch, capture)

    resp = client.post(
        "/api/ai-title-translation/test",
        json={"config": {"enabled": True, "model": "openai/gpt-4o-mini", "api_key": "sk-test"}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["translated_title"] == "测试"
    assert capture["kwargs"]["model"] == "openai/gpt-4o-mini"


def test_test_endpoint_reuses_subtitle_api_config(client, monkeypatch):
    capture: dict = {}
    _install_fake_litellm_capture(monkeypatch, capture)

    saved_model = client.app.state  # noqa: F841  仅确保 app 可访问
    from app.config.settings import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "ai_subtitle_matching", type(
        "SubCfg",
        (),
        {
            "model": "openai/subtitle-model",
            "api_key": "sk-sub",
            "api_base": "https://sub.example.com/v1",
            "api_version": "",
            "organization": "",
            "proxy_url": "",
            "timeout_seconds": 45,
            "max_retries": 1,
            "temperature": 0.2,
        },
    )(), raising=False)

    resp = client.post(
        "/api/ai-title-translation/test",
        json={"config": {"use_ai_subtitle_api": True}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert capture["kwargs"]["model"] == "openai/subtitle-model"
    assert capture["kwargs"]["api_key"] == "sk-sub"
    assert capture["kwargs"]["api_base"] == "https://sub.example.com/v1"


def test_test_endpoint_masked_api_key_falls_back_to_disk(client, monkeypatch):
    capture: dict = {}
    _install_fake_litellm_capture(monkeypatch, capture)

    import app.api.routes as routes_mod
    monkeypatch.setattr(
        routes_mod,
        "_read_ai_title_translation_api_key_from_disk",
        lambda: "sk-saved-on-disk",
    )

    resp = client.post(
        "/api/ai-title-translation/test",
        json={"config": {"enabled": True, "model": "openai/gpt-4o-mini", "api_key": "********"}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert capture["kwargs"]["api_key"] == "sk-saved-on-disk"


# ---------------------------------------------------------------------------
# 推理模型「思考被当成答案」回归（v2.6.50）
#
# 用户实测：推理模型把全部输出写在 reasoning_content（10200 字），content 为空。
# v2.6.45 为防「有输出却判空」把 reasoning_content 提升为主文本，导致思考文本
# 被当答案送进 JSON 校验，报 `Extra data: line 4 column 1 (char 84)` 这类与事实
# 不符的错误（模型其实没返回坏 JSON，是解析对象错了）。
#
# 这里固定：
#   1. 思考字段绝不进入返回文本（正文只由 content/text/未知自定义字段拼成）；
#   2. 思考里含多个 {} 片段时，回退路径必须宽松提取、不得报「JSON 无法解析」；
#   3. 正文为 JSON 时，历史严格行为不变；
#   4. ⚠️ v2.6.45 红线（无 {} 的思考文本仍被判成功）不得回归。
# ---------------------------------------------------------------------------

def test_pick_answer_piece_skips_reasoning_fields():
    from app.core.ai_title_translation_service import _pick_answer_piece

    assert _pick_answer_piece({"reasoning_content": "思考中"}) == ""
    assert _pick_answer_piece({"reasoning": "思考中", "thinking": "更多思考"}) == ""
    assert _pick_answer_piece({"content": "正文"}) == "正文"
    assert _pick_answer_piece({"reasoning_content": "思考", "content": "正文"}) == "正文"
    # 未知自定义字段（非思考类）仍可作答案，保持通用性。
    assert _pick_answer_piece({"answer_text": "自定义正文"}) == "自定义正文"
    assert _pick_answer_piece({"reasoning_content": "思考", "answer_text": "自定义正文"}) == "自定义正文"


def test_scan_first_json_object_ignores_fragmented_braces():
    from app.core.ai_title_translation_service import _scan_first_json_object

    # 思考文本里先有示例、再有真答案：扫描应取到第一个可解析的 dict。
    text = '我们需要回答用户。{key: value} 输出 JSON 格式：\n{"作品": "标题翻译"}\n综上。'
    assert _scan_first_json_object(text) == {"作品": "标题翻译"}
    # 完全扫不到合法 dict。
    assert _scan_first_json_object("没有任何括号") is None
    assert _scan_first_json_object("{不是合法JSON }") is None


def test_extract_translation_text_reasoning_fallback_no_strict_json_error():
    from app.core.ai_title_translation_service import _extract_translation_text

    # 思考文本：多个 {} 片段 + 夹带真答案；回退路径不得抛「JSON 无法解析」。
    reasoning = (
        "用户要求：输出 JSON 格式，key 为原文，value 为翻译结果：\n"
        '分析：{"ごめんね": "对不起"}\n'
        "综上，翻译完成。{补充说明}"
    )
    out = _extract_translation_text(reasoning, reasoning_fallback=True)
    assert "对不起" in out


def test_extract_translation_text_reasoning_fallback_plain_text():
    from app.core.ai_title_translation_service import _extract_translation_text

    # 思考文本没有任何 JSON：按纯文本返回（去包裹引号），不报错。
    out = _extract_translation_text("「思考：直接翻译即可」", reasoning_fallback=True)
    assert out == "思考：直接翻译即可"


@pytest.mark.asyncio
async def test_reasoning_content_with_fragmented_braces_does_not_fail_as_bad_json(monkeypatch):
    """核心回归：推理模型只在 reasoning_content 输出、且思考里含多个 {} 片段。"""
    _install_fake_litellm_stream(monkeypatch, [
        _delta_chunk({"content": "", "reasoning_content": "用户要求输出 JSON 格式：{key: value}\n我准备翻译："}),
        _delta_chunk({"content": "", "reasoning_content": '{"タイトル": "标题翻译"}\n综上，完成。'}),
        _delta_chunk({}, finish_reason="stop"),
    ])
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config(max_retries=0))
    assert result["success"] is True, result
    assert "标题翻译" in result["translated_title"]
    # 绝不能出现误导性的「JSON 无法解析」。
    assert "无法解析" not in str(result.get("error", {}))


@pytest.mark.asyncio
async def test_reasoning_only_without_braces_still_succeeds(monkeypatch):
    """⚠️ v2.6.45 红线：无 {} 的思考文本必须仍被判成功（不得因本次修复回归）。

    这条与既有的 test_stream_reasoning_only_content_is_used_not_failed 同义，
    额外把「content 非空时思考不参与正文」的新语义一并锁住。
    """
    _install_fake_litellm_stream(monkeypatch, [
        _delta_chunk({"content": "", "reasoning_content": "（思考）先分析日文标题"}),
        _delta_chunk({"content": "", "reasoning_content": "圣职者姐妹"}),
        _delta_chunk({}, finish_reason="stop"),
    ])
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config())
    assert result["success"] is True, result
    assert "圣职者姐妹" in result["translated_title"]


@pytest.mark.asyncio
async def test_content_present_reasoning_excluded_from_answer(monkeypatch):
    """content 与 reasoning_content 同时存在：正文只含 content，思考不混入。"""
    _install_fake_litellm_stream(monkeypatch, [
        _delta_chunk({"content": "", "reasoning_content": "思考部分不该出现"}),
        _delta_chunk({"content": '{"タイトル": "正确标题"}'}),
        _delta_chunk({}, finish_reason="stop"),
    ])
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config())
    assert result["success"] is True, result
    assert result["translated_title"] == "正确标题"
    assert "思考部分" not in result["translated_title"]


@pytest.mark.asyncio
async def test_content_json_still_strictly_parsed(monkeypatch):
    """正文就是 JSON 映射：历史严格行为不变（多值重新 dump、单值取值）。"""
    _install_fake_litellm_stream(monkeypatch, [
        _delta_chunk({"content": '{"作品": "标题翻译", "01文件": "01翻译"}'}),
        _delta_chunk({}, finish_reason="stop"),
    ])
    service = AITitleTranslationService()
    result = await service.translate_single("作品", _config())
    assert result["success"] is True, result
    assert "标题翻译" in result["translated_title"]
    assert "01翻译" in result["translated_title"]


# ---------------------------------------------------------------------------
# v2.6.50 二次修复：思考文本不得整段当标题（用户实测 RJ01668964）
#
# v2.6.50 首版把 reasoning_content 排除出正文后，回退路径「宽松提取」在
# **思考里没有 JSON** 时会把整段 9638 字思考当标题返回，导致 [RENAME] 把文件夹
# 改名成「[RJ01668964][我们需要回答用户。用户要求充当翻译家…」。
#
# 这里固定：
#   1. 思考文本里有「翻译：」等标记 → 抠出答案行；
#   2. 长 / 多行思考且抠不到答案 → 明确失败，绝不返回整段思考；
#   3. ⚠️ 短单行思考（v2.6.45 形态，如「圣职者姐妹」）仍照常接受，不得回归；
#   4. 候选标题超长 / 多行分析措辞 → 拒绝（防垃圾名写库）。
# ---------------------------------------------------------------------------

def test_looks_like_reasoning_text_short_single_line_is_not_reasoning():
    from app.core.ai_title_translation_service import _looks_like_reasoning_text

    # v2.6.45 形态：短单行 → 不是思考，必须照常接受。
    assert _looks_like_reasoning_text("圣职者姐妹") is False
    assert _looks_like_reasoning_text("这是翻译后的标题") is False
    # 长文本 → 是思考。
    assert _looks_like_reasoning_text("很长" * 400) is True
    # 多行 + 分析措辞 → 是思考。
    assert _looks_like_reasoning_text("我们需要回答用户。\n用户要求翻译成中文。\n逐条分析。") is True


def test_extract_answer_from_reasoning_marker():
    from app.core.ai_title_translation_service import _extract_answer_from_reasoning

    text = (
        "我们需要回答用户。用户要求翻译。\n"
        "日文标题：\nごめんね、マネージャーくん\n"
        "翻译：\n对不起，经纪人君\n"
        "或“抱歉呢”\n需要自然。"
    )
    assert _extract_answer_from_reasoning(text) == "对不起，经纪人君"


def test_extract_translation_text_reasoning_with_marker_returns_answer():
    from app.core.ai_title_translation_service import _extract_translation_text

    reasoning = (
        "我们需要回答用户。用户要求充当翻译家，输出 JSON 格式。\n"
        "日文标题：\nごめんね、マネージャーくん ～イケメス特撮女優の肉体改造アクメ地獄堕ち報告～\n"
        "翻译：\n对不起，经纪人君 ～帅哥系特摄女演员的肉体改造高潮地狱堕落报告～\n"
        "或“抱歉呢，经纪人小弟”\n需要自然。"
    )
    out = _extract_translation_text(reasoning, reasoning_fallback=True)
    assert out == "对不起，经纪人君 ～帅哥系特摄女演员的肉体改造高潮地狱堕落报告～"


def test_extract_translation_text_long_reasoning_without_answer_fails():
    """核心回归：长思考且无标记答案 → 必须失败，绝不返回整段思考。"""
    from app.core.ai_title_translation_service import _extract_translation_text

    long_reasoning = "我们需要回答用户。用户要求翻译。\n逐条分析中……\n综上，需要再想想。" + "这段思考很长。" * 300
    with pytest.raises(ValueError) as exc_info:
        _extract_translation_text(long_reasoning, reasoning_fallback=True)
    assert "思考" in str(exc_info.value)


def test_extract_translation_text_short_single_line_reasoning_accepted():
    """⚠️ v2.6.45 兼容：短单行推理字段内容仍可作为答案返回。"""
    from app.core.ai_title_translation_service import _extract_translation_text

    assert _extract_translation_text("圣职者姐妹", reasoning_fallback=True) == "圣职者姐妹"


def test_reject_overlong_plain_text_title():
    from app.core.ai_title_translation_service import _extract_translation_text

    with pytest.raises(ValueError):
        _extract_translation_text("很长" * 400)


@pytest.mark.asyncio
async def test_reasoning_only_long_text_does_not_pollute_title(monkeypatch):
    """端到端：思考里没有可提取答案时，任务必须失败而不是把思考写进标题。"""
    _install_fake_litellm_stream(monkeypatch, [
        _delta_chunk({"content": "", "reasoning_content": "我们需要回答用户。用户要求翻译成中文。"}),
        _delta_chunk({"content": "", "reasoning_content": "逐条分析：\n第一，确定语种。\n第二，决定用词。\n综上，还需再想。" + "很长思考。" * 200}),
        _delta_chunk({}, finish_reason="stop"),
    ])
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config(max_retries=0))
    assert result["success"] is False, result
    assert result["translated_title"] is None


# ---------------------------------------------------------------------------
# max_tokens 由运营商自报额度决定（发现式，不硬编码模型名）
# ---------------------------------------------------------------------------

def test_resolve_max_tokens_uses_user_value_when_within_limit():
    service = AITitleTranslationService()
    cfg = _config(model="m", api_base="https://api.example.com", max_tokens=4096)
    assert service._resolve_max_tokens(cfg) == 4096


def test_resolve_max_tokens_clamps_to_discovered_provider_limit():
    from app.core.ai_subtitle_match_service import (
        cache_model_token_limits,
        _model_token_limits_cache,
    )

    _model_token_limits_cache.clear()
    cache_model_token_limits(
        "https://api.example.com",
        [{"id": "m", "max_output_tokens": 8192}],
    )
    service = AITitleTranslationService()
    cfg = _config(model="m", api_base="https://api.example.com", max_tokens=99999)
    assert service._resolve_max_tokens(cfg) == 8192


def test_resolve_max_tokens_falls_back_to_conservative_when_unknown():
    from app.core.ai_subtitle_match_service import _model_token_limits_cache

    _model_token_limits_cache.clear()
    service = AITitleTranslationService()
    cfg = _config(model="unknown-model", api_base="https://api.example.com", max_tokens=200000)
    # 未发现运营商上限 → 保守兜底，绝不把超大值透传给服务端
    assert service._resolve_max_tokens(cfg) == AITitleTranslationService._FALLBACK_MAX_OUTPUT_TOKENS


def test_resolve_max_tokens_never_below_200():
    service = AITitleTranslationService()
    cfg = _config(model="m", api_base="https://api.example.com", max_tokens=1)
    assert service._resolve_max_tokens(cfg) >= 200


def test_extract_model_token_limits_deepseek_shape():
    from app.core.ai_subtitle_match_service import extract_model_token_limits

    limits = extract_model_token_limits({
        "id": "deepseek-reasoner",
        "context_window": 128000,
        "max_output_tokens": 65536,
    })
    assert limits["max_output_tokens"] == 65536
    assert limits["context_window"] == 128000


def test_extract_model_token_limits_aliases_and_nested():
    from app.core.ai_subtitle_match_service import extract_model_token_limits

    limits = extract_model_token_limits({
        "id": "some-model",
        "context_length": 32000,
        "top_provider": {"max_output_tokens": 4096},
    })
    assert limits["max_output_tokens"] == 4096
    assert limits["context_window"] == 32000


def test_extract_model_token_limits_missing_fields_default_zero():
    from app.core.ai_subtitle_match_service import extract_model_token_limits

    limits = extract_model_token_limits({"id": "x"})
    assert limits == {"max_output_tokens": 0, "context_window": 0}


def test_normalize_model_entries_carries_limits():
    from app.core.ai_subtitle_match_service import AISubtitleMatchService

    service = AISubtitleMatchService()
    rows = service._normalize_model_entries(
        {"data": [{"id": "m", "max_output_tokens": 8192, "context_window": 128000}]},
        {"api_base": "https://api.example.com"},
    )
    assert len(rows) == 1
    assert rows[0]["max_output_tokens"] == 8192
    assert rows[0]["context_window"] == 128000
