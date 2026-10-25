"""预览名称解析：把「规范输入行」解析成分享链接 + 命名提示。

职责边界（很重要）：

- 只处理输入框里的规范格式行，**不参与链接识别本身**；
- 只产出 ``(urls, hints)``，不写库、不发请求、不改预览的原始文件名字段；
- 不改 ``netdisk_link_parser`` 的任何行为（该模块 impact 14 / CRITICAL）。

为什么必须先切段、再交给既有解析器
----------------------------------

1. ``netdisk_link_parser._URL_RE`` 的排除字符类不含 ``|``，``url|解压码: xxx``
   会把竖线一起吞进 URL，``_clean_url_candidate`` 的尾部清理也不含 ``|``；
2. 既有 ``_extract_pass_code_from_segment(allow_bare=True)`` 在 URL 自身没有
   ``?pwd=`` 时，会把段内第一个 4~12 位字母数字裸串当作提取码 —— 也就是把
   ``解压码: abc123`` 里的 ``abc123`` 拼成 ``?pwd=abc123``，百度直接报错。

所以本模块先按 ``|`` 切段、再整段摘掉解压码，只把干净 URL 交给既有逻辑。
摘掉之后第 2 条风险自然消失，不需要额外防御。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlparse

from .password_utils import extract_labeled_password, strip_archive_suffix

_PASS_CODE_QUERY_KEYS = ("pwd", "password", "passcode", "pass_code", "code")

# 段分隔符：竖线（含全角）、TAB、前后带空格的连续短横线。
_SEGMENT_SPLIT_RE = re.compile(r"\s*[|｜]\s*|\t+|\s+-{3,}\s+")

_RJ_RE = re.compile(r"(?<![A-Za-z0-9])RJ\s*(\d{5,8})(?!\d)", re.IGNORECASE)
_RJ_HEAD_RE = re.compile(r"^\s*(?:\[|【)?\s*RJ\s*\d{5,8}", re.IGNORECASE)

_EXTRACT_PASSWORD_LABEL_RE = re.compile(
    r"(?:解压密码|解壓密碼|解压码|解壓碼|压缩包密码|壓縮包密碼|压缩密码|壓縮密碼|"
    r"archive\s*password|unzip\s*password|extract\s*password)"
    r"\s*(?:[:：=]|是|为|為)?\s*([^\s,，。;；、|｜]+)",
    re.IGNORECASE,
)
_PASS_CODE_LABEL_RE = re.compile(
    r"(?:提取码|提取口令|访问码|pwd|passcode|pass_code)\s*[:：=]?\s*([A-Za-z0-9]{4,12})",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"'|｜]+", re.IGNORECASE)
_BARE_URL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:pan|yun|eyun)\.baidu\.com(?:/[^\s<>\"'|｜]*)?",
    re.IGNORECASE,
)

_PASSWORD_TRAILING_JUNK = ")]}>》」】'\"`。，；、,，"

# 纯文件名的分卷识别（预览阶段没有本地文件，不能复用 archive_volume_utils，
# 它在 zip_z 分支会 os.path.isfile）。
_VOLUME_GROUP_PATTERNS: Tuple[Tuple[re.Pattern, int], ...] = (
    (re.compile(r"^(?P<base>.+?)\.part(?P<index>\d+)\.rar$", re.IGNORECASE), 1),
    (re.compile(r"^(?P<base>.+?\.(?:7z|zip))\.(?P<index>\d{3})$", re.IGNORECASE), 1),
    (re.compile(r"^(?P<base>.+?)\.z(?P<index>\d{2})$", re.IGNORECASE), 1),
    (re.compile(r"^(?P<base>.+?)\.r(?P<index>\d{2})$", re.IGNORECASE), 0),
)

# 展示名用的扩展名拆分：分卷后缀整体保留。
_VOLUME_EXTENSION_PATTERNS = (
    re.compile(r"^(?P<stem>.+?)(?P<ext>\.part\d+\.rar)$", re.IGNORECASE),
    re.compile(r"^(?P<stem>.+?)(?P<ext>\.(?:7z|zip)\.\d{3})$", re.IGNORECASE),
    re.compile(r"^(?P<stem>.+?)(?P<ext>\.z\d{2})$", re.IGNORECASE),
    re.compile(r"^(?P<stem>.+?)(?P<ext>\.r\d{2})$", re.IGNORECASE),
)
_PLAIN_EXTENSION_RE = re.compile(r"^(?P<stem>.*?)(?P<ext>\.[A-Za-z0-9]{1,10})$")


def share_identity(url: str) -> str:
    """去掉提取码等查询参数后的分享身份，用作 hint 的绑定键。"""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return text.rstrip("/")
    pairs = [
        (key, value)
        for key, value in parse_qs(parsed.query or "", keep_blank_values=True).items()
        if str(key or "").lower() not in _PASS_CODE_QUERY_KEYS
    ]
    try:
        return parsed._replace(query=urlencode(pairs, doseq=True), fragment="").geturl().rstrip("/")
    except Exception:
        return text.rstrip("/")


def extract_rj(value: str) -> str:
    """从任意文本里取 RJ 号，返回 ``RJ01234567`` 形式；没有则空串。"""
    match = _RJ_RE.search(str(value or ""))
    return f"RJ{match.group(1)}" if match else ""


def _clean_password(value: str) -> str:
    text = str(value or "").strip()
    while text and text[-1] in _PASSWORD_TRAILING_JUNK:
        text = text[:-1]
    return text.strip()


def _clean_inline_password(value: str) -> str:
    """规范输入行里的解压码：额外切掉粘贴文件名带进来的归档后缀。

    只用于「解压码: xxx」标签解析；用户自定义正则的结果必须原样保留
    （``extract_password_by_rules`` 的既有契约）。
    """
    text = _clean_password(value)
    if not text:
        return ""
    return strip_archive_suffix(text) or text


def _pass_code_from_url(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return ""
    query = parse_qs(parsed.query or "")
    for key in _PASS_CODE_QUERY_KEYS:
        values = query.get(key) or []
        if values and re.fullmatch(r"[A-Za-z0-9]{4,12}", str(values[0] or "").strip()):
            return str(values[0]).strip()
    return ""


def _append_pass_code(url: str, code: str) -> str:
    text = str(url or "").strip()
    if not text or not code or _pass_code_from_url(text):
        return text
    return f"{text}{'&' if '?' in text else '?'}pwd={quote(code)}"


def _normalize_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = f"https://{text}"
    return text


def _split_segments(line: str) -> List[str]:
    return [segment for segment in _SEGMENT_SPLIT_RE.split(str(line or "")) if segment.strip()]


def _collect_segment_part(segment: str, state: Dict[str, str]) -> None:
    """从一段文本里抽取 解压码 / 链接 / 提取码 / RJ；**段内多要素也能全部认出**。

    为什么不能「命中一种要素就 continue」：用户经常写成
    ``https://pan.baidu.com/s/1abc 解压码10221``（同一段，空格分隔，没有竖线）。
    旧实现先匹配解压码并 continue，链接跟着整段被丢掉，整行判定失败、密码与 RJ
    全部回退成「无规范格式」。这里改成「摘掉已识别的片段，继续看剩余文本」。
    """
    text = str(segment or "").strip()
    if not text:
        return
    # 1) 解压码必须最先摘掉：否则残留的裸串会被 allow_bare 当成提取码。
    password_match = _EXTRACT_PASSWORD_LABEL_RE.search(text)
    if password_match:
        if not state["password"]:
            state["password"] = _clean_inline_password(password_match.group(1))
        text = f"{text[:password_match.start()]} {text[password_match.end():]}".strip()
        if not text:
            return
    # 2) 链接：不再要求位于段首，``链接：https://...`` 同样能认。
    url_match = _URL_RE.search(text) or _BARE_URL_RE.search(text)
    if url_match:
        if not state["url"]:
            state["url"] = _normalize_url(url_match.group(0))
        text = f"{text[:url_match.start()]} {text[url_match.end():]}".strip()
        if not text:
            return
    # 3) 提取码：链接已摘除，不会误命中 URL 自带的 ?pwd=。
    pass_code_match = _PASS_CODE_LABEL_RE.search(text)
    if pass_code_match:
        if not state["pass_code"]:
            state["pass_code"] = pass_code_match.group(1)
        text = f"{text[:pass_code_match.start()]} {text[pass_code_match.end():]}".strip()
        if not text:
            return
    # 4) RJ：剩余文本里出现 RJ 号就取（``RJ01607138(10221).z1p`` / 带前缀文字都算）。
    if not state["rj"] and text:
        state["rj"] = extract_rj(text)


def _parse_standard_segments(segments: Iterable[str]) -> Optional[Dict[str, str]]:
    """解析一组段（可来自多行合并）；不命中返回 ``None``（调用方退回既有识别）。"""
    state = {"rj": "", "url": "", "pass_code": "", "password": ""}
    for segment in segments:
        _collect_segment_part(segment, state)

    url = state["url"]
    if not url:
        return None
    rj = state["rj"]
    password = state["password"]
    # 既没有 RJ 也没有解压码 → 不算规范格式，整行回退。
    if not rj and not password:
        return None
    pass_code = state["pass_code"]
    url_pass_code = _pass_code_from_url(url)
    if not pass_code:
        pass_code = url_pass_code
    elif url_pass_code and url_pass_code != pass_code:
        # 链接自带的提取码优先，与规范段冲突时以链接为准。
        pass_code = url_pass_code
    if pass_code:
        url = _append_pass_code(url, pass_code)
    return {
        "rj": rj,
        "url": url,
        "pass_code": pass_code,
        "extract_password": password,
    }


def _parse_standard_line(line: str) -> Optional[Dict[str, str]]:
    """解析一行规范格式（单行版本，保留给单元测试与单行调用方）。"""
    return _parse_standard_segments(_split_segments(line))


def _line_has_url(segments: Iterable[str]) -> bool:
    for segment in segments:
        if _URL_RE.search(str(segment or "")) or _BARE_URL_RE.search(str(segment or "")):
            return True
    return False


def _segments_are_continuation(segments: Iterable[str]) -> bool:
    """这一行是否是「上一行链接的续行」：只有解压码 / 提取码 / RJ，没有链接。"""
    for segment in segments:
        text = str(segment or "").strip()
        if not text:
            continue
        if _EXTRACT_PASSWORD_LABEL_RE.search(text):
            return True
        if _PASS_CODE_LABEL_RE.search(text):
            return True
        if _RJ_HEAD_RE.match(text):
            return True
    return False


def _group_standard_records(lines: Iterable[str]) -> List[Dict[str, Any]]:
    """把「链接行 + 紧随其后的解压码/RJ 续行」合成一条记录。

    粘贴多行时用户常按「链接一行、解压码一行、RJ 一行」排版；旧实现逐行解析，
    单独的解压码行与 RJ 行既没有链接也没有上下文，会被判成「非规范格式」直接
    退回，导致整段 hint 为空 —— 预览还能靠文件名标签兜底，起任务阶段没有兜底，
    密码就此丢失。这里把续行并入最近的链接行（链接尚未出现时挂到下一行）。
    """
    records: List[Dict[str, Any]] = []
    pending: List[str] = []
    for line in lines:
        segments = _split_segments(line)
        if not segments:
            continue
        if _line_has_url(segments):
            records.append({"segments": pending + segments, "has_url": True})
            pending = []
            continue
        if _segments_are_continuation(segments) and records and records[-1]["has_url"]:
            records[-1]["segments"].extend(segments)
            continue
        if _segments_are_continuation(segments):
            # 链接还没出现：先攒着，等下一行链接一起解析（否则上下文丢失）。
            pending.extend(segments)
            continue
        records.append({"segments": segments, "has_url": False})
    if pending:
        records.append({"segments": pending, "has_url": False})
    return records


def _legacy_line_urls(line: str) -> List[str]:
    """不命中规范格式的行，退回既有识别逻辑（只取链接，不改行为）。"""
    from .netdisk_link_parser import extract_baidu_urls

    try:
        return list(extract_baidu_urls(line))
    except Exception:
        return []


def parse_standard_input(
    text: str,
    *,
    enabled: bool = True,
) -> Optional[Tuple[List[str], Dict[str, Dict[str, str]]]]:
    """解析整段输入。

    返回 ``(urls, hints)``；``hints`` 的键是 :func:`share_identity`。
    整段没有任何一行命中规范格式时返回 ``None``，调用方应完全走原有逻辑。
    """
    if not enabled:
        return None
    lines = [line for line in re.split(r"[\r\n]+", str(text or "")) if line.strip()]
    if not lines:
        return None

    urls: List[str] = []
    hints: Dict[str, Dict[str, str]] = {}
    matched = False

    for record in _group_standard_records(lines):
        segments = list(record.get("segments") or [])
        parsed = _parse_standard_segments(segments)
        if not parsed:
            # 未命中规范格式：整条记录交给既有识别，只取链接、不改行为。
            for url in _legacy_line_urls(" ".join(segments)):
                if url and url not in urls:
                    urls.append(url)
            continue
        matched = True
        url = str(parsed.get("url") or "")
        if url and url not in urls:
            urls.append(url)
        key = share_identity(url)
        if key and key not in hints:
            hints[key] = {
                "rj": str(parsed.get("rj") or ""),
                "extract_password": str(parsed.get("extract_password") or ""),
                "pass_code": str(parsed.get("pass_code") or ""),
            }

    if not matched or not urls:
        return None
    return urls, hints


def _volume_group_signature(name: str) -> Optional[Tuple[int, str, int]]:
    text = str(name or "").strip()
    for kind, (pattern, _start) in enumerate(_VOLUME_GROUP_PATTERNS):
        match = pattern.match(text)
        if match:
            try:
                index = int(match.group("index"))
            except (TypeError, ValueError):
                return None
            return kind, str(match.group("base") or "").lower(), index
    return None


def is_complete_volume_group(names: Iterable[str]) -> bool:
    """纯文件名判定：这一组文件是否是完整连续的分卷。"""
    values = [str(name or "").strip() for name in names if str(name or "").strip()]
    if len(values) < 2:
        return False
    parsed = [_volume_group_signature(name) for name in values]
    if any(item is None for item in parsed):
        return False
    kinds = {item[0] for item in parsed if item}
    bases = {item[1] for item in parsed if item}
    if len(kinds) != 1 or len(bases) != 1:
        return False
    kind = next(iter(kinds))
    start = _VOLUME_GROUP_PATTERNS[kind][1]
    indexes = sorted(item[2] for item in parsed if item)
    return indexes == list(range(start, start + len(indexes)))


def split_display_extension(name: str) -> Tuple[str, str]:
    """把文件名拆成 (主体, 扩展名)，分卷后缀整体算扩展名。"""
    text = str(name or "")
    for pattern in _VOLUME_EXTENSION_PATTERNS:
        match = pattern.match(text)
        if match:
            return match.group("stem"), match.group("ext")
    match = _PLAIN_EXTENSION_RE.match(text)
    if match:
        return match.group("stem"), match.group("ext")
    return text, ""


def build_name_hint(source_name: str, custom_name: str, password: str) -> str:
    """生成预览里「预计下载后名称」展示串。与 _custom_download_relative_path 同形。"""
    source = str(source_name or "").strip()
    custom = str(custom_name or "").strip()
    if custom:
        # 主体用 custom_name，扩展名优先沿用原文件（含分卷后缀）。
        base, custom_extension = split_display_extension(custom)
        _, source_extension = split_display_extension(source) if source else ("", "")
        extension = custom_extension or source_extension
        base = base or custom
    elif source:
        base, extension = split_display_extension(source)
    else:
        return ""
    if not base:
        return ""
    clean_password = str(password or "").strip()
    if clean_password:
        return f"{base}({clean_password}){extension}"
    return f"{base}{extension}"


def extract_password_by_rules(filename: str, rules: Optional[Iterable[str]]) -> str:
    """按用户配置的正则从文件名抽密码；坏正则静默跳过。"""
    name = str(filename or "")
    if not name or not rules:
        return ""
    for pattern in rules:
        text = str(pattern or "").strip()
        if not text:
            continue
        try:
            compiled = re.compile(text, re.IGNORECASE)
        except re.error:
            continue
        match = compiled.search(name)
        if not match:
            continue
        value = match.group(1) if compiled.groups else match.group(0)
        cleaned = _clean_password(value)
        if cleaned:
            return cleaned
    return ""


def _row_name(row: Dict[str, Any]) -> str:
    return str(row.get("name") or row.get("filename") or row.get("remote_name") or "").strip()


def password_from_filename_label(filename: str) -> str:
    """从 ``解压码1021`` 这类标签式文件名里摘密码；没有则空串。

    规范输入行没写解压码段、用户也没配自定义规则时，这是最后一道兜底：
    否则 RJ 改名照常生效，但预计下载名会缺 ``(1021)``。
    """
    try:
        return extract_labeled_password(filename)
    except Exception:  # 兜底逻辑不允许影响预览主流程
        return ""


def _assign_custom_name(row: Dict[str, Any], value: str) -> None:
    """显式设置永远优先：已有 custom_name 就不覆盖。"""
    if str(row.get("custom_name") or row.get("custom_filename") or "").strip():
        return
    row["custom_name"] = str(value or "").strip()


def apply_hints_to_preview(
    preview: Dict[str, Any],
    hints: Optional[Dict[str, Dict[str, str]]],
    *,
    rules: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """把 hints 落进 preview 的 preview_files（幂等，只填空位）。

    写入粒度：**全部写 file 级**。``_baidu_selected_file_overrides``
    (baidu_netdisk_service.py:871) 会读取 preview_files 的 custom_name /
    custom_extract_password 并在行处理前应用，因此不会触发「多文件分享不自动
    套用单文件重命名」，也不会把整组塞进 custom_group_folder 子目录。
    """
    if not isinstance(preview, dict) or not hints:
        return preview
    for item in preview.get("items") or []:
        if not isinstance(item, dict):
            continue
        key = share_identity(str(item.get("share_url") or item.get("url") or ""))
        hint = hints.get(key) if key else None
        if not hint:
            continue
        files = [row for row in list(item.get("preview_files") or []) if isinstance(row, dict)]
        if not files:
            continue
        hint_password = str(hint.get("extract_password") or "").strip()
        hint_rj = str(hint.get("rj") or "").strip()

        # 1) 密码：该分享下每条文件都填（与文件数量、文件名无关）。
        #    取值优先级：规范输入行的解压码段 ＞ 用户自定义正则 ＞ 文件名标签兜底。
        for row in files:
            if str(row.get("custom_extract_password") or row.get("extract_password") or "").strip():
                continue
            row_name = _row_name(row)
            password = (
                hint_password
                or extract_password_by_rules(row_name, rules)
                or password_from_filename_label(row_name)
            )
            if password:
                row["custom_extract_password"] = password

        # 2) 改名：文件名已有 RJ 就跳过（密码照填），用户明确要求。
        names = [_row_name(row) for row in files]
        if hint_rj and not any(extract_rj(name) for name in names):
            if len(files) == 1 or is_complete_volume_group(names):
                for row in files:
                    _assign_custom_name(row, hint_rj)
            else:
                # 多文件非卷组：只给建议，由前端右键手动套用。
                item["rj_name_suggested"] = hint_rj

        # 3) 展示串：密码被 sanitize 剥掉后，前端靠它渲染「预计下载名」。
        for row in files:
            password = str(row.get("custom_extract_password") or "").strip()
            hint_text = build_name_hint(
                _row_name(row),
                str(row.get("custom_name") or "").strip(),
                password,
            )
            if hint_text:
                row["name_hint"] = hint_text

    return preview
