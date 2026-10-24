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


def test_disguised_detection_suggests_zip_names_for_z01_zi(tmp_path):
    z01 = tmp_path / "archive.z01"
    zi = tmp_path / "archive.zi"
    _write(z01)
    _write(zi)

    detection = ExtractService()._detect_disguised_volume_set(str(z01))

    assert detection is not None
    assert [os.path.basename(item["old"]) for item in detection["suggested_renames"]] == [
        z01.name,
        zi.name,
    ]
    assert [os.path.basename(item["new"]) for item in detection["suggested_renames"]] == [
        "archive.zip.001",
        "archive.zip.002",
    ]


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
