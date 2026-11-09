"""入库后扫描的「暂存库」拦截。

现场问题：作品 13:32:42 落到暂存库 ``/library/RJ016xxxxx``，13:32:57 触发定向
扫描（媒体库 ``RJ016xxxxx`` = ``.../汉化/RJ016xxxxx``），扫描 1 秒就结束、
新增 0；13:34:39 用户「一键移库」把它搬到 ``/汉化/RJ016xxxxx``，13:35:16 手动
扫描才入库。根因是**扫描时机**：入库完成时作品还不存在于 Kikoeru 的媒体库。

因此：暂存库里的路径一律不扫（等移库后由 ``/api/library/move-to-real`` 触发），
只有真正落在媒体库路径下（尾部命中至少两段）才发 ``PERFORM_SCAN``。
"""

import asyncio
from types import SimpleNamespace

import pytest

import app.core.kikoeru_scan_trigger as trigger_module
from app.core.kikoeru_scan_trigger import KikoeruScanTrigger

ROOTS = [
    {"name": "RJ015xxxxx", "path": "/usr/src/kikoeru/VoiceWork/汉化/RJ015xxxxx"},
    {"name": "RJ016xxxxx", "path": "/usr/src/kikoeru/VoiceWork/汉化/RJ016xxxxx"},
    {"name": "未处理", "path": "/usr/src/kikoeru/VoiceWork/汉化/未处理"},
]


def _fake_config(staging: str = "/library", enabled: bool = True):
    return SimpleNamespace(
        kikoeru_db=SimpleNamespace(enabled=enabled, scan_trigger_enabled=enabled),
        storage=SimpleNamespace(library_path=staging),
    )


@pytest.fixture(autouse=True)
def fresh_trigger(monkeypatch):
    KikoeruScanTrigger._instance = None
    trigger_module._trigger = None
    # 不真的起合并窗口任务
    monkeypatch.setattr(KikoeruScanTrigger, "_ensure_worker", lambda self: None)
    yield
    KikoeruScanTrigger._instance = None
    trigger_module._trigger = None


def _prepare(monkeypatch, staging: str = "/library", enabled: bool = True):
    trigger = KikoeruScanTrigger()
    monkeypatch.setattr(trigger_module, "get_config", lambda: _fake_config(staging, enabled))

    async def fake_roots(force: bool = False):
        return ROOTS

    monkeypatch.setattr(trigger, "_load_roots", fake_roots)
    return trigger


def test_staging_work_is_not_scanned(monkeypatch) -> None:
    trigger = _prepare(monkeypatch)
    result = asyncio.run(
        trigger.trigger_for_path("/library/RJ016xxxxx/[RJ01692977][作品]", "RJ01692977")
    )

    assert result["queued"] is False
    assert result["reason"] == "staging_library"
    assert trigger._pending == {}


def test_staging_subdir_deep_under_bucket_is_not_scanned(monkeypatch) -> None:
    trigger = _prepare(monkeypatch)
    result = asyncio.run(
        trigger.trigger_for_path("/library/RJ016xxxxx/社团组/[RJ01692977][作品]", "RJ01692977")
    )

    assert result["reason"] == "staging_library"
    assert trigger._pending == {}


def test_real_library_work_is_scanned(monkeypatch) -> None:
    trigger = _prepare(monkeypatch)
    result = asyncio.run(
        trigger.trigger_for_path("/汉化/RJ016xxxxx/[RJ01692977][作品]", "RJ01692977")
    )

    assert result["queued"] is True
    assert result["root_folder"] == "RJ016xxxxx"
    assert trigger._pending == {"RJ016xxxxx": "RJ01692977"}


def test_media_library_next_to_work_root_is_scanned(monkeypatch) -> None:
    # 媒体库根下一层就是作品（尾部命中「汉化/未处理」两段），照扫
    trigger = _prepare(monkeypatch)
    result = asyncio.run(
        trigger.trigger_for_path("/汉化/未处理/[RJ01692977][作品]", "RJ01692977")
    )

    assert result["queued"] is True
    assert result["root_folder"] == "未处理"


def test_staging_root_that_is_a_media_library_still_scans(monkeypatch) -> None:
    # 有些用户的默认解压库存直接就是 Kikoeru 媒体库，此时必须照扫
    trigger = _prepare(monkeypatch, staging="/汉化")
    result = asyncio.run(
        trigger.trigger_for_path("/汉化/RJ016xxxxx/[RJ01692977][作品]", "RJ01692977")
    )

    assert result["queued"] is True
    assert result["root_folder"] == "RJ016xxxxx"


def test_rjcode_fallback_outside_staging(monkeypatch) -> None:
    # 路径尾部跟媒体库完全对不上（挂载前缀不同），但不在暂存库，按 RJ 号反推
    trigger = _prepare(monkeypatch)
    result = asyncio.run(trigger.trigger_for_path("/mnt/别的盘/[RJ01692977][作品]", "RJ01692977"))

    assert result["queued"] is True
    assert result["root_folder"] == "RJ016xxxxx"


def test_rjcode_fallback_not_used_inside_staging(monkeypatch) -> None:
    trigger = _prepare(monkeypatch)
    result = asyncio.run(trigger.trigger_for_path("/library/乱七八糟/[RJ01692977][作品]", "RJ01692977"))

    assert result["reason"] == "staging_library"
    assert trigger._pending == {}


def test_disabled_switch_short_circuits(monkeypatch) -> None:
    trigger = _prepare(monkeypatch, enabled=False)
    result = asyncio.run(
        trigger.trigger_for_path("/汉化/RJ016xxxxx/[RJ01692977][作品]", "RJ01692977")
    )

    assert result["reason"] == "disabled"
    assert trigger._pending == {}


def test_empty_path_is_rejected(monkeypatch) -> None:
    trigger = _prepare(monkeypatch)
    result = asyncio.run(trigger.trigger_for_path("", "RJ01692977"))

    assert result["queued"] is False
    assert trigger._pending == {}
