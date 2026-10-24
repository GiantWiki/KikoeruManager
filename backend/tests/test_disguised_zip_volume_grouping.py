"""无标准主卷的 ZIP 分卷聚合测试。"""

import os

from app.core.archive_volume_utils import detect_archive_volume_group
from app.core.extract_service import ExtractService
from app.core.file_processor import FileProcessor


ZIP_PAYLOAD = b"PK\x03\x04" + b"\x00" * 4092


def _write(path, content=ZIP_PAYLOAD):
    with open(path, "wb") as fh:
        fh.write(content)


def test_z01_zi_detected_as_single_volume_group(tmp_path):
    z01 = tmp_path / "RJ01701267(southplus).z01"
    zi = tmp_path / "RJ01701267(southplus).zi"
    _write(z01)
    _write(zi)

    processor = FileProcessor()
    volume_set = processor.detect_volume_set(str(z01))

    assert volume_set is not None
    assert volume_set.type == "zip_truncated_main"
    assert volume_set.entry_path == str(z01)
    assert [os.path.basename(path) for path in volume_set.volumes] == [z01.name, zi.name]
    assert processor.is_archive(str(z01)) is True
    assert processor.is_archive(str(zi)) is False


def test_z01_z02_without_zip_detected_as_single_volume_group(tmp_path):
    z01 = tmp_path / "RJ01701267(southplus).z01"
    z02 = tmp_path / "RJ01701267(southplus).z02"
    _write(z01)
    _write(z02)

    processor = FileProcessor()
    volume_set = processor.detect_volume_set(str(z01))

    assert volume_set is not None
    assert volume_set.type == "zip_disguised_volumes"
    assert volume_set.entry_path == str(z01)
    assert [os.path.basename(path) for path in volume_set.volumes] == [z01.name, z02.name]
    assert processor.is_archive(str(z01)) is True
    assert processor.is_archive(str(z02)) is False


def test_standard_zip_main_remains_entry(tmp_path):
    zip_path = tmp_path / "archive.zip"
    z01 = tmp_path / "archive.z01"
    _write(zip_path)
    _write(z01)

    volume_set = FileProcessor().detect_volume_set(str(zip_path))

    assert volume_set is not None
    assert volume_set.type == "zip_volume_main"
    assert volume_set.entry_path == str(zip_path)
    assert set(volume_set.volumes) == {str(zip_path), str(z01)}


def test_single_zi_without_z01_keeps_single_file_behavior(tmp_path):
    zi = tmp_path / "archive.zi"
    _write(zi)

    processor = FileProcessor()

    assert processor.detect_volume_set(str(zi)) is None
    assert processor.is_archive(str(zi)) is True


def test_single_z01_without_siblings_is_not_treated_as_group(tmp_path):
    z01 = tmp_path / "archive.z01"
    _write(z01)

    processor = FileProcessor()

    assert processor.detect_volume_set(str(z01)) is None
    assert processor.is_archive(str(z01)) is True


def test_single_z77_without_siblings_keeps_legacy_behavior(tmp_path):
    disguised = tmp_path / "archive.z77"
    _write(disguised)

    processor = FileProcessor()

    assert processor.detect_volume_set(str(disguised)) is None
    assert processor.is_archive(str(disguised)) is True


def test_non_contiguous_z_volumes_are_not_grouped(tmp_path):
    z01 = tmp_path / "archive.z01"
    z03 = tmp_path / "archive.z03"
    _write(z01)
    _write(z03)

    assert FileProcessor().detect_volume_set(str(z01)) is None


def test_archive_volume_utils_includes_zi_last(tmp_path):
    z01 = tmp_path / "archive.z01"
    zi = tmp_path / "archive.zi"
    _write(z01)
    _write(zi)

    group = detect_archive_volume_group(str(z01))

    assert group is not None
    assert group.volume_type == "zip_z"
    assert [os.path.basename(path) for path in group.volumes] == [z01.name, zi.name]


