"""定向扫描后的结果校验与补扫。

现场问题：作品 13:32:42 落盘、13:32:57 触发定向扫描，Kikoeru 报
「共找到 351 个、新增 0 个」——刚 rename 完的目录被当成「仍在写入」跳过，
直到 13:35 手动全量扫描才入库。单次 PERFORM_SCAN 不可靠，因此加一层
「扫描后回查 t_work，缺作品就隔 90 秒重发」的兜底。
"""

import asyncio

import pytest

import app.core.kikoeru_db_service as db_module
import app.core.kikoeru_scan_listener as listener_module
from app.core import kikoeru_scan_trigger as trigger_module
from app.core.kikoeru_scan_trigger import KikoeruScanTrigger


@pytest.fixture(autouse=True)
def fresh_trigger(monkeypatch):
    # 单例在测试之间会串味（_last_result / _pending），每个用例重建
    KikoeruScanTrigger._instance = None
    trigger_module._trigger = None
    monkeypatch.setattr(trigger_module, "_VERIFY_DELAY_SECONDS", 0)
    monkeypatch.setattr(trigger_module, "_VERIFY_RETRY_INTERVAL_SECONDS", 0)
    yield
    KikoeruScanTrigger._instance = None
    trigger_module._trigger = None


class FakeListener:
    def __init__(self, connected: bool = True):
        self.connected = connected
        self.calls = []

    def is_connected(self) -> bool:
        return self.connected

    async def request_scan(self, targets, reason="", wait_timeout=180.0):
        self.calls.append({"targets": list(targets), "reason": reason})
        return {"started": True}


def test_verify_stops_when_all_works_present(monkeypatch) -> None:
    async def fake_missing(codes):
        return []

    monkeypatch.setattr(KikoeruScanTrigger, "_missing_rj_ids", staticmethod(fake_missing))
    listener = FakeListener()
    monkeypatch.setattr(listener_module, "get_kikoeru_scan_listener", lambda: listener)

    trigger = KikoeruScanTrigger()
    asyncio.run(trigger._verify_scan_result(["RJ016xxxxx"], {"RJ016xxxxx": "RJ01692977"}))

    assert listener.calls == []


def test_verify_resends_once_then_passes(monkeypatch) -> None:
    calls = {"n": 0}

    async def fake_missing(codes):
        calls["n"] += 1
        # 第一次缺，第二次齐
        return ["RJ01692977"] if calls["n"] == 1 else []

    monkeypatch.setattr(KikoeruScanTrigger, "_missing_rj_ids", staticmethod(fake_missing))
    listener = FakeListener()
    monkeypatch.setattr(listener_module, "get_kikoeru_scan_listener", lambda: listener)

    trigger = KikoeruScanTrigger()
    asyncio.run(trigger._verify_scan_result(["RJ016xxxxx"], {"RJ016xxxxx": "RJ01692977"}))

    assert [c["targets"] for c in listener.calls] == [["RJ016xxxxx"]]
    assert listener.calls[0]["reason"] == "verify_retry1"


def test_verify_gives_up_after_max_attempts(monkeypatch) -> None:
    async def fake_missing(codes):
        return ["RJ01692977"]

    monkeypatch.setattr(KikoeruScanTrigger, "_missing_rj_ids", staticmethod(fake_missing))
    listener = FakeListener()
    monkeypatch.setattr(listener_module, "get_kikoeru_scan_listener", lambda: listener)

    trigger = KikoeruScanTrigger()
    asyncio.run(trigger._verify_scan_result(["RJ016xxxxx"], {"RJ016xxxxx": "RJ01692977"}))

    # 首发之外再重发 _VERIFY_MAX_ATTEMPTS - 1 次
    assert len(listener.calls) == trigger_module._VERIFY_MAX_ATTEMPTS - 1
    assert trigger._last_result["verified"] is False
    assert trigger._last_result["missing_works"] == ["RJ01692977"]


def test_verify_skips_resend_when_socket_disconnected(monkeypatch) -> None:
    async def fake_missing(codes):
        return ["RJ01692977"]

    monkeypatch.setattr(KikoeruScanTrigger, "_missing_rj_ids", staticmethod(fake_missing))
    listener = FakeListener(connected=False)
    monkeypatch.setattr(listener_module, "get_kikoeru_scan_listener", lambda: listener)

    trigger = KikoeruScanTrigger()
    asyncio.run(trigger._verify_scan_result(["RJ016xxxxx"], {"RJ016xxxxx": "RJ01692977"}))

    assert listener.calls == []


def test_verify_swallows_db_errors(monkeypatch) -> None:
    async def fake_missing(codes):
        raise RuntimeError("db boom")

    monkeypatch.setattr(KikoeruScanTrigger, "_missing_rj_ids", staticmethod(fake_missing))
    listener = FakeListener()
    monkeypatch.setattr(listener_module, "get_kikoeru_scan_listener", lambda: listener)

    trigger = KikoeruScanTrigger()
    # 不应抛出，最多记 debug
    asyncio.run(trigger._verify_scan_result(["RJ016xxxxx"], {"RJ016xxxxx": "RJ01692977"}))
    assert listener.calls == []


def test_missing_rj_ids_ignores_unparsable_codes() -> None:
    result = asyncio.run(KikoeruScanTrigger._missing_rj_ids(["乱七八糟", ""]))
    assert result == []


def test_missing_rj_ids_reports_missing_from_t_work(monkeypatch) -> None:
    class FakeConn:
        def execute(self, sql, params):
            return self

        def fetchall(self):
            # RJ01692977 在库，RJ01692978 不在
            return [{"id": 1692977}]

        def close(self):
            pass

    class FakeService:
        def _connect(self, *args, **kwargs):
            return FakeConn()

    monkeypatch.setattr(db_module, "get_kikoeru_db_service", lambda: FakeService())

    result = asyncio.run(
        KikoeruScanTrigger._missing_rj_ids(["RJ01692977", "RJ01692978"])
    )

    assert result == ["RJ01692978"]


def test_missing_rj_ids_returns_empty_when_db_unavailable(monkeypatch) -> None:
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db_module, "get_kikoeru_db_service", boom)

    result = asyncio.run(KikoeruScanTrigger._missing_rj_ids(["RJ01692977"]))

    # 查询失败不补扫，避免因 DB 抖动反复触发扫描
    assert result == []
