import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_sqlite_json(_type, _compiler, **_kwargs):
    return "JSON"

from app.core.task_center_service import TaskCenterService
from app.core.task_engine import Task, TaskEngine, TaskStatus, TaskType
from app.models.database import Base, Task as TaskRecord, TaskCenterItem


@pytest.mark.asyncio
async def test_task_center_cache_uses_engine_version_without_rescanning_tasks(monkeypatch):
    service = TaskCenterService()
    task = Task(TaskType.EXTRACT, "/tmp/work.zip", task_id="task-1")
    task.created_at = datetime(2026, 1, 1)

    snapshot = Mock(return_value=[task])
    monkeypatch.setattr(service, "_engine_tasks_snapshot", snapshot)
    monkeypatch.setattr(service, "_engine_change_version", lambda: 7)
    monkeypatch.setattr(service, "_get_pending_items_cached", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_get_waiting_retry_items_cached", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_get_active_conflicts_cached", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service,
        "_safe_serialize_engine_task",
        lambda _task, mode="detail": {
            "id": f"engine:{_task.id}",
            "engine_task_id": _task.id,
            "domain": "system",
            "status": _task.status.value,
            "title": "测试任务",
            "subtitle": "",
            "progress": _task.progress,
            "current_step": _task.current_step,
            "created_at": _task.created_at.isoformat(),
            "updated_at": _task.created_at.isoformat(),
            "metrics": [],
            "actions": [],
            "details": {"metadata": {}},
        },
    )

    first = await service.list_items(mode="summary")
    second = await service.list_items(mode="summary")

    assert first["total"] == 1
    assert second["total"] == 1
    assert snapshot.call_count == 1


def test_task_engine_task_center_version_increments_on_task_event(monkeypatch):
    engine = TaskEngine()
    task = Task(TaskType.EXTRACT, "/tmp/work.zip", task_id="task-version")
    engine.tasks[task.id] = task
    engine._ensure_task_context(task)
    monkeypatch.setattr(engine, "enqueue_task_center_item_snapshot", Mock())
    monkeypatch.setattr(engine, "_write_task_runtime_to_redis", Mock())
    monkeypatch.setattr("app.core.task_center_event_service.broadcast_task_center_changed", Mock())

    before = engine.get_task_center_version()
    task.start()

    assert engine.get_task_center_version() == before + 1


def test_task_center_dedupes_waiting_manual_retry_chain():
    service = TaskCenterService()
    old_item = {
        "id": "engine:old-task",
        "entity_id": "old-task",
        "engine_task_id": "old-task",
        "status": TaskStatus.WAITING_MANUAL.value,
        "source_path": "/input/RJ01652675.rar",
        "created_at": "2026-08-04T00:33:11",
        "updated_at": "2026-08-04T00:35:58",
        "details": {"metadata": {}},
    }
    retry_item = {
        "id": "engine:retry-task",
        "entity_id": "retry-task",
        "engine_task_id": "retry-task",
        "status": TaskStatus.WAITING_MANUAL.value,
        "source_path": "/input/RJ01652675.rar",
        "created_at": "2026-08-04T00:41:44",
        "updated_at": "2026-08-04T00:41:50",
        "details": {"metadata": {"retry_failed_task_id": "old-task"}},
    }

    deduped = service._dedupe_items([old_item, retry_item])

    assert [item["entity_id"] for item in deduped] == ["retry-task"]


def test_task_center_materialized_summary_dedupes_retry_chain(monkeypatch):
    import app.core.task_center_materialization_service as materialization_module

    old_item = {
        "id": "engine:old-task",
        "entity_id": "old-task",
        "engine_task_id": "old-task",
        "domain": "system",
        "status": TaskStatus.WAITING_MANUAL.value,
        "source_path": "/input/RJ01652675.rar",
        "created_at": "2026-08-04T00:33:11",
        "updated_at": "2026-08-04T00:35:58",
        "details": {"metadata": {}},
    }
    retry_item = {
        "id": "engine:retry-task",
        "entity_id": "retry-task",
        "engine_task_id": "retry-task",
        "domain": "system",
        "status": TaskStatus.WAITING_MANUAL.value,
        "source_path": "/input/RJ01652675.rar",
        "created_at": "2026-08-04T00:41:44",
        "updated_at": "2026-08-04T00:41:50",
        "details": {"metadata": {"retry_failed_task_id": "old-task"}},
    }
    materialized_service = Mock()
    materialized_service.list_items.return_value = {
        "items": [old_item, retry_item],
        "total": 2,
    }
    monkeypatch.setattr(
        materialization_module,
        "get_task_center_materialization_service",
        lambda: materialized_service,
    )

    result = TaskCenterService().list_materialized_items(limit=10)

    assert [item["entity_id"] for item in result["items"]] == ["retry-task"]
    assert result["total"] == 1
    assert result["counts_by_status"][TaskStatus.WAITING_MANUAL.value] == 1
    assert result["highlight_counts"]["waiting_manual"] == 1


def test_task_center_materialized_item_snapshot_skips_unchanged_payload():
    engine = TaskEngine()
    task = Task(TaskType.EXTRACT, "/tmp/work.zip", task_id="task-materialized-version")
    item = {
        "id": f"engine:{task.id}",
        "engine_task_id": task.id,
        "status": TaskStatus.PROCESSING.value,
        "title": "解压任务",
        "progress": 10,
    }

    assert engine._should_upsert_task_center_item_snapshot(task, item) is True

    engine._materialized_task_center_item_versions[item["id"]] = engine._task_metadata_fingerprint(item)

    assert engine._should_upsert_task_center_item_snapshot(task, item) is False

    changed = {**item, "progress": 11}
    assert engine._should_upsert_task_center_item_snapshot(task, changed) is True


