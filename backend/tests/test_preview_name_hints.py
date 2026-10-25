"""预览名称解析单测。"""

from app.core.preview_name_hints import (
    apply_hints_to_preview,
    build_name_hint,
    extract_password_by_rules,
    extract_rj,
    is_complete_volume_group,
    parse_standard_input,
    password_from_filename_label,
    share_identity,
)

URL = "https://pan.baidu.com/s/1AbCdEf"


def test_four_segment_format():
    result = parse_standard_input(
        f"RJ01234567 | {URL} | 提取码: xywz | 解压码: abc123"
    )
    assert result is not None
    urls, hints = result
    assert urls == [f"{URL}?pwd=xywz"]
    hint = hints[share_identity(URL)]
    assert hint["rj"] == "RJ01234567"
    assert hint["extract_password"] == "abc123"
    assert hint["pass_code"] == "xywz"


def test_three_segment_with_pwd_in_url():
    result = parse_standard_input(f"RJ01234567 | {URL}?pwd=xywz | 解压码: abc123")
    assert result is not None
    urls, hints = result
    assert urls == [f"{URL}?pwd=xywz"]
    assert hints[share_identity(URL)]["extract_password"] == "abc123"


def test_two_segment_without_password():
    result = parse_standard_input(f"RJ01234567 | {URL}")
    assert result is not None
    urls, hints = result
    assert urls == [URL]
    assert hints[share_identity(URL)]["extract_password"] == ""


def test_extract_password_segment_is_not_pass_code():
    """回归：解压码段必须被摘掉，不能变成 ?pwd=解压码。"""
    result = parse_standard_input(f"RJ01234567 | {URL} | 解压码: abc123")
    assert result is not None
    urls, _hints = result
    assert "pwd=" not in urls[0]
    assert "abc123" not in urls[0]


def test_non_rj_head_falls_back():
    """首段不是 RJ 且没有解压码标签 → 不命中，整段回退。"""
    assert parse_standard_input(f"某社团作品 | {URL} | 提取码: xywz") is None


def test_non_rj_head_with_password_only_keeps_password():
    result = parse_standard_input(f"某社团作品 | {URL} | 解压码: abc123")
    assert result is not None
    _urls, hints = result
    hint = hints[share_identity(URL)]
    assert hint["rj"] == ""
    assert hint["extract_password"] == "abc123"


def test_multiple_lines_bind_independently():
    text = "\n".join([
        f"RJ01111111 | {URL}AA | 解压码: passAAA",
        f"RJ02222222 | {URL}BB | 解压码: passBBB",
    ])
    result = parse_standard_input(text)
    assert result is not None
    _urls, hints = result
    assert hints[share_identity(f"{URL}AA")]["rj"] == "RJ01111111"
    assert hints[share_identity(f"{URL}AA")]["extract_password"] == "passAAA"
    assert hints[share_identity(f"{URL}BB")]["rj"] == "RJ02222222"
    assert hints[share_identity(f"{URL}BB")]["extract_password"] == "passBBB"


def test_no_newline_separator_does_not_pollute_url():
    """无空格写法 url|解压码 不能把竖线带进 URL。"""
    result = parse_standard_input(f"RJ01234567 | {URL}|解压码: abc123")
    assert result is not None
    urls, _hints = result
    assert "|" not in urls[0]
    assert "abc123" not in urls[0]


def test_apply_hints_single_file():
    preview = {
        "items": [
            {
                "share_url": URL,
                "preview_files": [{"name": "解压码****.zip"}],
            }
        ]
    }
    hints = {share_identity(URL): {"rj": "RJ01234567", "extract_password": "abc123"}}
    apply_hints_to_preview(preview, hints)
    row = preview["items"][0]["preview_files"][0]
    assert row["custom_name"] == "RJ01234567"
    assert row["custom_extract_password"] == "abc123"
    assert row["name_hint"] == "RJ01234567(abc123).zip"


