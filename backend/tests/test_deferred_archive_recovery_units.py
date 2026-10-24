"""延后归档孤儿判定与源变化异常的无数据库测试。"""

import asyncio

from app.core import deferred_archive_service as module


def _tail_claim(path):
    return {
        "job_id": "job",
        "lease_epoch": 1,
        "source_manifest": [
            {
                "source_path": str(path),
                "filename": path.name,
                "size": path.stat().st_size,
            }
        ],
    }


def test_stale_orphan_detected_when_first_volume_exists(tmp_path):
    tail = tmp_path / "archive.7z.002"
    first = tmp_path / "archive.7z.001"
    tail.write_bytes(b"tail")
    first.write_bytes(b"first")

    service = module.DeferredArchiveService()

    assert service._detect_stale_orphan_sync(_tail_claim(tail)) is True


def test_stale_orphan_not_detected_when_first_volume_missing(tmp_path):
    tail = tmp_path / "archive.7z.002"
    tail.write_bytes(b"tail")

    service = module.DeferredArchiveService()

    assert service._detect_stale_orphan_sync(_tail_claim(tail)) is False


def test_source_changed_exception_is_distinct_from_generic_failure():
    assert issubclass(module._ArchiveSourceChanged, RuntimeError)
    assert not issubclass(module._ArchiveSourceChanged, module._ArchiveCancelled)


def test_manifest_alignment_uses_first_volume_order(tmp_path):
    tail = tmp_path / "archive.7z.002"
    first = tmp_path / "archive.7z.001"
    sources = [
        {"source_path": str(tail), "filename": tail.name},
        {"source_path": str(first), "filename": first.name},
    ]
    targets = [
        {"source_path": str(tail), "target_path": str(tmp_path / "archive.7z.002")},
        {"source_path": str(first), "target_path": str(tmp_path / "archive.7z.001")},
    ]
    fresh = [
        {"source_path": str(first), "filename": first.name},
        {"source_path": str(tail), "filename": tail.name},
    ]

    aligned_sources, aligned_targets = module.DeferredArchiveService._align_manifest_to_fresh(
        sources, targets, fresh
    )

    assert [item["filename"] for item in aligned_sources] == [first.name, tail.name]
    assert [item["target_path"] for item in aligned_targets] == [
        str(tmp_path / "archive.7z.001"),
        str(tmp_path / "archive.7z.002"),
    ]


def test_execute_checks_stale_orphan_before_manifest_refresh(tmp_path, monkeypatch):
    tail = tmp_path / "archive.7z.002"
    tail.write_bytes(b"tail")
    service = module.DeferredArchiveService()
    events = []
    claim = {
        "job_id": "job",
        "lease_epoch": 1,
        "task_id": "",
        "source_manifest": [{"source_path": str(tail), "filename": tail.name}],
        "target_manifest": [],
    }

    async def _update_parent(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_update_parent_task", _update_parent)
    monkeypatch.setattr(service, "_broadcast", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_detect_stale_orphan_sync",
        lambda _claim: events.append("stale") or True,
    )
    monkeypatch.setattr(
        service,
        "_refresh_job_manifest_sync",
        lambda *_args, **_kwargs: events.append("refresh") or {},
    )
    monkeypatch.setattr(
        service,
        "_cancel_job_sync",
        lambda *_args, **_kwargs: events.append("cancel") or {
            "status": "cancelled",
            "source_manifest": claim["source_manifest"],
        },
    )
    monkeypatch.setattr(service, "_remove_claims", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_write_activity", lambda *_args, **_kwargs: None)

    asyncio.run(service._execute_claimed_job(claim))

    assert events == ["stale", "cancel"]


def test_manifest_alignment_keeps_original_when_members_do_not_match(tmp_path):
    tail = tmp_path / "archive.7z.002"
    first = tmp_path / "archive.7z.001"
    missing = tmp_path / "archive.7z.003"
    sources = [{"source_path": str(tail), "filename": tail.name}]
    targets = [{"source_path": str(tail), "target_path": str(tmp_path / "archive.7z.002")}]
    fresh = [
        {"source_path": str(first), "filename": first.name},
        {"source_path": str(missing), "filename": missing.name},
    ]

    aligned_sources, aligned_targets = module.DeferredArchiveService._align_manifest_to_fresh(
        sources, targets, fresh
    )

    assert aligned_sources == sources
    assert aligned_targets == targets
