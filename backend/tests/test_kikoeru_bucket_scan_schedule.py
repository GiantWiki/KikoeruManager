"""定向扫描调度与失败清理的安全边界（v2.6.70 线上回归）。

事故经过：``_schedule_kikoeru_bucket_scan`` 里调了 ``get_config()``，但本模块没有
模块级导入（全项目约定是函数体内 ``from ..config.settings import get_config``），
于是作品已经解压、命名、分类入库、归档入队、社团补全全部写完之后抛 ``NameError``，
任务被判定失败，紧接着 ``_cleanup_failed_task`` 把刚入库的 2GB 成品目录整目录删掉。
日志现场（RJ01692977，2026-11-09 12:44:06）::

    File "/app/app/core/task_engine.py", line 2691, in _schedule_kikoeru_bucket_scan
        config = get_config()
    NameError: name 'get_config' is not defined
    清理失败任务缓存: /library/RJ016xxxxx/[RJ01692977][...]

本文件锁两条边界：
1. 收尾阶段的扫描调度，无论取配置失败还是 trigger 内部异常，都不得冒泡；
2. 失败清理不得整目录删除已经落在库存根下的成品。
"""

from types import SimpleNamespace

import pytest

from app.config import settings as settings_module
from app.core.task_engine import Task, TaskEngine, TaskStatus, TaskType


def _config(*, enabled=True, scan_trigger_enabled=True, temp_path=""):
    return SimpleNamespace(
        kikoeru_db=SimpleNamespace(
            enabled=enabled,
            scan_trigger_enabled=scan_trigger_enabled,
        ),
        storage=SimpleNamespace(temp_path=str(temp_path or "")),
    )


def _task(tmp_path, output_path=None):
    return Task(
        task_type=TaskType.AUTO_PROCESS,
        source_path=str(tmp_path / "RJ01692977(RJ01692977).rar"),
        output_path=output_path,
        rjcode="RJ01692977",
    )


class _RecordingTrigger:
    def __init__(self):
        self.calls = []

    def schedule(self, path, rjcode="", reason=""):
        self.calls.append({"path": path, "rjcode": rjcode, "reason": reason})


# ---------------------------------------------------------------------------
# 扫描调度：任何异常都不能影响任务收尾
# ---------------------------------------------------------------------------

def test_schedule_scan_never_raises_when_config_lookup_fails(tmp_path, monkeypatch):
    """取配置本身抛错时保持静默（v2.6.70 的 NameError 就是这一类）。"""

    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_module, "get_config", _boom)

    engine = TaskEngine(max_concurrent=1)
    # 不抛异常即通过：收尾阶段的副作用不能反噬已经入库的成品
    engine._schedule_kikoeru_bucket_scan(
        _task(tmp_path), "/library/RJ016xxxxx/some-work", "RJ01692977"
    )


def test_schedule_scan_skips_when_switch_disabled(tmp_path, monkeypatch):
    trigger = _RecordingTrigger()
    monkeypatch.setattr(settings_module, "get_config", lambda: _config(enabled=False))
    monkeypatch.setattr(
        "app.core.kikoeru_scan_trigger.get_kikoeru_scan_trigger", lambda: trigger
    )

    TaskEngine(max_concurrent=1)._schedule_kikoeru_bucket_scan(
        _task(tmp_path), "/library/RJ016xxxxx/some-work", "RJ01692977"
    )

    assert trigger.calls == []


def test_schedule_scan_queues_resolved_path(tmp_path, monkeypatch):
    trigger = _RecordingTrigger()
    monkeypatch.setattr(settings_module, "get_config", lambda: _config())
    monkeypatch.setattr(
        "app.core.kikoeru_scan_trigger.get_kikoeru_scan_trigger", lambda: trigger
    )

    TaskEngine(max_concurrent=1)._schedule_kikoeru_bucket_scan(
        _task(tmp_path), "/library/RJ016xxxxx/some-work", "RJ01692977"
    )

    assert trigger.calls == [
        {
            "path": "/library/RJ016xxxxx/some-work",
            "rjcode": "RJ01692977",
            "reason": "auto_process",
        }
    ]


