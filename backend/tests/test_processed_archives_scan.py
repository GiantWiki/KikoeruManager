import asyncio
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api import routes as routes_module
from app.config import settings as settings_module
from app.core.task_engine import Task, TaskEngine, TaskStatus, TaskType
from app.models import database as database_module
from app.models.database import ProcessedArchive


@pytest.mark.asyncio
async def test_scan_processed_archives_aggregates_exe_e_volume_size_and_removes_member_record(
    tmp_path,
    db_session,
    monkeypatch,
):
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    exe = processed_dir / "RJ01629292.exe"
    e01 = processed_dir / "RJ01629292.e01"
    e02 = processed_dir / "RJ01629292.e02"
    exe.write_bytes(b"x" * 700)
    e01.write_bytes(b"y" * 701)
    e02.write_bytes(b"z" * 123)

    db_session.add(
        ProcessedArchive(
            id="stale-member-record",
            original_path=str(e01),
            current_path=str(e01),
            filename="RJ01629292.e01",
            rjcode="RJ01629292",
            file_size=701,
            processed_at=datetime.now(),
            process_count=1,
            task_id="",
            status="completed",
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        settings_module,
        "get_config",
        lambda: SimpleNamespace(
            storage=SimpleNamespace(processed_archives_path=str(processed_dir))
        ),
    )
    monkeypatch.setattr(
        routes_module,
        "get_config",
        lambda: SimpleNamespace(
            storage=SimpleNamespace(processed_archives_path=str(processed_dir))
        ),
    )

    def fake_get_db():
        yield db_session

    monkeypatch.setattr(database_module, "get_db", fake_get_db)

    await routes_module.scan_processed_archives()

    records = db_session.query(ProcessedArchive).all()
    assert len(records) == 1
    assert records[0].filename == "RJ01629292.exe"
    assert records[0].current_path == str(exe)
    assert records[0].file_size == 1524
    assert records[0].volume_count == 3


@pytest.mark.asyncio
async def test_scan_processed_archives_skips_active_deferred_target_group(
    tmp_path,
    db_session,
    monkeypatch,
):
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    exe = processed_dir / "RJ00009999.exe"
    e01 = processed_dir / "RJ00009999.e01"
    exe.write_bytes(b"x" * 700)
    e01.write_bytes(b"y" * 701)

    monkeypatch.setattr(
        routes_module,
        "get_config",
        lambda: SimpleNamespace(
            storage=SimpleNamespace(processed_archives_path=str(processed_dir))
        ),
    )

    class FakeDeferredArchiveService:
        def active_target_paths_sync(self):
            return {
                str(exe.resolve()).casefold(),
                str(e01.resolve()).casefold(),
            }

    monkeypatch.setattr(
        "app.core.deferred_archive_service.get_deferred_archive_service",
        lambda: FakeDeferredArchiveService(),
    )

    def fake_get_db():
        yield db_session

    monkeypatch.setattr(database_module, "get_db", fake_get_db)

    await routes_module.scan_processed_archives()

    assert db_session.query(ProcessedArchive).filter(
        ProcessedArchive.current_path.in_([str(exe), str(e01)])
    ).count() == 0


@pytest.mark.asyncio
async def test_scan_processed_archives_keeps_records_when_directory_listing_fails(
    tmp_path,
    db_session,
    monkeypatch,
):
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    archive_path = processed_dir / "RJ01629292.zip"
    archive_path.write_bytes(b"payload")
    db_session.add(
        ProcessedArchive(
            id="keep-on-scan-error",
            original_path=str(archive_path),
            current_path=str(archive_path),
            filename=archive_path.name,
            rjcode="RJ01629292",
            file_size=archive_path.stat().st_size,
            processed_at=datetime.now(),
            process_count=1,
            task_id="",
            status="completed",
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        routes_module,
        "get_config",
        lambda: SimpleNamespace(
            storage=SimpleNamespace(processed_archives_path=str(processed_dir))
        ),
    )

    class FakeDeferredArchiveService:
        def active_target_paths_sync(self):
            return set()

    monkeypatch.setattr(
        "app.core.deferred_archive_service.get_deferred_archive_service",
        lambda: FakeDeferredArchiveService(),
    )

    def fake_get_db():
        yield db_session

    monkeypatch.setattr(database_module, "get_db", fake_get_db)
    monkeypatch.setattr(routes_module.os, "listdir", lambda _path: (_ for _ in ()).throw(OSError("SMB unavailable")))

    await routes_module.scan_processed_archives()

    assert db_session.query(ProcessedArchive).filter_by(id="keep-on-scan-error").count() == 1


@pytest.mark.asyncio
async def test_scan_processed_archives_keeps_records_when_member_stat_fails(
    tmp_path,
    db_session,
    monkeypatch,
):
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    archive_path = processed_dir / "RJ01629293.zip"
    archive_path.write_bytes(b"payload")
    db_session.add(
        ProcessedArchive(
            id="keep-on-stat-error",
            original_path=str(archive_path),
            current_path=str(archive_path),
            filename=archive_path.name,
            rjcode="RJ01629293",
            file_size=archive_path.stat().st_size,
            processed_at=datetime.now(),
            process_count=1,
            task_id="",
            status="completed",
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        routes_module,
        "get_config",
        lambda: SimpleNamespace(
            storage=SimpleNamespace(processed_archives_path=str(processed_dir))
        ),
    )

    class FakeDeferredArchiveService:
        def active_target_paths_sync(self):
            return set()

    monkeypatch.setattr(
        "app.core.deferred_archive_service.get_deferred_archive_service",
        lambda: FakeDeferredArchiveService(),
    )

    def fake_get_db():
        yield db_session

    real_getsize = routes_module.os.path.getsize
    normalized_archive = os.path.normcase(os.path.abspath(str(archive_path)))

    def fail_member_stat(path):
        if os.path.normcase(os.path.abspath(str(path))) == normalized_archive:
            raise OSError("SMB stat unavailable")
        return real_getsize(path)

    monkeypatch.setattr(database_module, "get_db", fake_get_db)
    monkeypatch.setattr(routes_module.os.path, "getsize", fail_member_stat)

    await routes_module.scan_processed_archives()

    assert db_session.query(ProcessedArchive).filter_by(id="keep-on-stat-error").count() == 1


class _FakeArchiveQuery:
    def __init__(self, archive):
        self.archive = archive

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.archive


class _FakeArchiveDB:
    def __init__(self, archive):
        self.archive = archive
        self.commit_count = 0
        self.closed = False

    def query(self, _model):
        return _FakeArchiveQuery(self.archive)

    def commit(self):
        self.commit_count += 1

    def close(self):
        self.closed = True


class _FakeTaskEngine:
    def __init__(self, tasks=()):
        self.tasks = {task.id: task for task in tasks}
        self.queue = asyncio.Queue()
        self.processing = set()
        self.submitted = []

    def get_all_tasks(self, include_hidden=False):
        del include_hidden
        return sorted(self.tasks.values(), key=lambda task: task.created_at, reverse=True)

    async def submit(self, task):
        self.tasks[task.id] = task
        self.submitted.append(task)
        await self.queue.put(task)
        return task.id


def _reprocess_archive_record(archive_path, *, status="completed", task_id=""):
    return ProcessedArchive(
        id="archive-reprocess",
        original_path=str(archive_path),
        current_path=str(archive_path),
        filename=os.path.basename(str(archive_path)),
        rjcode="RJ00012345",
        file_size=7,
        processed_at=datetime.now(),
        process_count=1,
        task_id=task_id,
        status=status,
    )


def _patch_reprocess_dependencies(monkeypatch, archive, engine):
    db = _FakeArchiveDB(archive)

    def fake_get_db():
        yield db

    monkeypatch.setattr(database_module, "get_db", fake_get_db)
    monkeypatch.setattr(routes_module, "get_task_engine", lambda: engine)
    monkeypatch.setattr(
        routes_module,
        "get_config",
        lambda: SimpleNamespace(watcher=SimpleNamespace(auto_classify=True)),
    )
    return db


@pytest.mark.asyncio
async def test_reprocessed_archive_status_follows_task_completion(tmp_path, monkeypatch):
    archive_path = tmp_path / "RJ00012345.zip"
    archive_path.write_bytes(b"payload")
    task = Task(
        task_type=TaskType.CIRCLE_COMPLETION_DOWNLOAD_BATCH,
        task_id="reprocess-task",
        source_path=str(archive_path),
        skip_archive=True,
        rjcode="RJ00012345",
    )
    archive = _reprocess_archive_record(
        archive_path,
        status="reprocessing",
        task_id=task.id,
    )
    archive.process_count = 2
    engine = TaskEngine()
    db = _patch_reprocess_dependencies(monkeypatch, archive, engine)
    monkeypatch.setattr(engine, "_notify_progress", AsyncMock())
    monkeypatch.setattr(engine, "_resolve_retry_extract_conflict", Mock())
    monkeypatch.setattr(engine, "_resolve_completed_failure_followups", Mock())
    monkeypatch.setattr(engine, "_finalize_conflict_resolution_task", Mock())
    monkeypatch.setattr(engine, "_cleanup_task_temp_extract_path", AsyncMock())
    monkeypatch.setattr(engine, "_cleanup_failed_task", AsyncMock())
    monkeypatch.setattr(engine, "persist_task_snapshot", Mock())
    monkeypatch.setattr(engine, "persist_task_center_item_snapshot", Mock())
    monkeypatch.setattr("app.core.activity_log_service.log_task_lifecycle_event", Mock())
    monkeypatch.setattr("app.core.task_notification_service.enqueue_notification_check", AsyncMock())
    broadcast_archive_changed = Mock()
    monkeypatch.setattr(
        "app.core.task_center_event_service.broadcast_processed_archive_changed",
        broadcast_archive_changed,
    )

    await engine._process_task(task)

    assert task.status == TaskStatus.COMPLETED
    assert archive.status == "completed"
    assert archive.process_count == 2
    assert db.commit_count == 1
    assert db.closed is True
    broadcast_archive_changed.assert_called_once_with(archive)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [TaskStatus.FAILED, TaskStatus.CANCELLED])
async def test_reprocessed_archive_status_follows_task_failure(tmp_path, monkeypatch, terminal_status):
    archive_path = tmp_path / "RJ00012345.zip"
    archive_path.write_bytes(b"payload")
    task = Task(
        task_type=TaskType.AUTO_PROCESS,
        task_id="reprocess-terminal-task",
        source_path=str(archive_path),
        skip_archive=True,
        status=terminal_status,
    )
    archive = _reprocess_archive_record(
        archive_path,
        status="reprocessing",
        task_id=task.id,
    )
    engine = TaskEngine()
    db = _patch_reprocess_dependencies(monkeypatch, archive, engine)

    engine._finalize_reprocessed_archive(task)

    assert archive.status == "failed"
    assert db.commit_count == 1
    assert db.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("active_status", [TaskStatus.WAITING_RETRY, TaskStatus.WAITING_MANUAL])
async def test_reprocessed_archive_status_waits_for_terminal_task(tmp_path, monkeypatch, active_status):
    task = Task(
        task_type=TaskType.AUTO_PROCESS,
        task_id="reprocess-active-task",
        source_path=str(tmp_path / "RJ00012345.zip"),
        skip_archive=True,
        status=active_status,
    )
    archive = _reprocess_archive_record(
        tmp_path / "RJ00012345.zip",
        status="reprocessing",
        task_id=task.id,
    )
    engine = TaskEngine()
    db = _patch_reprocess_dependencies(monkeypatch, archive, engine)

    engine._finalize_reprocessed_archive(task)

    assert archive.status == "reprocessing"
    assert db.commit_count == 0
    assert db.closed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_status", ["completed", "failed"])
async def test_reprocess_archive_reuses_terminal_auto_task_without_old_output(
    tmp_path,
    monkeypatch,
    archive_status,
):
    archive_path = tmp_path / "RJ00012345.zip"
    archive_path.write_bytes(b"payload")
    old_output = tmp_path / "deleted-library-output"
    task = Task(
        task_type=TaskType.AUTO_PROCESS,
        task_id="auto-task",
        source_path=str(tmp_path / "old-source.zip"),
        output_path=str(old_output),
        status=TaskStatus.COMPLETED,
        rjcode="RJ00012345",
        metadata={
            "target_library_id": "library-a",
            "final_file_tree_items": [{"name": "old.wav"}],
            "extracted_file_tree_items": [{"name": "old-extracted.wav"}],
            "file_tree_items": [{"name": "old-tree.wav"}],
            "filtered_items": [{"name": "old-filtered.wav"}],
            "filtered_files": ["old-filtered.wav"],
            "filtered_dirs": ["old-dir"],
            "filtered_count": 1,
            "filtered_size": 7,
            "filter_recovery": {"version": 1},
            "filter_path_transforms": [{"removed_segment": "old"}],
            "file_dedup_removed": 1,
            "skip_retry_precheck": True,
            "retry_from_conflicts": True,
            "retry_conflict_id": "conflict-old",
            "conflict_resolution_conflict_id": "conflict-old",
            "conflict_resolution_action": "RETRY",
            "manual_retry_password_requested": True,
            "skip_duplicate_precheck": True,
            "progress_log": [{"message": "old"}],
            "temp_extract_path": str(tmp_path / "old-temp"),
            "hidden_in_task_lists": True,
            "superseded_by_task_id": "other-task",
        },
    )
    task.progress = 100
    task.error_message = "old error"
    task.completed_at = datetime.now()
    archive = _reprocess_archive_record(
        archive_path,
        status=archive_status,
        task_id=task.id,
    )
    engine = _FakeTaskEngine([task])
    db = _patch_reprocess_dependencies(monkeypatch, archive, engine)

    result = await routes_module.reprocess_archive(archive.id)

    assert result["task_id"] == task.id
    assert task.type == TaskType.AUTO_PROCESS
    assert task.source_path == str(archive_path)
    assert task.output_path is None
    assert task.skip_archive is True
    assert task.status == TaskStatus.PENDING
    assert task.progress == 0
    assert task.current_step == "等待重新解压"
    assert task.error_message is None
    assert task.started_at is None
    assert task.completed_at is None
    assert engine.queue.qsize() == 1
    assert not old_output.exists()
    assert task.task_metadata["target_library_id"] == "library-a"
    for key in (
        "final_file_tree_items",
        "extracted_file_tree_items",
        "file_tree_items",
        "filtered_items",
        "filtered_files",
        "filtered_dirs",
        "filtered_count",
        "filtered_size",
        "filter_recovery",
        "filter_path_transforms",
        "file_dedup_removed",
        "skip_retry_precheck",
        "retry_from_conflicts",
        "retry_conflict_id",
        "conflict_resolution_conflict_id",
        "conflict_resolution_action",
        "manual_retry_password_requested",
        "skip_duplicate_precheck",
        "progress_log",
        "temp_extract_path",
        "hidden_in_task_lists",
        "superseded_by_task_id",
    ):
        assert key not in task.task_metadata
    assert archive.status == "reprocessing"
    assert archive.process_count == 2
    assert archive.task_id == task.id
    assert db.commit_count == 1
    assert db.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active_status",
    [
        TaskStatus.PENDING,
        TaskStatus.PROCESSING,
        TaskStatus.PAUSED,
        TaskStatus.WAITING_RETRY,
    ],
)
async def test_reprocess_archive_rejects_active_rj_task(
    tmp_path,
    monkeypatch,
    active_status,
):
    archive_path = tmp_path / "RJ00012345.zip"
    archive_path.write_bytes(b"payload")
    original_source = str(tmp_path / "active-source.zip")
    task = Task(
        task_type=TaskType.AUTO_PROCESS,
        task_id="active-task",
        source_path=original_source,
        status=active_status,
        rjcode="RJ00012345",
    )
    archive = _reprocess_archive_record(archive_path, task_id=task.id)
    engine = _FakeTaskEngine([task])
    db = _patch_reprocess_dependencies(monkeypatch, archive, engine)

    with pytest.raises(HTTPException) as exc_info:
        await routes_module.reprocess_archive(archive.id)

    assert exc_info.value.status_code == 409
    assert engine.queue.qsize() == 0
    assert task.source_path == original_source
    assert task.status == active_status
    assert archive.status == "completed"
    assert archive.process_count == 1
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_reprocess_archive_does_not_reuse_other_task_type(tmp_path, monkeypatch):
    archive_path = tmp_path / "RJ00012345.zip"
    archive_path.write_bytes(b"payload")
    subtitle_task = Task(
        task_type=TaskType.RJ_SUBTITLE_FETCH,
        task_id="subtitle-task",
        source_path=str(tmp_path / "subtitle-source"),
        status=TaskStatus.COMPLETED,
        rjcode="RJ00012345",
    )
    archive = _reprocess_archive_record(archive_path, task_id=subtitle_task.id)
    engine = _FakeTaskEngine([subtitle_task])
    _patch_reprocess_dependencies(monkeypatch, archive, engine)

    result = await routes_module.reprocess_archive(archive.id)

    assert result["task_id"] != subtitle_task.id
    assert len(engine.submitted) == 1
    new_task = engine.submitted[0]
    assert new_task.type == TaskType.AUTO_PROCESS
    assert new_task.source_path == str(archive_path)
    assert new_task.skip_archive is True
    assert subtitle_task.status == TaskStatus.COMPLETED
    assert archive.task_id == new_task.id
    assert engine.queue.qsize() == 1


@pytest.mark.asyncio
async def test_reprocess_archive_rejects_reprocessing_record(tmp_path, monkeypatch):
    archive_path = tmp_path / "RJ00012345.zip"
    archive_path.write_bytes(b"payload")
    archive = _reprocess_archive_record(archive_path, status="reprocessing")
    engine = _FakeTaskEngine()
    db = _patch_reprocess_dependencies(monkeypatch, archive, engine)

    with pytest.raises(HTTPException) as exc_info:
        await routes_module.reprocess_archive(archive.id)

    assert exc_info.value.status_code == 409
    assert engine.queue.qsize() == 0
    assert archive.process_count == 1
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_reprocess_archive_returns_404_when_archive_file_is_missing(
    tmp_path,
    monkeypatch,
):
    archive = _reprocess_archive_record(tmp_path / "missing.zip")
    engine = _FakeTaskEngine()
    db = _patch_reprocess_dependencies(monkeypatch, archive, engine)

    with pytest.raises(HTTPException) as exc_info:
        await routes_module.reprocess_archive(archive.id)

    assert exc_info.value.status_code == 404
    assert engine.queue.qsize() == 0
    assert archive.status == "completed"
    assert db.commit_count == 0