def test_apply_hints_skips_rename_when_file_has_rj():
    preview = {
        "items": [
            {
                "share_url": URL,
                "preview_files": [{"name": "[RJ123456] 作品.zip"}],
            }
        ]
    }
    hints = {share_identity(URL): {"rj": "RJ09999999", "extract_password": "abc123"}}
    apply_hints_to_preview(preview, hints)
    row = preview["items"][0]["preview_files"][0]
    assert "custom_name" not in row
    assert row["custom_extract_password"] == "abc123"
    assert row["name_hint"] == "[RJ123456] 作品(abc123).zip"


def test_apply_hints_volume_group_renames_every_file():
    preview = {
        "items": [
            {
                "share_url": URL,
                "preview_files": [
                    {"name": "解压码****.part1.rar"},
                    {"name": "解压码****.part2.rar"},
                ],
            }
        ]
    }
    hints = {share_identity(URL): {"rj": "RJ01234567", "extract_password": "abc123"}}
    apply_hints_to_preview(preview, hints)
    rows = preview["items"][0]["preview_files"]
    assert [row["custom_name"] for row in rows] == ["RJ01234567", "RJ01234567"]
    assert rows[0]["name_hint"] == "RJ01234567(abc123).part1.rar"
    assert rows[1]["name_hint"] == "RJ01234567(abc123).part2.rar"


def test_apply_hints_multi_file_non_volume_only_suggests():
    preview = {
        "items": [
            {
                "share_url": URL,
                "preview_files": [
                    {"name": "解压码****.zip"},
                    {"name": "说明.txt"},
                ],
            }
        ]
    }
    hints = {share_identity(URL): {"rj": "RJ01234567", "extract_password": "abc123"}}
    apply_hints_to_preview(preview, hints)
    item = preview["items"][0]
    assert item["rj_name_suggested"] == "RJ01234567"
    assert all("custom_name" not in row for row in item["preview_files"])
    assert all(row["custom_extract_password"] == "abc123" for row in item["preview_files"])


def test_volume_group_detection():
    assert is_complete_volume_group(["a.part1.rar", "a.part2.rar"])
    assert is_complete_volume_group(["a.7z.001", "a.7z.002"])
    assert is_complete_volume_group(["a.z01", "a.z02"])
    assert is_complete_volume_group(["a.r00", "a.r01"])
    assert not is_complete_volume_group(["a.part1.rar", "a.part3.rar"])
    assert not is_complete_volume_group(["a.part1.rar", "b.part2.rar"])
    assert not is_complete_volume_group(["a.zip", "说明.txt"])


def test_build_name_hint_variants():
    assert build_name_hint("解压码****.zip", "RJ01234567", "abc123") == "RJ01234567(abc123).zip"
    assert build_name_hint("解压码****.7z.001", "RJ01234567", "abc123") == "RJ01234567(abc123).7z.001"
    assert build_name_hint("解压码****", "", "") == "解压码****"


def test_extract_password_by_rules_bad_regex_is_silent():
    assert extract_password_by_rules("解压码 abc123.zip", [r"([unclosed"]) == ""
    assert extract_password_by_rules("解压码: abc123.zip", [r"解压码\s*[:：]?\s*(\S+)"]) == "abc123.zip"


def test_extract_rj():
    assert extract_rj("[RJ123456] 作品") == "RJ123456"
    assert extract_rj("rj01234567 作品") == "RJ01234567"
    assert extract_rj("没有编号") == ""


def test_password_from_filename_label():
    assert password_from_filename_label("解压码1021") == "1021"
    assert password_from_filename_label("解压码1021.zip") == "1021"
    assert password_from_filename_label("解压码：abc123.rar") == "abc123"
    assert password_from_filename_label("解压码.zip") == ""
    assert password_from_filename_label("RJ01667840.zip") == ""