def test_task_center_materialized_item_snapshot_keeps_terminal_payload():
    engine = TaskEngine()
    task = Task(TaskType.EXTRACT, "/tmp/work.zip", task_id="task-materialized-terminal")
    task.complete()
    item = {
        "id": f"engine:{task.id}",
        "engine_task_id": task.id,
        "status": TaskStatus.COMPLETED.value,
        "title": "解压完成",
        "progress": 100,
    }
    engine._materialized_task_center_item_versions[item["id"]] = engine._task_metadata_fingerprint(item)

    assert engine._should_upsert_task_center_item_snapshot(task, item) is True


def test_http_download_task_subtitle_uses_current_file_name_without_rj():
    service = TaskCenterService()
    task = Task(
        TaskType.HTTP_DOWNLOAD,
        "https://gofile.io/d/content-id",
        task_id="task-gofile-title",
        metadata={
            "download_mode": "gofile",
            "source_modes": ["gofile"],
            "source_page": "asmr-sync",
            "source_action": "manual_gofile_download",
            "source_label": "Gofile 下载",
            "download_files": [
                {
                    "name": "RJ01621622.zip",
                    "relative_path": "RJ01621622.zip",
                    "status": "downloading",
                    "total": 3983571968,
                }
            ],
            "download_runtime": {
                "current_file_name": "RJ01621622.zip",
                "total_files": 1,
                "completed_files": 0,
                "total_bytes": 3983571968,
            },
        },
    )
    task.status = TaskStatus.PROCESSING

    item = service._safe_serialize_engine_task(task, mode="summary")

    assert item["domain"] == "http_download"
    assert item["title"] == "RJ01621622.zip"
    assert item["subtitle"] == "RJ01621622.zip"
    assert item["rjcode"] == ""


def test_summary_engine_item_cache_reuses_unchanged_task(monkeypatch):
    service = TaskCenterService()
    task = Task(TaskType.EXTRACT, "/tmp/work.zip", task_id="task-summary-cache")
    serialize = Mock(return_value={"id": f"engine:{task.id}", "status": task.status.value})
    monkeypatch.setattr(service, "_safe_serialize_engine_task", serialize)

    first = service._serialize_engine_task_cached(task, mode="summary")
    second = service._serialize_engine_task_cached(task, mode="summary")

    assert first == second
    assert serialize.call_count == 1

    task.update_progress(10, "解压中")
    third = service._serialize_engine_task_cached(task, mode="summary")

    assert third == first
    assert serialize.call_count == 2


def test_task_center_detail_paginates_large_download_rows_without_mutating_task():
    service = TaskCenterService()
    rows = [{"name": f"file-{index}.wav", "relative_path": f"dir/file-{index}.wav"} for index in range(3478)]
    task = Task(
        TaskType.ASMR_SYNC_DOWNLOAD,
        "/tmp/source",
        task_id="task-large-download-detail",
        metadata={"download_files": rows, "rjcode": "RJ01571862"},
    )

    item = service._serialize_engine_task(task, mode="detail")
    metadata = item["details"]["metadata"]

    assert len(metadata["download_files"]) == service.DETAIL_ARRAY_PREVIEW_LIMIT
    assert metadata["download_files_total"] == 3478
    assert metadata["download_files_truncated"] is True
    assert len(task.task_metadata["download_files"]) == 3478


@pytest.mark.asyncio
async def test_task_center_item_files_reads_paginated_file_tree_field(monkeypatch):
    service = TaskCenterService()
    rows = [{"name": f"dir-{index}", "relative_path": f"dir-{index}", "type": "dir"} for index in range(1400)]
    task = Task(
        TaskType.EXTRACT,
        "/tmp/source.zip",
        task_id="task-large-file-tree",
        metadata={"final_file_tree_items": rows},
    )
    engine = Mock()
    engine.get_task.return_value = task
    monkeypatch.setattr("app.core.task_center_service.get_task_engine", lambda: engine)

    detail = service._serialize_engine_task(task, mode="detail")
    detail_metadata = detail["details"]["metadata"]
    assert len(detail_metadata["final_file_tree_items"]) == service.DETAIL_ARRAY_PREVIEW_LIMIT
    assert detail_metadata["final_file_tree_items_total"] == 1400
    assert detail_metadata["final_file_tree_items_truncated"] is True

    result = await service.get_item_files(
        item_id="engine:task-large-file-tree",
        field="final_file_tree_items",
        offset=600,
        limit=120,
    )

    assert result["field"] == "final_file_tree_items"
    assert result["offset"] == 600
    assert result["total"] == 1400
    assert len(result["items"]) == 120
    assert result["items"][0]["relative_path"] == "dir-600"


@pytest.mark.asyncio
async def test_task_center_get_item_reads_single_engine_task(monkeypatch):
    service = TaskCenterService()
    task = Task(TaskType.ASMR_SYNC_DOWNLOAD, "/tmp/source", task_id="task-direct-detail")
    engine = Mock()
    engine.get_task.return_value = task
    monkeypatch.setattr("app.core.task_center_service.get_task_engine", lambda: engine)
    build_all = AsyncMock(side_effect=AssertionError("不应重建所有任务详情"))
    monkeypatch.setattr(service, "_build_all_items", build_all)

    result = await service.get_item(item_id="engine:task-direct-detail")

    assert result["engine_task_id"] == "task-direct-detail"
    build_all.assert_not_awaited()


