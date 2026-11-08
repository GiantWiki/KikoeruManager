"""内置用户脚本分发（概览页「插件说明」使用）。

设计约束：
- 只暴露 ``tampermonkey/`` 下**白名单**里的脚本；不遍历目录、不接受任意文件名，
  避免这个只读接口变成任意文件读取入口。
- 源码运行时目录取仓库根；PyInstaller 打包后优先取 ``sys._MEIPASS``，其次取可执行
  文件同级目录。都没有时接口仍可访问，只是 ``available=false``，由前端降级提示
  「从仓库 tampermonkey/ 目录获取」。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/userscripts", tags=["userscripts"])

# 白名单：key -> 元数据。filename 必须与 tampermonkey/ 下的文件名完全一致。
_SCRIPTS: Dict[str, Dict[str, Any]] = {
    "panai": {
        "filename": "kikoeru-panai.user.js",
        "name": "网盘智能识别助手（KikoeruManager 改版）",
        "version": "2.1.0",
        "summary": "选中文字里的网盘链接，一键送进 KikoeruManager 下载面板。",
        "supports": ["百度网盘（规范格式）", "Gofile", "PikPak", "Transfer.it"],
    },
    "voicelinks": {
        "filename": "kikoeru-voicelinks.user.js",
        "name": "VoiceLinks（KikoeruManager 改版）",
        "version": "5.0.4.4",
        "summary": "RJ 号自动变链接、内联库存圆点、Telegram 字幕下载、本地索引查重。",
        "supports": ["RJ 号解析", "库存圆点", "字幕下载", "查重"],
    },
}


def _script_dir() -> Optional[Path]:
    """按「环境变量 → 仓库/Docker 根 → 打包目录 → 可执行文件同级」顺序找脚本目录。

    源码运行时 ``__file__`` 是 ``<repo>/backend/app/api/userscripts.py``，仓库根在
    parents[3]；Docker 镜像里是 ``/app/app/api/userscripts.py``，根在 parents[2]。
    所以按父级逐层探测，而不是写死某一层。
    """
    candidates: List[Path] = []
    env_value = str(os.environ.get("USERSCRIPTS_PATH") or "").strip()
    if env_value:
        candidates.append(Path(env_value))
    try:
        for parent in list(Path(__file__).resolve().parents)[:5]:
            candidates.append(parent / "tampermonkey")
    except OSError:
        pass
    candidates.append(Path.cwd() / "tampermonkey")
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        candidates.append(Path(bundle_root) / "tampermonkey")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "tampermonkey")

    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def _describe(key: str) -> Dict[str, Any]:
    meta = _SCRIPTS[key]
    directory = _script_dir()
    path = (directory / meta["filename"]) if directory else None
    available = bool(path and path.is_file())
    size = 0
    if available:
        try:
            size = int(path.stat().st_size)
        except OSError:
            available = False
    return {
        "key": key,
        "name": meta["name"],
        "version": meta["version"],
        "summary": meta["summary"],
        "supports": list(meta.get("supports") or []),
        "filename": meta["filename"],
        "available": available,
        "size": size,
        "download_url": f"/api/userscripts/{key}/download" if available else "",
    }


@router.get("")
async def list_userscripts() -> Dict[str, Any]:
    """列出内置脚本；脚本缺失时返回 available=false，不报错。"""
    scripts = [_describe(key) for key in _SCRIPTS]
    return {
        "directory_found": _script_dir() is not None,
        "scripts": scripts,
    }


@router.get("/{key}/download")
async def download_userscript(key: str) -> FileResponse:
    """下载指定脚本（仅白名单 key）。"""
    meta = _SCRIPTS.get(str(key or "").strip().lower())
    if not meta:
        raise HTTPException(status_code=404, detail="脚本不存在")

    directory = _script_dir()
    if directory is None:
        raise HTTPException(
            status_code=404,
            detail="当前安装未内置用户脚本目录，请从仓库 tampermonkey/ 目录获取",
        )
    path = directory / meta["filename"]
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"脚本文件缺失：tampermonkey/{meta['filename']}",
        )
    return FileResponse(
        path=str(path),
        media_type="application/javascript; charset=utf-8",
        filename=meta["filename"],
    )