def test_disguised_scanner_maps_zi_to_final_volume(tmp_path):
    z01 = tmp_path / "archive.z01"
    zi = tmp_path / "archive.zi"
    _write(z01)
    _write(zi)

    siblings = ExtractService()._scan_disguised_volume_siblings(
        str(tmp_path),
        z01.name,
        sorted(os.listdir(tmp_path)),
    )

    assert [(item["name"], item["index"]) for item in siblings] == [
        (z01.name, 1),
        (zi.name, 2),
    ]


def test_disguised_detection_skips_standard_z_volume_set(tmp_path):
    """标准 .z01 + .zi 是「缺末端 .zip 卷」，不是伪装命名，不能弹重命名流程。"""
    z01 = tmp_path / "archive.z01"
    zi = tmp_path / "archive.zi"
    _write(z01)
    _write(zi)

    assert ExtractService()._detect_disguised_volume_set(str(z01)) is None


def test_extract_volume_set_includes_zi_as_terminal(tmp_path):
    z01 = tmp_path / "archive.z01"
    zi = tmp_path / "archive.zi"
    _write(z01)
    _write(zi)

    volume_set = ExtractService()._detect_volume_set(str(z01))

    assert volume_set is not None
    assert volume_set.type == "zip_missing_main_volume"
    assert volume_set.entry_path == str(z01)
    assert [os.path.basename(path) for path in volume_set.volumes] == [z01.name, zi.name]


def test_extract_volume_set_treats_last_z02_as_terminal(tmp_path):
    """用户手工把 .zi 改成 .z02 后，序号最大的 zNN 就是末端卷。"""
    z01 = tmp_path / "archive.z01"
    z02 = tmp_path / "archive.z02"
    _write(z01)
    _write(z02)

    volume_set = ExtractService()._detect_volume_set(str(z01))

    assert volume_set is not None
    assert volume_set.type == "zip_missing_main_volume"
    assert [os.path.basename(path) for path in volume_set.volumes] == [z01.name, z02.name]


def test_extract_volume_set_rejects_non_contiguous_z_volumes(tmp_path):
    z01 = tmp_path / "archive.z01"
    z03 = tmp_path / "archive.z03"
    _write(z01)
    _write(z03)

    assert ExtractService()._detect_volume_set(str(z01)) is None


def test_extract_volume_set_accepts_zi_as_entry(tmp_path):
    """任务直接落在 .zi 上（旧版本遗留的问题作品项）也要能整组成组。"""
    z01 = tmp_path / "archive.z01"
    zi = tmp_path / "archive.zi"
    _write(z01)
    _write(zi)

    volume_set = ExtractService()._detect_volume_set(str(zi))

    assert volume_set is not None
    assert volume_set.type == "zip_missing_main_volume"
    assert os.path.basename(volume_set.entry_path) == z01.name
    assert [os.path.basename(path) for path in volume_set.volumes] == [z01.name, zi.name]


def test_extract_volume_set_accepts_zip_signature_behind_prefix(tmp_path):
    """polyglot 前缀壳会让 PK 签名落在偏移 4，不能因此拒认分卷组。"""
    z01 = tmp_path / "archive.z01"
    zi = tmp_path / "archive.zi"
    _write(z01, b"\x00\x00\x00\x00" + ZIP_PAYLOAD)
    _write(zi, b"\x00\x00\x00\x00" + ZIP_PAYLOAD)

    volume_set = ExtractService()._detect_volume_set(str(z01))

    assert volume_set is not None
    assert volume_set.type == "zip_missing_main_volume"


def test_extract_volume_set_rejects_seven_zip_renamed_to_z(tmp_path):
    """7z 分卷被改名成 .zNN 时属于伪装多卷，不能被 ZIP 补名链路抢走。"""
    z01 = tmp_path / "archive.z01"
    z02 = tmp_path / "archive.z02"
    _write(z01, b"7z\xbc\xaf\x27\x1c" + b"\x00" * 4088)
    _write(z02, b"7z\xbc\xaf\x27\x1c" + b"\x00" * 4088)

    assert ExtractService()._detect_volume_set(str(z01)) is None