def test_apply_hints_falls_back_to_filename_label():
    """线上实际场景：规范行只有提取码，解压码只在文件名里。"""
    preview = {
        "items": [
            {
                "share_url": URL,
                "preview_files": [{"name": "解压码1021"}],
            }
        ]
    }
    hints = {share_identity(URL): {"rj": "RJ01667840", "extract_password": ""}}
    apply_hints_to_preview(preview, hints, rules=[])
    row = preview["items"][0]["preview_files"][0]
    assert row["custom_name"] == "RJ01667840"
    assert row["custom_extract_password"] == "1021"
    assert row["name_hint"] == "RJ01667840(1021)"


def test_apply_hints_hint_password_beats_filename_label():
    preview = {
        "items": [
            {
                "share_url": URL,
                "preview_files": [{"name": "解压码1021.zip"}],
            }
        ]
    }
    hints = {share_identity(URL): {"rj": "RJ01667840", "extract_password": "abc123"}}
    apply_hints_to_preview(preview, hints, rules=[])
    row = preview["items"][0]["preview_files"][0]
    assert row["custom_extract_password"] == "abc123"
    assert row["name_hint"] == "RJ01667840(abc123).zip"


def test_apply_hints_user_rule_beats_filename_label():
    preview = {
        "items": [
            {
                "share_url": URL,
                "preview_files": [{"name": "解压码1021.zip"}],
            }
        ]
    }
    hints = {share_identity(URL): {"rj": "RJ01667840", "extract_password": ""}}
    apply_hints_to_preview(preview, hints, rules=[r"解压码(\d+)"])
    row = preview["items"][0]["preview_files"][0]
    assert row["custom_extract_password"] == "1021"


# ---------------------------------------------------------------------------
# 跨行 / 同段粘贴：起任务阶段没有文件名标签兜底，解析必须尽量认全
# ---------------------------------------------------------------------------


def test_password_in_same_segment_as_url_keeps_url():
    """``URL 解压码10221``（空格同段）：旧实现命中解压码后 continue，链接被丢掉。"""
    parsed = parse_standard_input(f"{URL} 解压码10221 RJ01607138")
    assert parsed is not None
    urls, hints = parsed
    assert urls == [URL]
    hint = hints[share_identity(URL)]
    assert hint["extract_password"] == "10221"
    assert hint["rj"] == "RJ01607138"


def test_prefix_text_before_url_is_tolerated():
    """``链接：URL 解压码：10221 RJ…`` 这类带前缀的写法。"""
    parsed = parse_standard_input(f"链接：{URL} 解压码：10221 RJ01607138")
    assert parsed is not None
    _, hints = parsed
    hint = hints[share_identity(URL)]
    assert hint["extract_password"] == "10221"
    assert hint["rj"] == "RJ01607138"


def test_multiline_paste_binds_continuation_lines_to_url():
    """链接一行、解压码一行、RJ 一行：续行必须并入链接，否则整段 hint 为空。"""
    text = f"{URL}\n解压码10221.z1p\nRJ01607138(10221).z1p"
    parsed = parse_standard_input(text)
    assert parsed is not None
    urls, hints = parsed
    assert urls == [URL]
    hint = hints[share_identity(URL)]
    assert hint["extract_password"] == "10221"  # 归档后缀要剥掉
    assert hint["rj"] == "RJ01607138"


def test_multiline_password_before_url_binds_forward():
    parsed = parse_standard_input(f"解压码10221\n{URL}")
    assert parsed is not None
    _, hints = parsed
    assert hints[share_identity(URL)]["extract_password"] == "10221"


def test_multiline_two_entries_do_not_cross_bind():
    other = "https://pan.baidu.com/s/1zzzzzzz?pwd=bbbb"
    text = f"{URL}?pwd=aaaa\nRJ00000001\n{other}\nRJ00000002"
    parsed = parse_standard_input(text)
    assert parsed is not None
    urls, hints = parsed
    assert urls == [f"{URL}?pwd=aaaa", other]
    assert hints[share_identity(urls[0])]["rj"] == "RJ00000001"
    assert hints[share_identity(urls[1])]["rj"] == "RJ00000002"