def test_schedule_scan_swallows_trigger_errors(tmp_path, monkeypatch):
    class _Broken:
        def schedule(self, *_args, **_kwargs):
            raise RuntimeError("socket 连接炸了")

    monkeypatch.setattr(settings_module, "get_config", lambda: _config())
    monkeypatch.setattr(
        "app.core.kikoeru_scan_trigger.get_kikoeru_scan_trigger", lambda: _Broken()
    )

    TaskEngine(max_concurrent=1)._schedule_kikoeru_bucket_scan(
        _task(tmp_path), "/library/RJ016xxxxx/some-work", "RJ01692977"
    )


def test_schedule_scan_ignores_empty_path(tmp_path, monkeypatch):
    trigger = _RecordingTrigger()
    monkeypatch.setattr(settings_module, "get_config", lambda: _config())
    monkeypatch.setattr(
        "app.core.kikoeru_scan_trigger.get_kikoeru_scan_trigger", lambda: trigger
    )

    TaskEngine(max_concurrent=1)._schedule_kikoeru_bucket_scan(_task(tmp_path), "", "")

    assert trigger.calls == []


# ---------------------------------------------------------------------------
# 失败清理：库存内成品保留，库存外临时目录照旧删除
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cleanup_keeps_failed_output_inside_library(tmp_path, monkeypatch):
    library_root = tmp_path / "library"
    output = library_root / "RJ016xxxxx" / "[RJ01692977][作品]"
    output.mkdir(parents=True)

    monkeypatch.setattr(
        settings_module, "get_config", lambda: _config(temp_path=tmp_path / "temp")
    )
    fake_manager = SimpleNamespace(
        find_local_library_for_path=lambda _path: SimpleNamespace(id="lib-1", type="local")
    )
    monkeypatch.setattr(
        "app.core.library_manager.get_library_manager", lambda: fake_manager
    )

    task = _task(tmp_path, output_path=str(output))
    task.status = TaskStatus.FAILED

    await TaskEngine(max_concurrent=1)._cleanup_failed_task(task)

    assert output.is_dir(), "已入库的成品不能被失败清理整目录删除"


@pytest.mark.asyncio
async def test_cleanup_removes_failed_output_outside_library(tmp_path, monkeypatch):
    output = tmp_path / "temp" / "RJ01692977_unpacked"
    output.mkdir(parents=True)

    monkeypatch.setattr(
        settings_module, "get_config", lambda: _config(temp_path=tmp_path / "temp")
    )
    monkeypatch.setattr(
        "app.core.library_manager.get_library_manager",
        lambda: SimpleNamespace(find_local_library_for_path=lambda _path: None),
    )

    task = _task(tmp_path, output_path=str(output))
    task.status = TaskStatus.FAILED

    await TaskEngine(max_concurrent=1)._cleanup_failed_task(task)

    assert not output.exists(), "库存外的临时解压目录仍应照旧清理"


@pytest.mark.asyncio
async def test_cleanup_keeps_output_when_library_probe_fails(tmp_path, monkeypatch):
    output = tmp_path / "unknown" / "work"
    output.mkdir(parents=True)

    monkeypatch.setattr(
        settings_module, "get_config", lambda: _config(temp_path=tmp_path / "temp")
    )

    def _boom():
        raise RuntimeError("library config broken")

    monkeypatch.setattr("app.core.library_manager.get_library_manager", _boom)

    task = _task(tmp_path, output_path=str(output))
    task.status = TaskStatus.FAILED

    await TaskEngine(max_concurrent=1)._cleanup_failed_task(task)

    assert output.is_dir(), "判断不出归属时应保守保留，不冒险删成品"