def test_remap_zip_missing_main_volume_renames_terminal(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from app.core.extract_service import VolumeSet

    z01 = tmp_path / "archive.z01"
    zi = tmp_path / "archive.zi"
    _write(z01)
    _write(zi)

    service = ExtractService()
    volume_set = VolumeSet(
        "archive",
        [str(z01), str(zi)],
        "zip_missing_main_volume",
        entry_path=str(z01),
    )
    task = SimpleNamespace(task_metadata={})

    remapped = asyncio.run(service._remap_zip_missing_main_volume(volume_set, task))

    assert remapped.type == "zip_volume_main"
    assert os.path.basename(remapped.entry_path) == z01.name
    assert [os.path.basename(path) for path in remapped.volumes] == ["archive.z01", "archive.zip"]
    assert (tmp_path / "archive.zip").exists()
    assert not zi.exists()
    assert task.task_metadata["zip_missing_main_remap"]["renamed"].endswith("archive.zip")

    asyncio.run(service._rollback_zip_missing_main_remap(task))

    assert zi.exists()
    assert not (tmp_path / "archive.zip").exists()
    assert "zip_missing_main_remap" not in task.task_metadata


def test_disguised_detection_rejects_non_contiguous_z_volumes(tmp_path):
    z01 = tmp_path / "archive.z01"
    z03 = tmp_path / "archive.z03"
    _write(z01)
    _write(z03)

    assert ExtractService()._detect_disguised_volume_set(str(z01)) is None


def test_conflict_skip_collects_z01_and_zi(tmp_path):
    from app.api.routes import _collect_split_archive_siblings

    z01 = tmp_path / "archive.z01"
    zi = tmp_path / "archive.zi"
    unrelated = tmp_path / "archive.txt"
    _write(z01)
    _write(zi)
    unrelated.write_text("keep", encoding="utf-8")

    siblings = _collect_split_archive_siblings(str(z01))

    assert {os.path.basename(path) for path in siblings} == {z01.name, zi.name}


def test_conflict_skip_collects_z01_and_z02(tmp_path):
    from app.api.routes import _collect_split_archive_siblings

    z01 = tmp_path / "archive.z01"
    z02 = tmp_path / "archive.z02"
    _write(z01)
    _write(z02)

    siblings = _collect_split_archive_siblings(str(z02))

    assert {os.path.basename(path) for path in siblings} == {z01.name, z02.name}


def test_zip_standard_volume_name_uses_spanning_convention():
    """ZIP 的推荐改名必须是 Info-ZIP 跨卷命名，不能是 .zip.NNN 字节切分命名。

    用户实测 RJ01701267：按旧建议把 .z01/.zi 改成 .zip.001/.zip.002 后，
    7-Zip 改用字节拼接处理器，`7zz l` 直接返回码 2 且无任何错误输出，
    本来能解的包被改成了读不出来的包。
    """
    service = ExtractService()

    first = service._build_standard_volume_name("foo", "zip", 1, total=3)
    middle = service._build_standard_volume_name("foo", "zip", 2, total=3)
    last = service._build_standard_volume_name("foo", "zip", 3, total=3)

    assert first == "foo.z01"
    assert middle == "foo.z02"
    assert last == "foo.zip"


def test_disguised_zip_suggestion_ends_with_dot_zip(tmp_path):
    """伪装 ZIP 分卷的改名建议：中间卷 .zNN，末端卷 .zip。"""
    v1 = tmp_path / "foo.z7.001"
    v2 = tmp_path / "foo.z7.002"
    _write(v1)
    _write(v2)

    detection = ExtractService()._detect_disguised_volume_set(str(v1))

    assert detection is not None
    assert detection["detected_kind"] == "zip"
    suggested = [os.path.basename(item["new"]) for item in detection["suggested_renames"]]
    assert suggested == ["foo.z01", "foo.zip"]


def test_archive_unreadable_reason_has_actionable_message():
    from app.core.failure_reason_formatter import format_extract_failure_message

    message = format_extract_failure_message(
        {"extract_failure_reason": "archive_unreadable"},
        "兜底文案",
    )

    assert "无法打开" in message
    assert "X.zip.001" in message
