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

import pytest

from app.core import rename_service as rename_service_module
from app.core.metadata_service import MetadataService
from app.core.rename_service import RenameService
from app.core.task_engine import Task, TaskEngine, TaskType


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
