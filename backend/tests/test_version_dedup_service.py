import hashlib
import os
from types import SimpleNamespace

import pytest

from app.core.filter_recovery_service import FilterRecoveryService
from app.core.version_dedup_service import (
    deduplicate_version_files,
    file_fingerprint,
    language_priority_of,
)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(content)


def _padding_bytes(seed, size):
    data = b"RIFF" + hashlib.sha1(str(seed).encode()).digest() + b"\x00" * size
    return data[:size]


def test_language_priority_prefers_simplified_chinese():
    assert language_priority_of("简体中文/WAV/01.wav") == 0
    assert language_priority_of("簡體中文/WAV/01.wav") == 0
    assert language_priority_of("簡体/WAV/01.wav") == 0
    assert language_priority_of("简体/WAV/01.wav") == 0
    assert language_priority_of("中文(简体)/LRC/01.lrc") == 0
    assert language_priority_of("漢化/WAV/01.wav") == 0
    assert language_priority_of("繁體中文/WAV/01.wav") is None
    assert language_priority_of("繁体/WAV/01.wav") is None
    assert language_priority_of("日本語/WAV/01.wav") is None
    assert language_priority_of("") is None


def test_file_fingerprint_same_content_same_fingerprint(tmp_path):
    left = tmp_path / "a.wav"
    right = tmp_path / "b.wav"
    content = _padding_bytes(1, 300 * 1024)
    _write(str(left), content)
    _write(str(right), content)
    assert file_fingerprint(str(left)) == file_fingerprint(str(right))


def test_file_fingerprint_differs_on_content(tmp_path):
    left = tmp_path / "a.wav"
    right = tmp_path / "b.wav"
    _write(str(left), _padding_bytes(1, 300 * 1024))
    _write(str(right), _padding_bytes(2, 300 * 1024))
    assert file_fingerprint(str(left)) != file_fingerprint(str(right))


@pytest.mark.asyncio
async def test_deduplicate_keeps_simplified_chinese_audio(tmp_path, monkeypatch):
    root = tmp_path / "work"
    content = _padding_bytes(1, 256 * 1024)
    for rel in ["日本語/WAV/01.wav", "简体中文/WAV/01.wav", "繁體中文/WAV/01.wav"]:
        _write(str(root / rel), content)
    _write(str(root / "cover.jpg"), b"jpeg-data")

    service = FilterRecoveryService(recovery_root=str(tmp_path / "recovery"))
    monkeypatch.setattr("app.core.filter_recovery_service.get_filter_recovery_service", lambda: service)

    result = await deduplicate_version_files(str(root), SimpleNamespace(id="t-audio"))

    assert result["removed_count"] == 2
    assert os.path.exists(str(root / "简体中文/WAV/01.wav")), "应只保留简体中文目录里的音频"
    assert not os.path.exists(str(root / "日本語/WAV/01.wav"))
    assert not os.path.exists(str(root / "繁體中文/WAV/01.wav"))
    assert os.path.exists(str(root / "cover.jpg")), "无重复的非音频文件不受影响"
    assert all(item.get("recovery_id") for item in result["removed_items"])


@pytest.mark.asyncio
async def test_deduplicate_keeps_same_directory_numbered_tracks(tmp_path, monkeypatch):
    """普通目录里的连续音轨及同名 VTT 不属于多语言版本，不能按内容去重。"""
    root = tmp_path / "work"
    contents = {
        "01": b"dice-a",
        "02": b"dice-b",
        "03": b"dice-c",
        "04": b"dice-d",
        "05": b"dice-e",
        "06": b"dice-d",
        "07": b"dice-c",
        "08": b"dice-b",
    }
    for number, content in contents.items():
        _write(str(root / f"音声/骰子/骰子{number}.wav"), content)
        _write(str(root / f"音声/骰子/骰子{number}.wav.vtt"), "WEBVTT\n")

    service = FilterRecoveryService(recovery_root=str(tmp_path / "recovery"))
    monkeypatch.setattr("app.core.filter_recovery_service.get_filter_recovery_service", lambda: service)

    result = await deduplicate_version_files(str(root), SimpleNamespace(id="t-dice"))

    assert result["removed_count"] == 0
    for number in contents:
        assert os.path.exists(str(root / f"音声/骰子/骰子{number}.wav"))
        assert os.path.exists(str(root / f"音声/骰子/骰子{number}.wav.vtt"))


