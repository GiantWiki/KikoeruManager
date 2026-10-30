"""解压入库重复文件查重服务。

清理同一作品解压产物里的重复文件：多语言版本目录（如 日本語、简体中文、
繁體中文）携带同一内容时自动去重，并优先保留简体中文目录下的版本。

判重口径：
- 只比较不同且可识别的多语言版本目录；同一版本目录及普通目录不参与清理。
- 非文本文件：扩展名 + 文件大小 + 首尾各 1MB 的 SHA1 指纹完全一致才算重复。
- 文本文件（.lrc/.srt/.vtt/.ass/.ssa/.txt）：内容经繁体转简体归一化后指纹
  一致即判重，覆盖「简繁版本几乎无差异」的常见多版本结构；不依赖文件名。
被清理的文件移入过滤恢复区（任务详情可还原），不直接物理删除。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 文本类文件：读全文做简繁归一化指纹（readme / LRC / 字幕都覆盖）
TEXT_EXTENSIONS = {".lrc", ".srt", ".vtt", ".ass", ".ssa", ".txt"}
# 跳过压缩包/自解压文件，避免误删嵌套包
SKIP_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".exe"}

# 优先保留的简体中文目录标记（覆盖全称与常见缩写，与字幕同步服务的简体 keys 对齐）
PREFERRED_LANG_MARKERS = (
    "简体中文", "簡體中文", "简中", "簡中",
    "中文(简体)", "中文（简体）", "中文(簡体)", "中文（簡体）",
    "zh-hans", "zh_cn", "chs",
    "简体", "簡体", "汉化", "漢化",
)

# 只有这些目录之间才属于“多语言版本查重”的适用范围。
VERSION_LANG_MARKERS = PREFERRED_LANG_MARKERS + (
    "繁體中文", "繁体中文", "繁體", "繁体",
    "zh-hant", "zh_tw", "cht",
    "日本語", "日语", "日語",
)

# 首尾指纹各读 1MB
_FINGERPRINT_CHUNK = 1024 * 1024

_opencc_converter = None


def language_priority_of(relative_path: str) -> Optional[int]:
    """路径命中简体中文标记时返回 0（最高优先），否则返回 None。"""
    normalized = str(relative_path or "").replace("\\", "/").casefold()
    for marker in PREFERRED_LANG_MARKERS:
        if marker.casefold() in normalized:
            return 0
    return None


def _version_root_of(relative_path: str) -> Optional[str]:
    """返回语言版本目录的相对路径；普通目录不参与版本查重。"""
    normalized = str(relative_path or "").replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        folded = part.casefold()
        if any(marker.casefold() in folded for marker in VERSION_LANG_MARKERS):
            return "/".join(parts[:index + 1]).casefold()
    return None


def _t2s(text: str) -> str:
    """繁体转简体（opencc t2s）；转换器不可用时原样返回。"""
    global _opencc_converter
    try:
        if _opencc_converter is None:
            import opencc

            _opencc_converter = opencc.OpenCC("t2s")
        return _opencc_converter.convert(text)
    except Exception as exc:
        logger.debug("opencc 繁转简不可用，按原文比较: %s", exc)
        return text


def file_fingerprint(path: str) -> Optional[str]:
    """计算文件的轻量内容指纹（首尾各 1MB）；失败返回 None。"""
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return None
        with open(path, "rb") as handle:
            head = handle.read(_FINGERPRINT_CHUNK)
            tail = b""
            if size > _FINGERPRINT_CHUNK * 2:
                handle.seek(-_FINGERPRINT_CHUNK, os.SEEK_END)
                tail = handle.read(_FINGERPRINT_CHUNK)
        digest = hashlib.sha1()
        digest.update(head)
        if tail:
            digest.update(b"\x00--tail--\x00")
            digest.update(tail)
        return digest.hexdigest()
    except Exception as exc:
        logger.debug("计算文件指纹失败，跳过判重: path=%s error=%s", path, exc)
        return None


def _normalized_text_fingerprint(path: str) -> Optional[str]:
    """文本文件指纹：繁转简归一化后全文 SHA1；失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
        if not content.strip():
            return None
        normalized = _t2s(content)
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()
    except Exception as exc:
        logger.debug("计算文本归一化指纹失败，跳过判重: path=%s error=%s", path, exc)
        return None


def _collect_groups(root: str) -> Dict[Tuple, List[Dict[str, Any]]]:
    """扫描目录树，按判重键聚合文件。"""
    groups: Dict[Tuple, List[Dict[str, Any]]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in SKIP_EXTENSIONS:
                continue
            path = os.path.join(dirpath, name)
            try:
                size = int(os.path.getsize(path))
            except OSError:
                continue
            relative_path = os.path.relpath(path, root).replace("\\", "/")
            if ext in TEXT_EXTENSIONS:
                text_fingerprint = _normalized_text_fingerprint(path)
                if text_fingerprint is None:
                    continue
                key: Tuple = (ext, "text", text_fingerprint)
            else:
                fingerprint = file_fingerprint(path)
                if fingerprint is None:
                    continue
                key = (ext, size, fingerprint)
            groups.setdefault(key, []).append({
                "path": path,
                "relative_path": relative_path,
                "name": name,
                "size": size,
            })
    return groups


def _pick_winner(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """选保留项：简体中文路径最优先，其次路径更浅，再次相对路径字典序。"""

    def sort_key(item: Dict[str, Any]) -> tuple:
        relative_path = str(item.get("relative_path") or "")
        preferred = language_priority_of(relative_path)
        return (
            0 if preferred == 0 else 1,
            relative_path.count("/"),
            relative_path.casefold(),
        )

    return min(items, key=sort_key)


async def deduplicate_version_files(root: str, task: Any) -> Dict[str, Any]:
    """执行重复文件查重，返回本次清理结果（removed_items 含恢复区信息）。"""
    result: Dict[str, Any] = {
        "removed_count": 0,
        "removed_items": [],
        "skipped": None,
    }
    if not root or not os.path.isdir(root):
        result["skipped"] = "work_dir_missing"
        return result

    groups = await asyncio.to_thread(_collect_groups, root)
    from .filter_recovery_service import get_filter_recovery_service

    recovery_service = get_filter_recovery_service()
    removed_items: List[Dict[str, Any]] = []
    for items in groups.values():
        if len(items) < 2:
            continue
        versioned_items = []
        version_roots = set()
        for item in items:
            version_root = _version_root_of(item["relative_path"])
            if version_root is None:
                continue
            versioned_items.append(item)
            version_roots.add(version_root)
        # 内容相同不等于多语言重复版本：同目录的连续音轨、空字幕等必须保留。
        if len(version_roots) < 2:
            continue
        winner = _pick_winner(versioned_items)
        winner_version_root = _version_root_of(winner["relative_path"])
        for item in versioned_items:
            if _version_root_of(item["relative_path"]) == winner_version_root:
                continue
            try:
                recovery = await asyncio.to_thread(
                    recovery_service.capture_item,
                    task.id,
                    item["path"],
                    relative_path=item["relative_path"],
                    entry_type="file",
                    size=item["size"],
                )
            except Exception as exc:
                logger.warning(
                    "重复文件清理失败，保留原文件: path=%s error=%s",
                    item["path"],
                    exc,
                    exc_info=True,
                )
                continue
            public_item = {
                **item,
                "type": "file",
                **recovery,
            }
            removed_items.append(public_item)
            logger.info(
                "重复文件清理: %s（保留 %s）",
                item["relative_path"],
                winner["relative_path"],
            )

    result["removed_items"] = removed_items
    result["removed_count"] = len(removed_items)
    return result
