"""降级元数据不得污染库存目录名。

用户场景：RJ01586348 解压时 DLsite 全部链路不可达（ConnectError / SSL EOF /
voicehub 超时），metadata_service 降级成 minimal，work_name 取自解压临时目录名
``RJ01586348_2_2(RJ01586348)_ba3822b2_jt_yoio4``，重命名模板把它拼成
``[RJ01586348][2_2(RJ01586348)_ba3822b2_jt_yoio4]`` 并搬进库存。

修复分三层：
1. ``_build_minimal_metadata`` 剥掉 mkdtemp 的 ``_<task_id8>_<随机串>`` 尾巴；
2. ``RenameService.resolve_target_name`` 对 minimal 元数据只保留 RJ 号，
   并清理模板空段、空名回退 RJ 号；
3. ``TaskEngine`` 断网时停在重命名之前，重试复用已解压产物。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import rename_service as rename_service_module
from app.core.metadata_service import MetadataService
from app.core.rename_service import RenameService
from app.core.task_engine import Task, TaskEngine, TaskStatus, TaskType


# 真实的解压临时目录名：源名主干 + task_id 前 8 位 + mkdtemp 随机串
TEMP_DIR_NAME = "RJ01586348_2_2(RJ01586348)_ba3822b2_jt_yoio4"


def _config(template: str, *, use_japanese_metadata: bool = False):
    return SimpleNamespace(
        rename=SimpleNamespace(
            template=template,
            use_japanese_metadata=use_japanese_metadata,
            date_format="%y%m%d",
            delimiter=" ",
            cv_list_left="(CV ",
            cv_list_right=")",
            tags_max_number=5,
            exclude_square_brackets=False,
            illegal_char_to_full_width=False,
        ),
    )


# ---------------------------------------------------------------------------
# 第 1 层：minimal 元数据不把临时目录名当作品名
# ---------------------------------------------------------------------------

def test_minimal_metadata_strips_extract_temp_suffix() -> None:
    metadata = MetadataService()._build_minimal_metadata("RJ01586348", f"/temp/{TEMP_DIR_NAME}")

    assert metadata.metadata_source == "minimal"
    assert "ba3822b2" not in metadata.work_name
    assert "jt_yoio4" not in metadata.work_name
    assert metadata.work_name == "2_2(RJ01586348)"


def test_minimal_metadata_keeps_normal_folder_name() -> None:
    metadata = MetadataService()._build_minimal_metadata("RJ01586348", "/temp/RJ01586348 夏の思い出")

    assert metadata.work_name == "夏の思い出"


def test_minimal_metadata_does_not_strip_plain_hex_suffix() -> None:
    # 没有随机串的普通目录名不能被误剥（正则要求 _<hex8>_<随机> 整体结尾）
    metadata = MetadataService()._build_minimal_metadata("RJ01586348", "/temp/RJ01586348_ba3822b2")

    assert metadata.work_name == "ba3822b2"


def test_minimal_metadata_empty_path_falls_back_to_rjcode() -> None:
    # normpath('') 得到 '.'，不能让它变成作品名
    metadata = MetadataService()._build_minimal_metadata("RJ01586348", "")

    assert metadata.work_name == "RJ01586348"


# ---------------------------------------------------------------------------
# 第 2 层：模板渲染层只输出 RJ 号 / 清理空段
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_minimal_metadata_names_by_rjcode_only(monkeypatch) -> None:
    monkeypatch.setattr(
        rename_service_module, "get_config", lambda: _config("[{rjcode}][{work_name}]")
    )

    name = await RenameService().resolve_target_name({
        "rjcode": "RJ01586348",
        "work_name": "2_2(RJ01586348)_ba3822b2_jt_yoio4",
        "metadata_source": "minimal",
    })

    assert name == "[RJ01586348]"


@pytest.mark.asyncio
async def test_verified_metadata_keeps_template_rendering(monkeypatch) -> None:
    monkeypatch.setattr(
        rename_service_module, "get_config", lambda: _config("[{rjcode}][{work_name}]")
    )

    name = await RenameService().resolve_target_name({
        "rjcode": "RJ01586348",
        "work_name": "夏の思い出",
        "metadata_source": "dlsite",
    })

    assert name == "[RJ01586348][夏の思い出]"


@pytest.mark.asyncio
async def test_empty_title_falls_back_to_rjcode(monkeypatch) -> None:
    monkeypatch.setattr(rename_service_module, "get_config", lambda: _config("【{work_name}】"))

    name = await RenameService().resolve_target_name({
        "rjcode": "RJ01586348",
        "work_name": "",
        "metadata_source": "minimal",
    })

    assert name == "RJ01586348"


def test_clean_empty_template_segments() -> None:
    assert RenameService._clean_empty_template_segments("[RJ1][ ]【】() （ ）") == "[RJ1]"
    assert RenameService._clean_empty_template_segments("  [RJ1]   标题  ") == "[RJ1] 标题"


# ---------------------------------------------------------------------------
# 第 3 层：断网暂停在重命名前，重试复用已解压产物
# ---------------------------------------------------------------------------

def _engine_stub() -> TaskEngine:
    return TaskEngine.__new__(TaskEngine)


def _task(metadata: dict, output_path: str = "") -> Task:
    task = Task(task_type=TaskType.AUTO_PROCESS, source_path="D:/input/RJ01586348.7z")
    task.rjcode = "RJ01586348"
    task.task_metadata = dict(metadata)
    task.output_path = output_path
    return task


def test_dlsite_pause_is_pre_rename() -> None:
    pre = _task({"dlsite_network_paused_stage": "pre_rename"})
    legacy = _task({"dlsite_network_paused": True})

    assert TaskEngine._dlsite_pause_is_pre_rename(pre) is True
    assert TaskEngine._dlsite_pause_is_pre_rename(legacy) is False


def test_mark_dlsite_network_paused_keeps_temp_artifacts(tmp_path) -> None:
    engine = _engine_stub()
    engine._record_problem_work_for_task_failure = lambda *a, **k: None
    engine._remove_waiting_retry_task = lambda *a, **k: None
    resume_dir = tmp_path / TEMP_DIR_NAME
    resume_dir.mkdir()
    task = _task({}, output_path="")

    engine._mark_dlsite_network_paused(
        task,
        "ConnectError",
        paused_stage="pre_rename",
        resume_path=str(resume_dir),
    )

    assert task.task_metadata["dlsite_network_paused_stage"] == "pre_rename"
    assert task.task_metadata["dlsite_network_resume_path"] == str(resume_dir)
    # 产物还在临时目录里，必须记到 output_path，否则重试入口判定产物丢失
    assert task.output_path == str(resume_dir)
    assert "未命名" in task.current_step


def test_mark_dlsite_network_paused_default_stage_keeps_output_path() -> None:
    engine = _engine_stub()
    engine._record_problem_work_for_task_failure = lambda *a, **k: None
    engine._remove_waiting_retry_task = lambda *a, **k: None
    task = _task({}, output_path="/library/RJ015xxxxx/[RJ01586348]")

    engine._mark_dlsite_network_paused(task, "ConnectError")

    assert task.task_metadata["dlsite_network_paused_stage"] == "pre_archive"
    assert "dlsite_network_resume_path" not in task.task_metadata
    # 旧暂停点不能覆盖已有库存路径
    assert task.output_path == "/library/RJ015xxxxx/[RJ01586348]"


def test_prepare_pre_rename_resume_restores_plain_task(tmp_path) -> None:
    engine = _engine_stub()
    resume_dir = tmp_path / TEMP_DIR_NAME
    resume_dir.mkdir()
    task = _task(
        {
            "resume_from_stage": "dlsite_network",
            "dlsite_network_paused": True,
            "dlsite_network_paused_stage": "pre_rename",
            "dlsite_network_error": "ConnectError",
            "retry_exhausted": True,
            "retry_exhausted_at": "2026-11-08T12:14:58",
            "available_actions": ["RETRY", "SKIP"],
        },
        output_path=str(resume_dir),
    )
    task.error_message = "boom"

    engine._prepare_pre_rename_resume(task)

    metadata = task.task_metadata
    assert "dlsite_network_paused" not in metadata
    assert "dlsite_network_paused_stage" not in metadata
    assert "resume_from_stage" not in metadata
    assert "retry_exhausted" not in metadata
    assert "available_actions" not in metadata
    assert metadata["pre_rename_resume_path"] == str(resume_dir)
    assert task.error_message is None


def test_prepare_pre_rename_resume_without_artifacts(tmp_path) -> None:
    engine = _engine_stub()
    missing = tmp_path / "gone"
    task = _task({"dlsite_network_paused_stage": "pre_rename"}, output_path=str(missing))

    engine._prepare_pre_rename_resume(task)

    # 目录丢了不要让任务卡死：不留续跑路径，主流程会重新解压
    assert "pre_rename_resume_path" not in task.task_metadata


# ---------------------------------------------------------------------------
# 第 4 层：降级元数据一律停止（不再依赖"是否发生过传输失败"这个旁证）
# ---------------------------------------------------------------------------

def test_metadata_unavailable_reason_flags_minimal_only() -> None:
    from app.core.metadata_service import metadata_unavailable_reason

    assert metadata_unavailable_reason(
        {"metadata_source": "minimal", "rename_skipped_reason": "DLsite 连接失败，无法获取元数据"}
    ) == "DLsite 连接失败，无法获取元数据"
    assert metadata_unavailable_reason(
        {"metadata_source": "minimal", "metadata_verification_reason": "DLsite 网络不可达: ConnectError"}
    ) == "DLsite 网络不可达: ConnectError"
    assert metadata_unavailable_reason({"metadata_source": "minimal"})
    assert metadata_unavailable_reason({"metadata_source": "dlsite", "work_name": "ok"}) == ""
    assert metadata_unavailable_reason({"metadata_source": "cache"}) == ""
    assert metadata_unavailable_reason(None)


def test_mark_metadata_unavailable_paused_keeps_artifacts_and_flags() -> None:
    engine = _engine_stub()
    engine._record_problem_work_for_task_failure = lambda *a, **k: None
    engine._remove_waiting_retry_task = lambda *a, **k: None

    task = _task({}, output_path="")

    engine._mark_metadata_unavailable_paused(
        task,
        "DLsite 连接失败，无法获取元数据",
        paused_stage="pre_rename",
        resume_path="D:/temp/RJ01586348_extract",
    )

    assert task.status.value == "waiting_manual"
    assert task.task_metadata["metadata_unavailable"] is True
    assert task.task_metadata["metadata_unavailable_reason"].startswith("DLsite 连接失败")
    assert task.task_metadata["dlsite_network_paused"] is True
    assert task.task_metadata["dlsite_network_paused_stage"] == "pre_rename"
    assert task.output_path == "D:/temp/RJ01586348_extract"
    assert "未命名" in task.current_step
    assert "元数据获取失败" in task.current_step


def test_prepare_pre_rename_resume_clears_metadata_unavailable_flags(tmp_path) -> None:
    engine = _engine_stub()
    resume_dir = tmp_path / TEMP_DIR_NAME
    resume_dir.mkdir()
    task = _task(
        {
            "metadata_unavailable": True,
            "metadata_unavailable_reason": "DLsite 连接失败",
            "dlsite_network_paused": True,
            "dlsite_network_paused_stage": "pre_rename",
            "resume_from_stage": "dlsite_network",
        },
        output_path=str(resume_dir),
    )

    engine._prepare_pre_rename_resume(task)

    assert "metadata_unavailable" not in task.task_metadata
    assert "metadata_unavailable_reason" not in task.task_metadata
    assert task.task_metadata["pre_rename_resume_path"] == str(resume_dir)


@pytest.mark.asyncio
async def test_auto_process_stops_before_rename_when_metadata_is_minimal(
    tmp_path, monkeypatch
) -> None:
    """核心回归：传输失败快照已空时，只要元数据是 minimal 也必须停在重命名前。

    旧实现用 ``_dlsite_transport_failure_reason()`` 当守卫条件，而续跑入口一进来就
    清了这份快照、短熔断期间又不会再记录 -> 守卫静默失效，作品被命名成 "[RJ01685546]"
    入库。这里把该函数打桩成返回空串，模拟现场。
    """
    from app.config import settings as settings_module
    from app.core.task_engine import TaskEngine

    engine = TaskEngine(max_concurrent=1)
    resume_dir = tmp_path / "extract" / "RJ01685546_abcd"
    resume_dir.mkdir(parents=True)
    (resume_dir / "track.wav").write_bytes(b"audio")
    task = Task(
        TaskType.AUTO_PROCESS,
        "D:/input/RJ01685546.7z",
        task_id="task-minimal-stop",
        metadata={
            "rjcode": "RJ01685546",
            "resume_from_stage": "dlsite_network",
            "dlsite_network_paused": True,
            "dlsite_network_paused_stage": "pre_rename",
            "dlsite_network_resume_path": str(resume_dir),
            "retry_from_conflicts": True,
        },
        rjcode="RJ01685546",
    )

    config = SimpleNamespace(
        auto_process=SimpleNamespace(
            check_duplicate=False,
            fetch_metadata=True,
            rename=True,
            filter=False,
            classify=True,
            archive=False,
            extract=True,
        ),
        storage=SimpleNamespace(library_path=str(tmp_path / "library")),
    )
    monkeypatch.setattr(settings_module, "get_config", lambda: config)

    class FakeMetadataService:
        async def fetch(self, *_args, **_kwargs):
            return {
                "rjcode": "RJ01685546",
                "metadata_source": "minimal",
                "rename_skipped_reason": "DLsite 连接失败，无法获取元数据",
            }

    class FakeRenameService:
        async def rename(self, *_args, **_kwargs):
            raise AssertionError("元数据没抓到就不能重命名")

    class FakeClassifier:
        async def classify_and_move(self, *_args, **_kwargs):
            raise AssertionError("元数据没抓到就不能入库")

    monkeypatch.setattr("app.core.metadata_service.MetadataService", FakeMetadataService)
    monkeypatch.setattr("app.core.rename_service.RenameService", FakeRenameService)
    monkeypatch.setattr("app.core.classifier.SmartClassifier", FakeClassifier)
    monkeypatch.setattr(engine, "_notify_progress", AsyncMock())
    monkeypatch.setattr(engine, "_record_problem_work_for_task_failure", Mock())
    monkeypatch.setattr(engine, "_remove_waiting_retry_task", Mock())
    monkeypatch.setattr(engine, "_resolve_retry_extract_conflict", Mock())
    monkeypatch.setattr(engine, "_resolve_completed_failure_followups", Mock())
    monkeypatch.setattr(engine, "_finalize_conflict_resolution_task", Mock())
    monkeypatch.setattr(engine, "_cleanup_task_temp_extract_path", AsyncMock())
    monkeypatch.setattr(engine, "_cleanup_failed_task", AsyncMock())
    monkeypatch.setattr(engine, "persist_task_center_item_snapshot", Mock())
    monkeypatch.setattr(engine, "persist_task_snapshot", Mock())
    monkeypatch.setattr("app.core.activity_log_service.log_task_lifecycle_event", Mock())
    monkeypatch.setattr("app.core.task_notification_service.enqueue_notification_check", AsyncMock())
    monkeypatch.setattr(engine, "_dlsite_transport_failure_reason", Mock(return_value=""))

    await engine._process_task(task)

    assert task.status == TaskStatus.WAITING_MANUAL
    assert task.task_metadata["metadata_unavailable"] is True
    assert str(task.output_path) == str(resume_dir)
    assert (resume_dir / "track.wav").read_bytes() == b"audio"