@pytest.mark.asyncio
async def test_deduplicate_does_not_remove_plain_directory_copy(tmp_path, monkeypatch):
    """多语言版本可以互判，但不能连带删除普通目录里的同内容文件。"""
    root = tmp_path / "work"
    for rel in ["简体中文/WAV/01.wav", "繁體中文/WAV/01.wav", "音声/骰子/骰子06.wav"]:
        _write(str(root / rel), b"shared-audio")

    service = FilterRecoveryService(recovery_root=str(tmp_path / "recovery"))
    monkeypatch.setattr("app.core.filter_recovery_service.get_filter_recovery_service", lambda: service)

    result = await deduplicate_version_files(str(root), SimpleNamespace(id="t-mixed-dirs"))

    assert result["removed_count"] == 1
    assert os.path.exists(str(root / "简体中文/WAV/01.wav"))
    assert not os.path.exists(str(root / "繁體中文/WAV/01.wav"))
    assert os.path.exists(str(root / "音声/骰子/骰子06.wav"))


@pytest.mark.asyncio
async def test_deduplicate_preserves_copies_inside_winning_language_root(tmp_path, monkeypatch):
    """跨语言判重不能连带清理胜者语言根内部的同内容副本。"""
    root = tmp_path / "work"
    for rel in ["简体中文/a.wav", "简体中文/copy/a.wav", "繁體中文/a.wav"]:
        _write(str(root / rel), b"shared-audio")

    service = FilterRecoveryService(recovery_root=str(tmp_path / "recovery"))
    monkeypatch.setattr("app.core.filter_recovery_service.get_filter_recovery_service", lambda: service)

    result = await deduplicate_version_files(str(root), SimpleNamespace(id="t-same-language-copy"))

    assert result["removed_count"] == 1
    assert os.path.exists(str(root / "简体中文/a.wav"))
    assert os.path.exists(str(root / "简体中文/copy/a.wav"))
    assert not os.path.exists(str(root / "繁體中文/a.wav"))


@pytest.mark.asyncio
async def test_deduplicate_normalizes_simplified_traditional_text(tmp_path, monkeypatch):
    """简繁文本内容一致（仅字体差异）时判重，保留简体路径版本。"""
    root = tmp_path / "work"
    simplified_lines = [
        "[00:01.00]用身体支付也是可以的，您打算怎么做呢？",
        "[00:02.00]面无表情房东的认真逆强奸",
    ]
    traditional_lines = [
        "[00:01.00]用身體支付也是可以的，您打算怎麼做呢？",
        "[00:02.00]面無表情房東的認真逆強姦",
    ]
    _write(str(root / "简体中文/LRC/01.mp3.lrc"), "\n".join(simplified_lines))
    _write(str(root / "繁體中文/LRC/01.mp3.lrc"), "\n".join(traditional_lines))

    service = FilterRecoveryService(recovery_root=str(tmp_path / "recovery"))
    monkeypatch.setattr("app.core.filter_recovery_service.get_filter_recovery_service", lambda: service)

    result = await deduplicate_version_files(str(root), SimpleNamespace(id="t-text"))

    assert result["removed_count"] == 1, "繁简归一化后内容一致的文本应判重"
    assert os.path.exists(str(root / "简体中文/LRC/01.mp3.lrc")), "保留简体版本"
    assert not os.path.exists(str(root / "繁體中文/LRC/01.mp3.lrc"))


@pytest.mark.asyncio
async def test_deduplicate_keeps_genuinely_different_text(tmp_path, monkeypatch):
    """简繁文本内容真正不同时不能判重。"""
    root = tmp_path / "work"
    _write(str(root / "简体中文/LRC/01.lrc"), "[00:01.00]完全不同的内容A")
    _write(str(root / "繁體中文/LRC/01.lrc"), "[00:01.00]完全不同的內容B")

    service = FilterRecoveryService(recovery_root=str(tmp_path / "recovery"))
    monkeypatch.setattr("app.core.filter_recovery_service.get_filter_recovery_service", lambda: service)

    result = await deduplicate_version_files(str(root), SimpleNamespace(id="t-text2"))

    assert result["removed_count"] == 0
    assert os.path.exists(str(root / "简体中文/LRC/01.lrc"))
    assert os.path.exists(str(root / "繁體中文/LRC/01.lrc"))