def test_completed_task_detail_prefers_final_file_tree(monkeypatch, tmp_path):
    extracted = tmp_path / "RJ01645332_1"
    final = tmp_path / "巨乳大好き屋" / "[巨乳大好き屋][RJ01645332]"
    extracted.mkdir()
    final.mkdir(parents=True)
    (extracted / "临时.txt").write_text("staging", encoding="utf-8")
    (final / "最终.wav").write_bytes(b"final")

    task = Task(
        TaskType.EXTRACT,
        str(tmp_path / "RJ01645332.zip"),
        task_id="task-final-tree",
        metadata={
            "file_tree_items": [
                {
                    "relative_path": "临时.txt",
                    "name": "临时.txt",
                    "type": "file",
                    "size": 7,
                }
            ],
            "file_tree_root_path": str(extracted),
            "file_tree_root_label": extracted.name,
            "final_output_path": str(final),
        },
    )
    task.complete()

    item = TaskCenterService()._serialize_engine_task(task, mode="detail")
    metadata = item["details"]["metadata"]

    assert metadata["file_tree_view_kind"] == "final"
    assert metadata["extracted_file_tree_root_path"] == str(extracted)
    assert metadata["extracted_file_tree_items"][0]["name"] == "临时.txt"
    assert metadata["final_file_tree_root_path"] == str(final)
    assert metadata["final_file_tree_items"][0]["name"] == "最终.wav"


def test_completed_task_detail_marks_legacy_tree_as_extracted_snapshot(tmp_path):
    missing_final = tmp_path / "missing-final"
    task = Task(
        TaskType.EXTRACT,
        str(tmp_path / "RJ000000.zip"),
        task_id="task-legacy-tree",
        metadata={
            "file_tree_items": [{"relative_path": "旧文件.txt", "name": "旧文件.txt", "type": "file"}],
            "file_tree_root_path": str(tmp_path / "RJ000000_1"),
            "file_tree_root_label": "RJ000000_1",
            "final_output_path": str(missing_final),
        },
    )
    task.complete()

    metadata = TaskCenterService()._serialize_engine_task(task, mode="detail")["details"]["metadata"]

    assert metadata["file_tree_view_kind"] == "extracted_snapshot"
    assert metadata["extracted_file_tree_items"][0]["name"] == "旧文件.txt"
    assert not metadata.get("final_file_tree_items")


def test_summary_serialization_never_scans_file_tree(monkeypatch, tmp_path):
    final = tmp_path / "RJ01645332"
    final.mkdir()
    task = Task(
        TaskType.EXTRACT,
        str(tmp_path / "RJ01645332.zip"),
        task_id="task-summary-no-walk",
        metadata={"final_output_path": str(final)},
    )
    service = TaskCenterService()
    monkeypatch.setattr(
        service,
        "_snapshot_directory_items",
        Mock(side_effect=AssertionError("summary 不应扫描目录")),
    )

    service._serialize_engine_task(task, mode="summary")


