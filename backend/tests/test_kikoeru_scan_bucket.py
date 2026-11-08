"""入库后只扫对应 RJ 桶（Kikoeru 媒体库）的解析与发送规则。

现场形态：库存 ``/汉化/RJ016xxxxx/<作品目录>``，Kikoeru 把每个桶注册成独立
媒体库（``path=/usr/src/kikoeru/VoiceWork/汉化/RJ016xxxxx``，``name=RJ016xxxxx``），
``PERFORM_SCAN`` 的 payload 是媒体库**名**列表，空列表=全库。

这里锁定三件事：
1. 路径按尾部片段匹配媒体库（容器内前缀与本机前缀不同）；
2. 路径不中时按 RJ 号位数模式反推，且必须唯一；
3. 解析不出来就放弃，绝不回退全库扫描。
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core import kikoeru_scan_trigger as trigger_module
from app.core.kikoeru_scan_listener import KikoeruScanListener
from app.core.kikoeru_scan_trigger import KikoeruScanTrigger


ROOTS = [
    {"name": "未处理", "path": "/usr/src/kikoeru/VoiceWork/汉化/未处理"},
    {"name": "未汉化", "path": "/usr/src/kikoeru/VoiceWork/未汉化"},
    {"name": "RJ1xxxxx", "path": "/usr/src/kikoeru/VoiceWork/汉化/RJ1xxxxx"},
    {"name": "RJ015xxxxx", "path": "/usr/src/kikoeru/VoiceWork/汉化/RJ015xxxxx"},
    {"name": "RJ016xxxxx", "path": "/usr/src/kikoeru/VoiceWork/汉化/RJ016xxxxx"},
    {"name": "RJ017xxxxx", "path": "/usr/src/kikoeru/VoiceWork/汉化/RJ017xxxxx"},
]


# --------------------------------------------------------------------- 路径匹配
def test_resolve_by_path_matches_bucket_ignoring_mount_prefix() -> None:
    work = "/汉化/RJ016xxxxx/[RJ01664818][标题]"

    assert KikoeruScanTrigger.resolve_by_path(work, ROOTS) == "RJ016xxxxx"


def test_resolve_by_path_prefers_deeper_match() -> None:
    roots = [
        {"name": "汉化", "path": "/usr/src/kikoeru/VoiceWork/汉化"},
        {"name": "未汉化-RJ016", "path": "/usr/src/kikoeru/VoiceWork/未汉化/RJ016xxxxx"},
        {"name": "汉化-RJ016", "path": "/usr/src/kikoeru/VoiceWork/汉化/RJ016xxxxx"},
    ]
    work = "/未汉化/RJ016xxxxx/[RJ01664818][标题]"

    # 命中最深的“未汉化/RJ016xxxxx”，不会被同名的两个桶搞成歧义
    assert KikoeruScanTrigger.resolve_by_path(work, roots) == "未汉化-RJ016"


def test_resolve_by_path_returns_none_when_parent_is_not_a_root() -> None:
    work = "/汉化/[RJ01664818][标题]"

    assert KikoeruScanTrigger.resolve_by_path(work, ROOTS) is None


def test_resolve_by_path_handles_windows_separators() -> None:
    roots = [{"name": "RJ016xxxxx", "path": "\\\\NAS\\VoiceWork\\汉化\\RJ016xxxxx"}]
    work = "\\\\NAS\\VoiceWork\\汉化\\RJ016xxxxx\\[RJ01664818][标题]"

    assert KikoeruScanTrigger.resolve_by_path(work, roots) == "RJ016xxxxx"


def test_resolve_by_path_is_ambiguous_for_same_depth_twins() -> None:
    roots = [
        {"name": "A-RJ016", "path": "/mnt/a/RJ016xxxxx"},
        {"name": "B-RJ016", "path": "/mnt/b/RJ016xxxxx"},
    ]
    # 父目录只剩 RJ016xxxxx 一段可对齐，两个库一样深
    assert KikoeruScanTrigger.resolve_by_path("/RJ016xxxxx/[RJ1]", roots) is None


# --------------------------------------------------------------------- RJ 号反推
def test_resolve_by_rjcode_prefers_specific_bucket() -> None:
    assert KikoeruScanTrigger.resolve_by_rjcode("RJ01664818", ROOTS) == "RJ016xxxxx"
    assert KikoeruScanTrigger.resolve_by_rjcode("RJ01586348", ROOTS) == "RJ015xxxxx"


def test_resolve_by_rjcode_supports_six_digit_buckets() -> None:
    assert KikoeruScanTrigger.resolve_by_rjcode("RJ123456", ROOTS) == "RJ1xxxxx"


def test_resolve_by_rjcode_returns_none_when_no_bucket() -> None:
    assert KikoeruScanTrigger.resolve_by_rjcode("RJ09999999", ROOTS) is None
    assert KikoeruScanTrigger.resolve_by_rjcode("", ROOTS) is None


def test_resolve_by_rjcode_is_ambiguous_for_duplicate_names() -> None:
    roots = [
        {"name": "RJ016xxxxx", "path": "/mnt/a"},
        {"name": "RJ016xxxxx", "path": "/mnt/b"},
    ]

    assert KikoeruScanTrigger.resolve_by_rjcode("RJ01664818", roots) is None


# --------------------------------------------------------------------- 触发门控
def _patch_config(monkeypatch, *, enabled: bool = True, trigger: bool = True) -> None:
    monkeypatch.setattr(
        trigger_module,
        "get_config",
        lambda: SimpleNamespace(
            kikoeru_db=SimpleNamespace(enabled=enabled, scan_trigger_enabled=trigger)
        ),
    )


def test_trigger_is_disabled_by_config(monkeypatch) -> None:
    _patch_config(monkeypatch, enabled=False)
    trigger = KikoeruScanTrigger()

    result = asyncio.run(trigger.trigger_for_path("/汉化/RJ016xxxxx/[RJ1]", "RJ01664818"))

    assert result == {"queued": False, "reason": "disabled", "root_folder": ""}
    assert trigger._pending == {}


def test_trigger_queues_resolved_bucket_once(monkeypatch) -> None:
    _patch_config(monkeypatch)
    trigger = KikoeruScanTrigger()
    trigger._pending.clear()
    monkeypatch.setattr(trigger, "_ensure_worker", lambda: None)

    async def fake_roots(force: bool = False):
        return ROOTS

    monkeypatch.setattr(trigger, "_load_roots", fake_roots)

    first = asyncio.run(trigger.trigger_for_path("/汉化/RJ016xxxxx/[RJ1]", "RJ01664818"))
    second = asyncio.run(trigger.trigger_for_path("/汉化/RJ016xxxxx/[RJ2]", "RJ01664818"))

    assert first["queued"] is True and first["root_folder"] == "RJ016xxxxx"
    assert second["queued"] is True
    # 同一批入库合并成一个媒体库，不会重复排队
    assert trigger._pending == {"RJ016xxxxx": "RJ01664818"}
    trigger._pending.clear()


def test_trigger_gives_up_without_full_scan(monkeypatch) -> None:
    _patch_config(monkeypatch)
    trigger = KikoeruScanTrigger()
    trigger._pending.clear()

    async def fake_roots(force: bool = False):
        return ROOTS

    monkeypatch.setattr(trigger, "_load_roots", fake_roots)

    result = asyncio.run(trigger.trigger_for_path("/mnt/其它/[RJ01664818][标题]", ""))

    assert result["queued"] is False
    assert result["reason"] == "no_root_folder_match"
    assert trigger._pending == {}


# --------------------------------------------------------------------- 发送侧
class _FakeSio:
    def __init__(self) -> None:
        self.calls = []

    def emit(self, event, payload=None):
        self.calls.append((event, payload))


def _bare_listener() -> KikoeruScanListener:
    listener = object.__new__(KikoeruScanListener)
    listener._connected = False
    listener._scan_active = False
    listener._sio = None
    listener._last_scan_request_at = ""
    return listener


def test_request_scan_refuses_empty_root_folders() -> None:
    listener = _bare_listener()

    result = asyncio.run(listener.request_scan([]))

    assert result["started"] is False
    assert "全库" in result["reason"]


def test_request_scan_requires_connection() -> None:
    listener = _bare_listener()

    result = asyncio.run(listener.request_scan(["RJ016xxxxx"]))

    assert result["started"] is False
    assert result["reason"] == "socket 未连接"


def test_request_scan_emits_perform_scan_with_names() -> None:
    listener = _bare_listener()
    fake = _FakeSio()
    listener._connected = True
    listener._sio = fake

    result = asyncio.run(listener.request_scan(["RJ016xxxxx", " "] , reason="test"))

    assert result["started"] is True
    assert fake.calls == [("PERFORM_SCAN", {"rootFolders": ["RJ016xxxxx"]})]


def test_request_scan_waits_for_running_scan() -> None:
    listener = _bare_listener()
    fake = _FakeSio()
    listener._connected = True
    listener._sio = fake
    listener._scan_active = True

    async def main():
        async def clear_flag():
            await asyncio.sleep(0.05)
            listener._scan_active = False

        asyncio.get_running_loop().create_task(clear_flag())
        return await listener.request_scan(["RJ016xxxxx"], wait_timeout=5)

    result = asyncio.run(main())

    assert result["started"] is True
    assert result["waited_seconds"] >= 1
    assert fake.calls == [("PERFORM_SCAN", {"rootFolders": ["RJ016xxxxx"]})]


def test_request_scan_reports_emit_failure() -> None:
    listener = _bare_listener()

    class _Broken:
        def emit(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    listener._connected = True
    listener._sio = _Broken()

    result = asyncio.run(listener.request_scan(["RJ016xxxxx"]))

    assert result["started"] is False
    assert "boom" in result["reason"]
