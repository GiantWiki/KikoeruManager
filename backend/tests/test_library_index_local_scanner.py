from __future__ import annotations

from app.core.library_index._helpers import is_filter_recovery_path, should_skip_name
from app.core.library_index.local_scanner import LocalScanner
from app.core.library_manager import LibraryManager


def test_should_skip_name_keeps_underscore_resources_visible() -> None:
    assert should_skip_name("_096.png") is False
    assert should_skip_name("_bonus") is False
    assert should_skip_name(".DS_Store") is True
    assert should_skip_name("#recycle") is True
    assert should_skip_name("@eaDir") is True
    assert should_skip_name("__MACOSX") is True
    assert LibraryManager._should_skip_entry(None, "_096.png") is False


def test_local_scanner_indexes_underscore_files_and_counts_their_size(tmp_path) -> None:
    root = tmp_path / "library"
    work = root / "RJ01476998"
    work.mkdir(parents=True)
    (work / "_096.png").write_bytes(b"underscore-resource")
    (work / "normal.png").write_bytes(b"normal")
    (work / ".DS_Store").write_bytes(b"hidden")

    entries = list(LocalScanner().scan("local-library", str(root)))
    by_path = {entry.relative_path: entry for entry in entries}

    assert "RJ01476998/_096.png" in by_path
    assert "RJ01476998/normal.png" in by_path
    assert "RJ01476998/.DS_Store" not in by_path
    assert by_path["RJ01476998"].file_count == 2
    assert by_path["RJ01476998"].size == len(b"underscore-resource") + len(b"normal")


def test_local_scanner_excludes_filter_recovery_from_full_and_subtree_scans(tmp_path) -> None:
    root = tmp_path / "library"
    normal = root / "RJ01476998"
    normal.mkdir(parents=True)
    (normal / "track.wav").write_bytes(b"normal-version")

    recovery = root / "filter-recovery" / "task-1" / "RJ01476998"
    recovery.mkdir(parents=True)
    cached_file = recovery / "deleted.wav"
    cached_file.write_bytes(b"recoverable-deleted-version")

    scanner = LocalScanner()
    entries = list(scanner.scan("local-library", str(root)))
    by_path = {entry.relative_path: entry for entry in entries}

    assert "RJ01476998/track.wav" in by_path
    assert not any(is_filter_recovery_path(path) for path in by_path)
    assert list(scanner.scan_subtree(
        "local-library", str(root), str(root / "filter-recovery")
    )) == []
    assert list(scanner.scan_subtree(
        "local-library", str(root), str(cached_file)
    )) == []
    assert cached_file.read_bytes() == b"recoverable-deleted-version"
    assert is_filter_recovery_path(r"filter-recovery\task-1\RJ01476998") is True
    assert is_filter_recovery_path("filter-recovery-copy/RJ01476998") is False