@pytest.mark.asyncio
async def test_deduplicate_prefers_abbreviated_simplified_dir(tmp_path, monkeypatch):
    """目录名使用缩写（簡体 / 繁体）时，也应保留簡体目录版本。"""
    root = tmp_path / "work"
    simplified_lines = "[00:01.00]用身体支付也是可以的"
    traditional_lines = "[00:01.00]用身體支付也是可以的"
    _write(str(root / "簡体/LRC/01.lrc"), simplified_lines)
    _write(str(root / "繁体/LRC/01.lrc"), traditional_lines)

    service = FilterRecoveryService(recovery_root=str(tmp_path / "recovery"))
    monkeypatch.setattr("app.core.filter_recovery_service.get_filter_recovery_service", lambda: service)

    result = await deduplicate_version_files(str(root), SimpleNamespace(id="t-abbr"))

    assert result["removed_count"] == 1
    assert os.path.exists(str(root / "簡体/LRC/01.lrc")), "保留簡体目录版本"
    assert not os.path.exists(str(root / "繁体/LRC/01.lrc"))


@pytest.mark.asyncio
async def test_deduplicate_images_and_readme(tmp_path, monkeypatch):
    """字节完全一致的图片与 readme 也应判重并保留简体路径版本。"""
    root = tmp_path / "work"
    _write(str(root / "简体中文/插图/插图.jpg"), b"\xff\xd8" + b"\x01" * 4096)
    _write(str(root / "繁體中文/插图/插图.jpg"), b"\xff\xd8" + b"\x01" * 4096)
    _write(str(root / "简体中文/readme.txt"), "同捆特典说明")
    _write(str(root / "繁體中文/readme.txt"), "同捆特典說明")

    service = FilterRecoveryService(recovery_root=str(tmp_path / "recovery"))
    monkeypatch.setattr("app.core.filter_recovery_service.get_filter_recovery_service", lambda: service)

    result = await deduplicate_version_files(str(root), SimpleNamespace(id="t-img"))

    assert result["removed_count"] == 2
    assert os.path.exists(str(root / "简体中文/插图/插图.jpg"))
    assert os.path.exists(str(root / "简体中文/readme.txt"))
    assert not os.path.exists(str(root / "繁體中文/插图/插图.jpg"))
    assert not os.path.exists(str(root / "繁體中文/readme.txt"))


@pytest.mark.asyncio
async def test_deduplicate_skips_nested_archives(tmp_path, monkeypatch):
    root = tmp_path / "work"
    _write(str(root / "简体中文/bonus.zip"), b"PK\x03\x04" + b"\x02" * 4096)
    _write(str(root / "繁體中文/bonus.zip"), b"PK\x03\x04" + b"\x02" * 4096)

    service = FilterRecoveryService(recovery_root=str(tmp_path / "recovery"))
    monkeypatch.setattr("app.core.filter_recovery_service.get_filter_recovery_service", lambda: service)

    result = await deduplicate_version_files(str(root), SimpleNamespace(id="t-zip"))

    assert result["removed_count"] == 0, "压缩包不参与查重"
    assert os.path.exists(str(root / "简体中文/bonus.zip"))
    assert os.path.exists(str(root / "繁體中文/bonus.zip"))


@pytest.mark.asyncio
async def test_deduplicate_skips_missing_dir(tmp_path, monkeypatch):
    service = FilterRecoveryService(recovery_root=str(tmp_path / "recovery"))
    monkeypatch.setattr("app.core.filter_recovery_service.get_filter_recovery_service", lambda: service)
    result = await deduplicate_version_files(str(tmp_path / "not-exist"), SimpleNamespace(id="t"))
    assert result["removed_count"] == 0
    assert result["skipped"] == "work_dir_missing"
