from app.api import routes
from app.core.task_engine import Task, TaskType


def test_http_download_input_pairs_pikpak_password_on_next_line():
    assert routes._http_download_urls_from_payload([
        "https://mypikpak.com/s/share-a",
        "提取码：A1b2",
        "https://example.com/file.zip",
    ]) == [
        "https://mypikpak.com/s/share-a?pwd=A1b2",
        "https://example.com/file.zip",
    ]


def test_http_download_input_pairs_multiline_pikpak_share_text():
    assert routes._http_download_urls_from_payload([
        "https://drive.mypikpak.com/s/share-b\n访问码: 9z8y",
    ]) == ["https://drive.mypikpak.com/s/share-b?pwd=9z8y"]

def test_http_download_input_does_not_duplicate_pikpak_url_with_code():
    assert routes._http_download_urls_from_payload([
        "分享给你：https://mypikpak.com/s/share-b 访问码: 9z8y",
    ]) == ["https://mypikpak.com/s/share-b?pwd=9z8y"]


def test_http_download_input_pikpak_fragment_code_single_url():
    assert routes._http_download_urls_from_payload([
        "https://mypikpak.com/s/share-b#提取码:9z8y",
    ]) == ["https://mypikpak.com/s/share-b?pwd=9z8y"]


def test_http_download_status_exposes_stable_domain_for_gofile():
    task = Task(
        task_type=TaskType.HTTP_DOWNLOAD,
        source_path="",
        metadata={
            "task_domain": "http_download",
            "task_kind": TaskType.HTTP_DOWNLOAD.value,
            "download_mode": "gofile",
            "selected_items": [],
        },
    )
    task.fail("没有通过校验的下载项")

    payload = routes._serialize_http_download_task(task)

    assert payload["download_mode"] == "gofile"
    assert payload["task_domain"] == "http_download"
    assert payload["task_kind"] == TaskType.HTTP_DOWNLOAD.value
    assert payload["task_metadata"]["task_domain"] == "http_download"