def test_task_snapshot_materializes_task_center_item(monkeypatch):
    engine_db = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine_db)
    Base.metadata.create_all(bind=engine_db)

    import app.models.database as database_module
    import app.core.task_center_materialization_service as materialization_module

    monkeypatch.setattr(database_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(materialization_module, "SessionLocal", TestingSessionLocal)

    task_engine = TaskEngine()
    task = Task(
        TaskType.HTTP_DOWNLOAD,
        "https://example.test/file.zip",
        task_id="task-materialized",
        metadata={
            "source_page": "asmr-sync",
            "source_action": "manual_http_download",
            "source_label": "HTTP 下载",
            "download_files": [{"name": "file.zip", "status": "completed", "size": 1024}],
            "business_key": "http:file.zip",
        },
    )
    task.created_at = datetime(2026, 1, 1, 8, 0, 0)
    task.update_progress(42, "下载中")

    task_engine.persist_task_snapshot(task)
    expected = TaskCenterService()._safe_serialize_engine_task(task, mode="summary")

    db = TestingSessionLocal()
    try:
        row = db.query(TaskCenterItem).filter(TaskCenterItem.engine_task_id == task.id).first()
        assert row is not None
        assert row.item_id == f"engine:{task.id}"
        assert row.domain == "http_download"
        assert row.status == expected["status"]
        assert row.business_key == "http:file.zip"
        assert row.payload_json == expected
    finally:
        db.close()

    materialized = materialization_module.get_task_center_materialization_service()
    assert materialized.diff_engine_item(expected) == {
        "matched": True,
        "engine_task_id": task.id,
        "missing": False,
        "changed_keys": [],
    }

    db = TestingSessionLocal()
    try:
        row = db.query(TaskCenterItem).filter(TaskCenterItem.engine_task_id == task.id).first()
        row.payload_json = {**dict(row.payload_json or {}), "status": "failed"}
        db.commit()
    finally:
        db.close()

    diff = materialized.diff_engine_item(expected)
    assert diff["matched"] is False
    assert diff["missing"] is False
    assert diff["changed_keys"] == ["status"]

    task_engine.delete_task_snapshot(task.id)
    db = TestingSessionLocal()
    try:
        assert db.query(TaskCenterItem).filter(TaskCenterItem.engine_task_id == task.id).count() == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_task_center_backfill_materialized_items_supports_sql_listing(monkeypatch):
    engine_db = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine_db)
    Base.metadata.create_all(bind=engine_db)

    import app.models.database as database_module
    import app.core.task_center_materialization_service as materialization_module

    monkeypatch.setattr(database_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(materialization_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(materialization_module, "_task_center_materialization_service", None)

    task = Task(
        TaskType.HTTP_DOWNLOAD,
        "https://example.test/file.zip",
        task_id="task-backfill-http",
        metadata={
            "source_page": "asmr-sync",
            "source_action": "manual_http_download",
            "source_label": "HTTP 下载",
            "download_files": [{"name": "file.zip", "status": "completed", "size": 1024}],
            "business_key": "http:file.zip",
        },
    )
    task.created_at = datetime(2026, 1, 1, 8, 0, 0)
    task.start()

    service = TaskCenterService()
    monkeypatch.setattr(service, "_engine_tasks_snapshot", Mock(return_value=[task]))
    monkeypatch.setattr(service, "_engine_change_version", lambda: 11)
    monkeypatch.setattr(service, "_get_pending_items_cached", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_get_waiting_retry_items_cached", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_get_active_conflicts_cached", AsyncMock(return_value=[]))

    result = await service.backfill_materialized_items()
    assert result["matched"] is True
    assert result["item_count"] == 1
    assert result["engine_item_count"] == 1
    assert result["upserted"] == 1

    listed = service.list_materialized_engine_items(
        domain="http_download",
        status=TaskStatus.PROCESSING.value,
        search="file.zip",
        limit=10,
        offset=0,
    )

    assert listed["total"] == 1
    assert listed["items"][0]["id"] == f"engine:{task.id}"
    assert listed["counts_by_domain"]["http_download"] == 1
    assert sum(listed["counts_by_domain"].values()) == 1
    assert listed["counts_by_status"][TaskStatus.PROCESSING.value] == 1
    assert sum(listed["counts_by_status"].values()) == 1
    assert listed["highlight_counts"]["processing"] == 1


@pytest.mark.asyncio
async def test_task_center_materialized_summary_can_read_all_item_kinds(monkeypatch):
    engine_db = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine_db)
    Base.metadata.create_all(bind=engine_db)

    import app.models.database as database_module
    import app.core.task_center_materialization_service as materialization_module

    monkeypatch.setattr(database_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(materialization_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(materialization_module, "_task_center_materialization_service", None)

    service = TaskCenterService()
    task = Task(TaskType.EXTRACT, "/tmp/work.zip", task_id="task-all-kinds")
    task.created_at = datetime(2026, 1, 1, 8, 0, 0)
    pending_item = {
        "id": "pending-1",
        "task_id": "",
        "source_path": "/tmp/subtitle.zip",
        "source_mode": "archive",
        "can_execute": True,
        "created_at": "2026-01-01T08:01:00",
        "preview": {
            "source_label": "字幕补配预检",
            "source_rjcode": "RJ111111",
            "target_rjcode": "RJ222222",
            "candidate_count": 1,
            "ready_candidate_count": 1,
            "execute_reason": "等待确认",
        },
    }

    monkeypatch.setattr(service, "_engine_tasks_snapshot", Mock(return_value=[task]))
    monkeypatch.setattr(service, "_engine_change_version", lambda: 12)
    monkeypatch.setattr(service, "_get_pending_items_cached", AsyncMock(return_value=[pending_item]))
    monkeypatch.setattr(service, "_get_waiting_retry_items_cached", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_get_active_conflicts_cached", AsyncMock(return_value=[]))

    backfill = await service.backfill_materialized_items()
    assert backfill["matched"] is True
    assert backfill["item_count"] == 2
    assert backfill["upserted"] == 2

    monkeypatch.setenv("KIKOERUMANAGER_TASK_CENTER_MATERIALIZED_SUMMARY", "1")
    monkeypatch.setattr(service, "_build_all_items", AsyncMock(side_effect=AssertionError("不应走旧聚合")))

    result = await service.list_items(mode="summary", limit=10)

    assert result["mode"] == "materialized_summary"
    assert result["total"] == 2
    assert {item["id"] for item in result["items"]} == {"engine:task-all-kinds", "subtitle-pending:pending-1"}
    assert result["counts_by_domain"]["system"] == 1
    assert result["counts_by_domain"]["subtitle_import"] == 1


def _use_empty_non_engine_task_sources(monkeypatch, service):
    monkeypatch.setattr(service, "_get_pending_items_cached", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_get_waiting_retry_items_cached", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_get_active_conflicts_cached", AsyncMock(return_value=[]))


@pytest.mark.asyncio
async def test_ai_title_terminal_task_survives_repeated_summary_queries(monkeypatch):
    engine = TaskEngine()
    task = Task(
        TaskType.AI_TITLE_TRANSLATION,
        "RJ01736954",
        task_id="task-ai-title-same-engine",
        metadata={"task_domain": "ai_title", "rjcode": "RJ01736954"},
        status=TaskStatus.COMPLETED,
    )
    task.completed_at = datetime(2026, 1, 1, 9, 0, 0)
    engine.tasks[task.id] = task
    service = TaskCenterService()
    monkeypatch.setattr("app.core.task_center_service.get_task_engine", lambda: engine)
    _use_empty_non_engine_task_sources(monkeypatch, service)

    first = await service.list_items(mode="summary", domain="ai_title", limit=10)
    second = await service.list_items(mode="summary", domain="ai_title", limit=10)

    assert first["total"] == 1
    assert second["total"] == 1
    assert second["items"][0]["engine_task_id"] == task.id


def _use_isolated_task_database(monkeypatch):
    engine_db = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine_db)
    Base.metadata.create_all(bind=engine_db)

    import app.models.database as database_module
    import app.core.task_center_materialization_service as materialization_module

    monkeypatch.setattr(database_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(materialization_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(materialization_module, "_task_center_materialization_service", None)
    return TestingSessionLocal


def _stub_task_engine_side_effects(monkeypatch, engine):
    monkeypatch.setattr(engine, "_notify_progress", AsyncMock())
    monkeypatch.setattr(engine, "enqueue_task_center_item_snapshot", Mock())
    monkeypatch.setattr(engine, "_write_task_runtime_to_redis", Mock())
    monkeypatch.setattr("app.core.task_center_event_service.broadcast_task_center_changed", Mock())
    monkeypatch.setattr(engine, "persist_task_center_item_snapshot", Mock())
    monkeypatch.setattr(engine, "_resolve_retry_extract_conflict", Mock())
    monkeypatch.setattr(engine, "_resolve_completed_failure_followups", Mock())
    monkeypatch.setattr(engine, "_finalize_conflict_resolution_task", Mock())
    monkeypatch.setattr(engine, "_cleanup_task_temp_extract_path", AsyncMock())
    monkeypatch.setattr(engine, "_cleanup_failed_task", AsyncMock())
    monkeypatch.setattr(engine, "_refresh_conflict_resolution_progress", Mock())
    monkeypatch.setattr(engine, "unmark_rjcode_processing", Mock())
    monkeypatch.setattr("app.core.activity_log_service.log_task_lifecycle_event", Mock())
    monkeypatch.setattr("app.core.task_notification_service.enqueue_notification_check", AsyncMock())


@pytest.mark.asyncio
async def test_pending_task_submit_persists_and_start_restores_queue(monkeypatch):
    """普通 pending 任务必须在重启后恢复内存列表和执行队列。"""
    TestingSessionLocal = _use_isolated_task_database(monkeypatch)
    engine = TaskEngine()
    task = Task(
        TaskType.HTTP_DOWNLOAD,
        "https://example.test/pending.zip",
        task_id="task-restart-pending",
        auto_classify=True,
        skip_archive=True,
    )

    await engine.submit(task)
    engine.stop()

    db = TestingSessionLocal()
    try:
        row = db.query(TaskRecord).filter(TaskRecord.id == task.id).one_or_none()
        assert row is not None
        assert row.status == TaskStatus.PENDING.value
    finally:
        db.close()

    restarted = TaskEngine()

    async def wait_forever():
        await asyncio.Event().wait()

    monkeypatch.setattr(restarted, "_worker", wait_forever)
    monkeypatch.setattr(restarted, "_retry_scheduler", wait_forever)
    restarted.start()
    try:
        restored = restarted.get_task(task.id)
        assert restored is not None
        assert restored.status == TaskStatus.PENDING
        assert restored.auto_classify is True
        assert restored.skip_archive is True
        assert restarted.queue.qsize() == 1
        assert restarted.load_persisted_queue_tasks() == 0
        assert restarted.queue.qsize() == 1

        service = TaskCenterService()
        monkeypatch.setattr("app.core.task_center_service.get_task_engine", lambda: restarted)
        _use_empty_non_engine_task_sources(monkeypatch, service)
        listed = await service.list_items(mode="summary", limit=10)
        assert listed["total"] == 1
        assert listed["items"][0]["engine_task_id"] == task.id
        assert listed["items"][0]["status"] == TaskStatus.PENDING.value
    finally:
        restarted.stop()
        await asyncio.gather(
            restarted._worker_task,
            restarted._retry_scheduler_task,
            return_exceptions=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_status", "expected_status", "expected_queued"),
    [
        (TaskStatus.PROCESSING, TaskStatus.PENDING, 1),
        (TaskStatus.PAUSED, TaskStatus.PAUSED, 0),
        (TaskStatus.WAITING_RETRY, TaskStatus.WAITING_RETRY, 0),
        (TaskStatus.WAITING_MANUAL, TaskStatus.WAITING_MANUAL, 0),
        (TaskStatus.FAILED, TaskStatus.FAILED, 0),
        (TaskStatus.COMPLETED, TaskStatus.COMPLETED, 0),
    ],
)
async def test_restart_restore_respects_task_status(
    monkeypatch,
    persisted_status,
    expected_status,
    expected_queued,
):
    """只有待处理或被中断的处理中任务可以自动重新入队。"""
    _use_isolated_task_database(monkeypatch)
    engine = TaskEngine()
    task = Task(
        TaskType.AUTO_PROCESS,
        f"/tmp/{persisted_status.value}.zip",
        task_id=f"task-restart-{persisted_status.value}",
    )
    await engine.submit(task)
    with task._set_state_silent():
        task.status = persisted_status
        if persisted_status == TaskStatus.PROCESSING:
            task.started_at = datetime.now()
        if persisted_status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            task.completed_at = datetime.now()
    task.mark_changed("status")
    engine.stop()

    restarted = TaskEngine()
    try:
        assert restarted.load_persisted_queue_tasks() == 1
        restored = restarted.get_task(task.id)
        assert restored.status == expected_status
        assert restarted.queue.qsize() == expected_queued
    finally:
        restarted.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted_status", [TaskStatus.PENDING, TaskStatus.PROCESSING])
async def test_restart_restore_shows_legacy_unmarked_task_without_executing(
    monkeypatch,
    persisted_status,
):
    """历史活动快照恢复为可见暂停任务，不能自动执行或伪装成运行中。"""
    TestingSessionLocal = _use_isolated_task_database(monkeypatch)
    task_id = f"task-legacy-unmarked-{persisted_status.value}"
    db = TestingSessionLocal()
    try:
        db.add(TaskRecord(
            id=task_id,
            type=TaskType.EXTRACT.value,
            status=persisted_status.value,
            source_path="/tmp/legacy.zip",
            task_metadata={},
        ))
        db.commit()
    finally:
        db.close()

    engine = TaskEngine()
    try:
        assert engine.load_persisted_queue_tasks() == 1
        restored = engine.get_task(task_id)
        assert restored is not None
        assert restored.status == TaskStatus.PAUSED
        assert restored.current_step == "历史任务已恢复，点击继续后执行"
        assert restored.task_metadata["legacy_restore_requires_manual_resume"] is True
        assert engine.queue.qsize() == 0

        service = TaskCenterService()
        monkeypatch.setattr("app.core.task_center_service.get_task_engine", lambda: engine)
        _use_empty_non_engine_task_sources(monkeypatch, service)
        listed = await service.list_items(mode="summary", limit=10)
        assert listed["total"] == 1
        assert listed["items"][0]["engine_task_id"] == task_id
        assert listed["items"][0]["status"] == TaskStatus.PAUSED.value
        assert listed["items"][0]["actions"] == ["resume", "cancel"]

        engine.resume_task(task_id)
        assert restored.status == TaskStatus.PENDING
        assert engine.queue.qsize() == 1
    finally:
        engine.stop()


@pytest.mark.asyncio
async def test_start_requeues_stale_processing_snapshot_marked_for_restore(monkeypatch):
    """启动清理与通用恢复必须串联，中断任务不能被留在等待重试状态。"""
    _use_isolated_task_database(monkeypatch)
    engine = TaskEngine()
    _stub_task_engine_side_effects(monkeypatch, engine)
    task = Task(
        TaskType.AUTO_PROCESS,
        "/tmp/stale-processing-restart.zip",
        task_id="task-restart-stale-processing",
    )
    await engine.submit(task)
    with task._set_state_silent():
        task.status = TaskStatus.PROCESSING
        task.started_at = datetime.now() - timedelta(hours=1)
    task.mark_changed("status")
    engine.stop()

    restarted = TaskEngine()
    restarted.stale_processing_seconds = 60

    async def wait_forever():
        await asyncio.Event().wait()

    monkeypatch.setattr(restarted, "_worker", wait_forever)
    monkeypatch.setattr(restarted, "_retry_scheduler", wait_forever)
    restarted.start()
    try:
        restored = restarted.get_task(task.id)
        assert restored.status == TaskStatus.PENDING
        assert restored.task_metadata["stale_processing_recovered"] is True
        assert restarted.queue.qsize() == 1
    finally:
        restarted.stop()
        await asyncio.gather(
            restarted._worker_task,
            restarted._retry_scheduler_task,
            return_exceptions=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        ("pause", TaskStatus.PAUSED),
        ("cancel", TaskStatus.CANCELLED),
    ],
)
async def test_restart_after_pause_or_cancel_does_not_requeue_task(
    monkeypatch,
    action,
    expected_status,
):
    """正常停止必须排空最后一次状态快照，不能复活旧的 pending 记录。"""
    TestingSessionLocal = _use_isolated_task_database(monkeypatch)
    engine = TaskEngine()
    _stub_task_engine_side_effects(monkeypatch, engine)
    task = Task(
        TaskType.AUTO_PROCESS,
        f"/tmp/{action}-before-restart.zip",
        task_id=f"task-restart-after-{action}",
    )
    await engine.submit(task)

    if action == "pause":
        engine.pause_task(task.id)
    else:
        engine.cancel_task(task.id)
    engine.stop()

    db = TestingSessionLocal()
    try:
        row = db.query(TaskRecord).filter(TaskRecord.id == task.id).one()
        assert row.status == expected_status.value
    finally:
        db.close()

    restarted = TaskEngine()
    try:
        assert restarted.load_persisted_queue_tasks() == 1
        restored = restarted.get_task(task.id)
        assert restored.status == expected_status
        assert restarted.queue.qsize() == 0
        if action == "pause":
            restarted.resume_task(task.id)
            assert restored.status == TaskStatus.PENDING
            assert restarted.queue.qsize() == 1
            restarted.stop()
            db = TestingSessionLocal()
            try:
                row = db.query(TaskRecord).filter(TaskRecord.id == task.id).one()
                assert row.status == TaskStatus.PENDING.value
            finally:
                db.close()
    finally:
        restarted.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("completed", TaskStatus.COMPLETED),
        ("failed", TaskStatus.FAILED),
        ("cancelled", TaskStatus.CANCELLED),
    ],
)
async def test_ai_title_production_terminal_persists_and_restores_after_restart(
    monkeypatch,
    outcome,
    expected_status,
):
    """终态记录必须由 _process_task 真实产生，测试不得手工预写 TaskRecord。"""
    TestingSessionLocal = _use_isolated_task_database(monkeypatch)
    engine = TaskEngine()
    _stub_task_engine_side_effects(monkeypatch, engine)
    task = Task(
        TaskType.AI_TITLE_TRANSLATION,
        "RJ01736954",
        task_id=f"task-ai-title-restart-{outcome}",
        metadata={
            "task_domain": "ai_title",
            "source_action": "ai_title_translation",
            "rjcode": "RJ01736954",
        },
    )
    engine.tasks[task.id] = task

    async def run_ai_title(current_task):
        current_task.task_metadata["ai_translation"] = [{
            "rjcode": "RJ01736954",
            "original_ai_title": None,
            "written_ai_title": "汉化标题",
            "write_status": "success",
            "rollback_status": "pending",
        }]
        current_task.task_metadata["renamed_files"] = []
        current_task.task_metadata["folder_renamed"] = []
        if outcome == "failed":
            raise RuntimeError("AI 汉化测试失败")
        if outcome == "cancelled":
            raise asyncio.CancelledError()

    monkeypatch.setattr(engine, "_process_ai_title_translation", run_ai_title)
    await engine._process_task(task)
    assert task.status == expected_status

    db = TestingSessionLocal()
    try:
        row = db.query(TaskRecord).filter(TaskRecord.id == task.id).one_or_none()
        assert row is not None
        assert row.status == expected_status.value
        assert row.task_metadata["ai_translation"][0]["written_ai_title"] == "汉化标题"
    finally:
        db.close()

    restarted_engine = TaskEngine()
    assert restarted_engine.load_persisted_ai_title_tasks() == 1
    restored = restarted_engine.get_task(task.id)
    assert restored.status == expected_status
    assert restored.task_metadata["source_action"] == "ai_title_translation"

    service = TaskCenterService()
    monkeypatch.setattr("app.core.task_center_service.get_task_engine", lambda: restarted_engine)
    _use_empty_non_engine_task_sources(monkeypatch, service)
    listed = await service.list_items(mode="summary", domain="ai_title", limit=10)
    detail = await service.get_item(item_id=f"engine:{task.id}")

    assert listed["total"] == 1
    assert listed["items"][0]["status"] == expected_status.value
    assert detail["engine_task_id"] == task.id


@pytest.mark.asyncio
async def test_ai_title_cancel_after_partial_changes_restores_complete_rollback_snapshot(
    monkeypatch,
    tmp_path,
):
    """首个作品已改标题/文件/目录后，第二个作品取消仍须可跨重启回退。"""
    import os

    import app.models.database as database_module

    TestingSessionLocal = _use_isolated_task_database(monkeypatch)
    engine = TaskEngine()
    _stub_task_engine_side_effects(monkeypatch, engine)
    first_dir = tmp_path / "RJ01000001 作品一"
    second_dir = tmp_path / "RJ01000002 作品二"
    first_dir.mkdir()
    second_dir.mkdir()
    task = Task(
        TaskType.AI_TITLE_TRANSLATION,
        "RJ01000001",
        task_id="task-ai-title-partial-cancel",
        metadata={
            "task_domain": "ai_title",
            "source_action": "ai_title_translation",
            "rjcode": "RJ01000001",
            "rjcodes": ["RJ01000001", "RJ01000002"],
            "library_id": "lib-a",
            "path": str(first_dir),
            "work_names": {
                "RJ01000001 作品一": "作品一",
                "RJ01000002 作品二": "作品二",
            },
        },
    )
    engine.tasks[task.id] = task

    work_names = {"RJ01000001": "作品一", "RJ01000002": "作品二"}
    ai_titles = {"RJ01000001": None, "RJ01000002": None}

    class FakeWorkSession:
        def execute(self, statement, params=None):
            sql = str(statement)
            params = params or {}
            rjcode = params.get("rjcode")
            if "SELECT work_name FROM work_metadata" in sql:
                return SimpleNamespace(fetchone=lambda: (work_names[rjcode],))
            if "SELECT ai_title, work_name, maker_id" in sql:
                return SimpleNamespace(fetchone=lambda: (
                    ai_titles[rjcode], work_names[rjcode], "", "", "", "", "", [], [],
                ))
            if "SELECT ai_title FROM work_metadata" in sql:
                return SimpleNamespace(fetchone=lambda: (ai_titles[rjcode],))
            if "UPDATE work_metadata SET ai_title" in sql:
                ai_titles[rjcode] = params["ai_title"]
                return SimpleNamespace(rowcount=1)
            raise AssertionError(sql)

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    def fake_get_db():
        yield FakeWorkSession()

    monkeypatch.setattr(database_module, "get_db", fake_get_db)
    monkeypatch.setattr(
        "app.config.settings.get_config",
        lambda: SimpleNamespace(ai_title_translation=SimpleNamespace(
            enabled=True,
            use_ai_subtitle_api=False,
        )),
    )
    monkeypatch.setattr(
        "app.api.routes._read_ai_title_translation_api_key_from_disk",
        lambda: "",
    )

    async def translate_batch(items, *_args, **_kwargs):
        results = []
        for item in items:
            original_title = str(item["work_name"]).split("\n", 1)[0]
            results.append({
                "success": True,
                "rjcode": item["rjcode"],
                "original_title": original_title,
                "translated_title": json.dumps({
                    original_title: f"{original_title}-汉化",
                    "01": f"{item['rjcode']}-音轨汉化",
                }, ensure_ascii=False),
            })
        return results

    monkeypatch.setattr(
        "app.core.ai_title_translation_service.get_ai_title_translation_service",
        lambda: SimpleNamespace(translate_batch=translate_batch),
    )

    class FakeRenameManager:
        def __init__(self):
            self.rename_calls = []

        def find_rj_in_ready_index(self, rjcode, **_kwargs):
            return {rjcode: [{"path": str(second_dir), "library_id": "lib-a"}]}

        async def folder_contents(self, path, **_kwargs):
            return {"items": [{
                "name": "01.wav",
                "path": os.path.join(path, "01.wav"),
                "is_directory": False,
            }]}

        async def rename(self, _library_id, path, new_name, **_kwargs):
            self.rename_calls.append((path, new_name))
            if len(self.rename_calls) == 3:
                raise asyncio.CancelledError()
            return {"new_path": os.path.join(os.path.dirname(path), new_name)}

    manager = FakeRenameManager()
    monkeypatch.setattr("app.core.library_manager.get_library_manager", lambda: manager)

    await engine._process_task(task)
    assert task.status == TaskStatus.CANCELLED
    assert len(manager.rename_calls) == 3
    db = TestingSessionLocal()
    try:
        row = db.query(TaskRecord).filter(TaskRecord.id == task.id).one_or_none()
        assert row is not None
        assert row.status == TaskStatus.CANCELLED.value
    finally:
        db.close()

    restarted_engine = TaskEngine()
    assert restarted_engine.load_persisted_ai_title_tasks() == 1
    restored = restarted_engine.get_task(task.id)
    assert restored.status == TaskStatus.CANCELLED
    assert len(restored.task_metadata["ai_translation"]) == 2
    assert len(restored.task_metadata["renamed_files"]) == 1
    assert len(restored.task_metadata["folder_renamed"]) == 1
    assert restored.task_metadata["renamed_count"] == 1
    assert restored.task_metadata["folder_renamed_count"] == 1
    assert restored.task_metadata["renamed_files"][0]["rollback_status"] == "pending"
    assert restored.task_metadata["folder_renamed"][0]["rollback_status"] == "pending"

    monkeypatch.setattr(restarted_engine, "persist_task_center_item_snapshot", Mock())
    monkeypatch.setattr("app.core.task_center_service.get_task_engine", lambda: restarted_engine)
    rollback = AsyncMock(return_value={"success": True, "restored_count": 4})
    monkeypatch.setattr(
        "app.core.ai_title_translation_service.get_ai_title_translation_service",
        lambda: SimpleNamespace(rollback_task_changes=rollback),
    )
    result = await TaskCenterService().execute_action(f"engine:{task.id}", "rollback_ai_title")

    assert result["success"] is True
    rollback.assert_awaited_once_with(restored)
    db = TestingSessionLocal()
    try:
        assert db.query(TaskRecord).filter(TaskRecord.id == task.id).one().status == TaskStatus.CANCELLED.value
    finally:
        db.close()


def test_ai_title_terminal_actions_include_rollback():
    service = TaskCenterService()
    task = Task(
        TaskType.AI_TITLE_TRANSLATION,
        "RJ01736954",
        metadata={"task_domain": "ai_title"},
        status=TaskStatus.FAILED,
    )

    assert service._build_engine_actions(task, "ai_title") == ["rollback_ai_title", "delete"]

    task.status = TaskStatus.PROCESSING
    assert "rollback_ai_title" not in service._build_engine_actions(task, "ai_title")


@pytest.mark.asyncio
async def test_ai_title_rollback_action_persists_updated_snapshot(monkeypatch):
    task = Task(
        TaskType.AI_TITLE_TRANSLATION,
        "RJ01736954",
        task_id="task-ai-rollback-action",
        metadata={"task_domain": "ai_title"},
        status=TaskStatus.FAILED,
    )
    engine = Mock()
    engine.get_task.return_value = task
    engine.persist_task_snapshot = Mock()
    monkeypatch.setattr("app.core.task_center_service.get_task_engine", lambda: engine)
    rollback = AsyncMock(return_value={
        "success": True,
        "message": "已安全回退 2 项 AI 汉化修改",
        "restored_count": 2,
    })
    monkeypatch.setattr(
        "app.core.ai_title_translation_service.get_ai_title_translation_service",
        lambda: Mock(rollback_task_changes=rollback),
    )

    result = await TaskCenterService().execute_action(f"engine:{task.id}", "rollback_ai_title")

    assert result["success"] is True
    assert result["restored_count"] == 2
    rollback.assert_awaited_once_with(task)
    engine.persist_task_snapshot.assert_called_once_with(task)


@pytest.mark.asyncio
async def test_dlsite_network_retry_reuses_original_engine_task(monkeypatch, tmp_path):
    output = tmp_path / "RJ01689866"
    output.mkdir()
    task = Task(
        TaskType.AUTO_PROCESS,
        str(tmp_path / "RJ01689866.7z"),
        output_path=str(output),
        task_id="task-dlsite-network-paused",
        status=TaskStatus.WAITING_MANUAL,
        metadata={
            "rjcode": "RJ01689866",
            "dlsite_network_paused": True,
            "resume_from_stage": "dlsite_network",
        },
        rjcode="RJ01689866",
    )
    service = TaskCenterService()
    assert service._build_engine_actions(task, "import") == ["retry"]

    retry = AsyncMock(return_value=task)
    engine = SimpleNamespace(
        get_task=Mock(return_value=task),
        retry_dlsite_network_paused_task=retry,
    )
    monkeypatch.setattr("app.core.task_center_service.get_task_engine", lambda: engine)
    file_processor = SimpleNamespace(
        process_file=AsyncMock(side_effect=AssertionError("DLsite 续跑不能新建导入任务"))
    )
    monkeypatch.setattr("app.core.file_processor.get_file_processor", lambda: file_processor)

    result = await service.execute_action(f"engine:{task.id}", "retry")

    assert result["success"] is True
    assert result["task_id"] == task.id
    retry.assert_awaited_once_with(task.id)
    file_processor.process_file.assert_not_awaited()
    assert task.output_path == str(output)
    assert output.is_dir()
