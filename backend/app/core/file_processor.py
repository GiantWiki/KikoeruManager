"""统一的文件处理服务

整合 Watcher、Upload API、Scan API 三种入口的文件处理逻辑：
1. 等待文件稳定
2. 检测分卷组
3. 处理分卷组（标记所有分卷为已处理）
4. 文件名规范化
5. 创建任务
"""

import os
import re
import time
import asyncio
from pathlib import Path
from typing import Optional, Set, List, Callable
import logging

from ..config.settings import get_config
from ..core.archive_detection import detect_embedded_7z_offset, has_embedded_zip_archive
from ..core.polyglot_detector import find_embedded_archive
from ..core.task_engine import Task, TaskType, get_task_engine
from ..core.password_utils import normalize_password_value
from .deferred_archive_service import get_deferred_archive_service

logger = logging.getLogger(__name__)


class VolumeSet:
    """分卷组信息"""
    def __init__(self, base_name: str, volumes: List[str], volume_type: str, entry_path: Optional[str] = None):
        self.base_name = base_name
        self.volumes = volumes  # 排序后的分卷路径列表
        self.type = volume_type
        self.entry_path = entry_path or (volumes[0] if volumes else "")
        self.is_complete = False


class FileProcessor:
    """统一的文件处理服务

    提供统一的文件入库处理流程，确保：
    - Watcher 监听
    - Upload API 上传
    - Scan API 扫描
    三种入口使用相同的处理逻辑
    """

    def __init__(self):
        self._processed_files: Set[str] = set()  # 已处理文件集合
        # 文件等待稳定超时计数：path -> (连续次数, 最后一次的时间戳)
        self._stability_timeout_counts: dict = {}

    @property
    def config(self):
        """动态获取最新配置"""
        return get_config()

    def _stability_timeout_blacklist_count(self) -> int:
        """连续超时多少次才把文件临时拉黑。"""
        try:
            return max(1, int(self.config.processing.stability_timeout_blacklist_count))
        except Exception:
            return 3

    def _stability_timeout_reset_seconds(self) -> float:
        """超过这么久没有再超时，计数就归零。

        不设归零的话，历史上一次偶发超时会一直累积，攒够次数后这个文件
        就被永久废掉了（用户只能重启进程）。
        """
        try:
            return max(0.0, float(self.config.processing.stability_timeout_reset_seconds))
        except Exception:
            return 3600.0

    def _bump_stability_timeout(self, file_path: str) -> int:
        """累计某路径的连续超时次数，跨过归零窗口则从头计。"""
        now = time.monotonic()
        count, last_ts = self._stability_timeout_counts.get(file_path, (0, now))
        if now - last_ts > self._stability_timeout_reset_seconds():
            count = 0
        count += 1
        self._stability_timeout_counts[file_path] = (count, now)
        return count

    @staticmethod
    def _call_mark_processed(mark_processed, file_path: str, failure: bool) -> None:
        """调用标记回调，兼容只接受 (path) 的旧签名。

        failure=True 时回调应给这条记忆一个更短的 TTL，让偶发失败的文件
        能自动恢复，而不是被永久跳过。
        """
        if not mark_processed:
            return
        try:
            mark_processed(file_path, failure=failure)
        except TypeError as exc:
            if "failure" not in str(exc):
                raise
            mark_processed(file_path)

    @staticmethod
    def _has_active_aria2_sidecar(file_path: str) -> bool:
        return os.path.isfile(f"{file_path}.aria2")

    @staticmethod
    def _zip_volume_base_name(file_path: str) -> str:
        filename = os.path.basename(file_path)
        match = re.fullmatch(r'(?P<base>.+)\.(?:z\d{2}|zi)', filename, re.IGNORECASE)
        return match.group('base') if match else ""

    @classmethod
    def _has_zip_volume_main(cls, file_path: str) -> bool:
        """判断 .zNN 是否有对应的标准 ZIP 主卷。"""
        base_name = cls._zip_volume_base_name(file_path)
        if not base_name:
            return False
        return os.path.isfile(os.path.join(os.path.dirname(file_path), f"{base_name}.zip"))

    @classmethod
    def _has_zip_volume_companion(cls, file_path: str) -> bool:
        """判断 .zNN/.zi 是否属于一个可识别的 ZIP 分卷组。

        标准组是 ``X.zip + X.z01``；下载源也会产生无标准主卷的
        ``X.z01 + X.z02`` 或被截断的 ``X.z01 + X.zi``。只有同目录确实存在
        组关系时才跳过非首卷，避免误吞单体伪装包（如单独一个 ``X.z77``）。
        """
        base_name = cls._zip_volume_base_name(file_path)
        if not base_name:
            return False
        directory = os.path.dirname(file_path)
        filename = os.path.basename(file_path)
        is_truncated_main = filename.casefold().endswith(".zi")
        if not is_truncated_main:
            if os.path.isfile(os.path.join(directory, f"{base_name}.zip")):
                return True
            if os.path.isfile(os.path.join(directory, f"{base_name}.zi")):
                return True
        try:
            entries = os.listdir(directory)
        except OSError:
            return False
        pattern = re.compile(rf'^{re.escape(base_name)}\.z\d{{2}}$', re.IGNORECASE)
        return any(
            entry.casefold() != filename.casefold() and pattern.fullmatch(entry)
            for entry in entries
        )

    @staticmethod
    def _inherit_download_password_metadata(file_path: str, engine) -> dict:
        """把下载工作台绑定到具体本地文件的解压密码传给监听器任务。

        HTTP / 百度下载任务和监听器导入任务是两条独立队列；仅靠文件名嗅探会在
        重命名、无后缀压缩包或服务重启后丢失用户指定密码。这里只复制密码候选，
        不把下载 URL、Cookie 等下载元数据带入导入任务。
        """
        target = os.path.normcase(os.path.abspath(str(file_path or "")))
        if not target:
            return {}
        passwords: list[str] = []
        seen: set[str] = set()

        def add(raw) -> None:
            value = normalize_password_value(raw)
            if value and value not in seen:
                seen.add(value)
                passwords.append(value)

        def matches(item: dict, metadata: dict) -> bool:
            paths = [item.get("local_path"), item.get("final_path")]
            relative_path = str(item.get("relative_path") or "").strip()
            download_root = str(
                item.get("download_root") or metadata.get("download_root") or metadata.get("final_output_path") or ""
            ).strip()
            if relative_path and download_root:
                paths.append(os.path.join(download_root, relative_path.replace("/", os.sep)))
            return any(
                path and os.path.normcase(os.path.abspath(str(path))) == target
                for path in paths
            )

        for download_task in engine.get_all_tasks(include_hidden=True):
            if download_task.type not in {TaskType.HTTP_DOWNLOAD, TaskType.BAIDU_NETDISK_DOWNLOAD}:
                continue
            metadata = dict(download_task.task_metadata or {})
            candidates = list(metadata.get("download_files") or []) + list(metadata.get("selected_items") or [])
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                if matches(item, metadata):
                    add(item.get("custom_extract_password") or item.get("extract_password"))
                    if not passwords:
                        for selected in list(metadata.get("selected_items") or []):
                            if isinstance(selected, dict) and matches(selected, metadata):
                                add(selected.get("custom_extract_password") or selected.get("extract_password"))
                    if passwords:
                        return {
                            "manual_retry_passwords": passwords,
                            "manual_retry_password": passwords[0],
                            "manual_retry_password_only": True,
                            "manual_retry_password_requested": True,
                        }
        return {}

    # ========== 公共接口 ==========

    async def process_file(
        self,
        file_path: str,
        auto_classify: bool = True,
        wait_stable: bool = True,
        max_wait: int = 300,
        max_total_wait: Optional[float] = None,
        is_processed: Optional[Callable[[str], bool]] = None,
        mark_processed: Optional[Callable[..., None]] = None,
        pause_fn: Optional[Callable[[], None]] = None,
        resume_fn: Optional[Callable[[], None]] = None,
        task_metadata: Optional[dict] = None,
        batch_context: Optional[dict] = None,
        report: Optional[dict] = None,
    ) -> Optional[Task]:
        """统一的文件处理流程

        Args:
            file_path: 文件路径
            auto_classify: 是否自动分类
            wait_stable: 是否等待文件稳定
            max_wait: 无进展超时（秒），文件还在变化就不会触发
            max_total_wait: 等待文件稳定的绝对总时长上限（秒）
            is_processed: 检查文件是否已处理的回调
            mark_processed: 标记文件为已处理的回调，可接收 failure 关键字参数
            pause_fn: 暂停文件监听的回调（用于重命名操作）
            resume_fn: 恢复文件监听的回调

        Returns:
            创建的任务对象，如果未创建任务则返回 None
        """
        logger.debug(f"[FileProcessor] 开始处理文件: {file_path}")
        original_path = file_path

        try:
            if self._has_active_aria2_sidecar(file_path):
                logger.info("[FileProcessor] 检测到 aria2 未完成标记，跳过处理: %s", file_path)
                return None

            # 1. 检查文件是否已处理
            # 先检查持久化归档声明，不能先走文件名规范化，否则可能改掉队列冻结的源路径。
            if await get_deferred_archive_service().is_source_claimed(file_path):
                logger.info("[FileProcessor] 文件已由空闲归档队列声明，跳过重复入库: %s", file_path)
                self._call_mark_processed(mark_processed, file_path, failure=False)
                if isinstance(report, dict):
                    report["skipped_deferred_archive_count"] = int(report.get("skipped_deferred_archive_count") or 0) + 1
                return None
            if is_processed and is_processed(file_path):
                logger.debug(f"[FileProcessor] 文件已处理，跳过: {file_path}")
                if isinstance(report, dict):
                    report["skipped_processed_count"] = int(report.get("skipped_processed_count") or 0) + 1
                return None

            # 2. 等待文件稳定
            if wait_stable:
                logger.debug(f"[FileProcessor] 等待文件稳定: {file_path}")
                try:
                    await self.wait_file_stable(
                        file_path, max_wait=max_wait, max_total_wait=max_total_wait
                    )
                    logger.debug(f"[FileProcessor] 文件已稳定: {file_path}")
                    self._stability_timeout_counts.pop(file_path, None)
                except TimeoutError:
                    # 超时意味着文件已经停滞（不再增长）这么久。仍不立即拉黑：
                    # 同一路径累计若干次后才按"失败"短期记忆，避免无限重试刷屏。
                    # 计数带归零窗口，历史偶发超时不会永久累积。
                    count = self._bump_stability_timeout(file_path)
                    blacklist_at = self._stability_timeout_blacklist_count()
                    if count >= blacklist_at:
                        logger.error(
                            "[FileProcessor] 文件连续 %d 次等待稳定超时，按失败短期记忆跳过: %s",
                            count, file_path,
                        )
                        self._call_mark_processed(mark_processed, file_path, failure=True)
                        self._stability_timeout_counts.pop(file_path, None)
                    else:
                        logger.error(
                            "[FileProcessor] 等待文件稳定超时（第 %d/%d 次，文件可能仍被占用/写入中），"
                            "本次跳过且不标记已处理，后续事件可重试: %s",
                            count, blacklist_at, file_path,
                        )
                    return None
                if self._has_active_aria2_sidecar(file_path):
                    logger.info("[FileProcessor] 文件稳定后仍存在 aria2 未完成标记，跳过处理: %s", file_path)
                    return None

            # 3. 检测分卷组
            volume_set = self.detect_volume_set(file_path)
            if volume_set:
                logger.info(f"[FileProcessor] 检测到分卷组: {volume_set.base_name}, 共 {len(volume_set.volumes)} 个分卷")
                file_path = await self._process_volume_set(
                    file_path, volume_set,
                    is_processed=is_processed,
                    mark_processed=mark_processed
                )
                if not file_path:
                    return None
            else:
                # 检查是否可能是分卷文件但未检测到完整组
                file_path = await self._handle_potential_volume(
                    file_path,
                    is_processed=is_processed,
                    mark_processed=mark_processed
                )
                if not file_path:
                    return None

            # 4. 文件名规范化（需要暂停监听以避免重复事件）
            file_path = await self._normalize_file(
                file_path,
                pause_fn=pause_fn,
                resume_fn=resume_fn,
                mark_processed=mark_processed
            )
            logger.debug(f"[FileProcessor] 规范化后路径: {file_path}")

            # 5. 检查是否已在任务队列中
            engine = get_task_engine()
            if await get_deferred_archive_service().is_source_claimed(file_path):
                logger.info("[FileProcessor] 文件已由空闲归档队列声明，跳过重复入库: %s", file_path)
                self._call_mark_processed(mark_processed, file_path, failure=False)
                if isinstance(report, dict):
                    report["skipped_deferred_archive_count"] = int(report.get("skipped_deferred_archive_count") or 0) + 1
                return None
            existing = any(
                t.source_path == file_path and t.status.value in ["pending", "processing"]
                for t in engine.get_all_tasks()
            )
            if existing:
                logger.debug(f"[FileProcessor] 文件已在任务队列中: {file_path}")
                self._call_mark_processed(mark_processed, file_path, failure=False)
                if isinstance(report, dict):
                    report["skipped_duplicate_count"] = int(report.get("skipped_duplicate_count") or 0) + 1
                return None

            # 6. 创建任务
            logger.debug(f"[FileProcessor] 创建任务: {file_path}")
            merged_metadata = dict(task_metadata or {})
            inherited_password_metadata = self._inherit_download_password_metadata(file_path, engine)
            if inherited_password_metadata:
                merged_metadata.update(inherited_password_metadata)
            if isinstance(batch_context, dict):
                merged_metadata.update({
                    "batch_id": str(batch_context.get("batch_id") or "").strip() or None,
                    "session_id": str(batch_context.get("session_id") or batch_context.get("batch_id") or "").strip() or None,
                    "batch_title": str(batch_context.get("batch_title") or "").strip() or None,
                    "batch_label": str(batch_context.get("batch_label") or "").strip() or None,
                    "batch_source_page": str(batch_context.get("source_page") or "").strip() or None,
                    "batch_source_action": str(batch_context.get("source_action") or "").strip() or None,
                    "batch_source_label": str(batch_context.get("source_label") or "").strip() or None,
                    "batch_requested_count": int(batch_context.get("requested_count") or 0),
                    "batch_log_parent": bool(batch_context.get("log_parent")),
                })
                if batch_context.get("source_page"):
                    merged_metadata.setdefault("source_page", batch_context.get("source_page"))
                if batch_context.get("source_action"):
                    merged_metadata.setdefault("source_action", batch_context.get("source_action"))
                if batch_context.get("source_label"):
                    merged_metadata.setdefault("source_label", batch_context.get("source_label"))
            task = Task(
                task_type=TaskType.AUTO_PROCESS,
                source_path=file_path,
                auto_classify=auto_classify,
                metadata=merged_metadata,
            )

            await engine.submit(task)
            logger.info(
                "[FileProcessor] 任务已提交: task_id=%s source=%s",
                task.id,
                os.path.basename(file_path),
            )
            if isinstance(report, dict):
                report["created_count"] = int(report.get("created_count") or 0) + 1

            # 标记文件为已处理
            self._call_mark_processed(mark_processed, file_path, failure=False)

            return task

        except Exception as e:
            logger.error(f"[FileProcessor] 处理文件失败: {file_path}, {e}", exc_info=True)
            self._call_mark_processed(mark_processed, original_path, failure=True)
            return None

    async def process_directory(
        self,
        directory: str,
        auto_classify: bool = True,
        is_processed: Optional[Callable[[str], bool]] = None,
        mark_processed: Optional[Callable[[str], None]] = None,
        pause_fn: Optional[Callable[[], None]] = None,
        resume_fn: Optional[Callable[[], None]] = None,
        task_metadata: Optional[dict] = None,
        batch_context: Optional[dict] = None,
        report: Optional[dict] = None,
    ) -> List[Task]:
        """扫描目录并处理所有文件

        Args:
            directory: 目录路径
            auto_classify: 是否自动分类
            is_processed: 检查文件是否已处理的回调
            mark_processed: 标记文件为已处理的回调
            pause_fn: 暂停文件监听的回调（用于重命名操作）
            resume_fn: 恢复文件监听的回调

        Returns:
            创建的任务列表
        """
        logger.info(f"[FileProcessor] 开始扫描目录: {directory}")
        tasks = []

        if not os.path.exists(directory):
            logger.warning(f"[FileProcessor] 目录不存在: {directory}")
            return tasks

        # 收集所有待处理的压缩包
        archive_files = []
        for root, dirs, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)

                # 检查是否已处理
                if is_processed and is_processed(file_path):
                    continue

                if await get_deferred_archive_service().is_source_claimed(file_path):
                    continue

                # 检查是否是压缩包
                if self.is_archive(file_path):
                    # 检查文件大小
                    try:
                        file_size = os.path.getsize(file_path)
                        if file_size < 1024:  # 小于1KB，可能正在复制中
                            logger.warning(f"[FileProcessor] 文件太小，跳过: {file_path} ({file_size} bytes)")
                            continue
                    except OSError:
                        continue

                    archive_files.append(file_path)

        logger.info(f"[FileProcessor] 找到 {len(archive_files)} 个待处理文件")
        if isinstance(report, dict):
            report["requested_count"] = len(archive_files)

        # 记录已处理的分卷文件，避免重复创建任务
        processed_volumes: Set[str] = set()

        for file_path in archive_files:
            # 检查是否是已处理的分卷
            if file_path in processed_volumes:
                continue

            # 检测分卷组
            volume_set = self.detect_volume_set(file_path)
            if volume_set:
                # 标记所有分卷为已处理（仅用于本次扫描的内存记录，不调用 mark_processed）
                # mark_processed 会在 process_file 成功创建任务后调用
                for vol in volume_set.volumes:
                    processed_volumes.add(vol)

            # 处理文件（不等待稳定，因为扫描时文件应该已经稳定）
            task = await self.process_file(
                file_path,
                auto_classify=auto_classify,
                wait_stable=False,
                is_processed=is_processed,
                mark_processed=mark_processed,
                pause_fn=pause_fn,
                resume_fn=resume_fn,
                task_metadata=task_metadata,
                batch_context=batch_context,
                report=report,
            )
            if task:
                tasks.append(task)

        logger.info(f"[FileProcessor] 创建了 {len(tasks)} 个任务")
        return tasks

    def has_active_download_sidecar(self, file_path: str) -> bool:
        """文件是否仍在下载中（存在活跃的 aria2 控制文件）。

        watcher 在 is_archive 返回 False 时会走分卷标记路径，必须先排除
        "下载中导致的暂时 False"，否则首卷会被永久标记已处理、永不建任务。
        """
        return self._has_active_aria2_sidecar(file_path)

    def is_archive(self, file_path: str) -> bool:
        """检测是否是压缩包文件

        复用 ArchiveHandler._is_archive 逻辑，但作为独立方法提供

        Args:
            file_path: 文件路径

        Returns:
            是否是压缩包（True/False），如果是非首卷分卷文件返回 False
        """
        if self._has_active_aria2_sidecar(file_path):
            logger.debug("[FileProcessor] 跳过 aria2 下载中的文件: %s", file_path)
            return False

        filename = Path(file_path).name.lower()
        ext = Path(file_path).suffix.lower()

        # 先检查是否是分卷文件后缀，只有真正的主执行文件才创建任务。
        # .zNN 既可能是 ZIP 分卷，也可能只是把单体 7z 伪装成了 z77 等后缀；
        # 只有同目录确实存在组关系时才把非首卷跳过。无标准 .zip 的
        # X.z01 + X.z02 / X.z01 + X.zi 也要由首卷统一建任务。
        z_match = re.search(r'\.z(\d{2})$', filename)
        if z_match and self._has_zip_volume_main(file_path):
            logger.debug(f"[FileProcessor] 跳过 ZIP 分卷文件，等待 *.zip 主文件: {filename}")
            return False
        if z_match and self._has_zip_volume_companion(file_path) and int(z_match.group(1)) > 1:
            logger.debug(f"[FileProcessor] 跳过无标准主卷的 ZIP 非首卷，等待 .z01: {filename}")
            return False
        # .zi 是截断的 ZIP 主卷命名；存在 X.z01 时由 .z01 统一建任务。
        if ext == '.zi' and self._has_zip_volume_companion(file_path):
            logger.debug(f"[FileProcessor] 跳过截断 ZIP 主卷，等待 .z01: {filename}")
            return False

        # 旧式 RAR 分卷: .r00, .r01, ...，应该由 *.rar 主文件触发处理
        rar_old_match = re.search(r'\.r(\d{2})$', filename)
        if rar_old_match:
            logger.debug(f"[FileProcessor] 跳过旧式 RAR 分卷文件，等待 *.rar 主文件: {filename}")
            return False

        # 自解压分卷（国产 SFX 工具）: .exe + .e01, .e02, ... 的 .eNN 部分
        # 由 *.exe 主文件触发处理；仅当同目录下确有同名 *.exe 时才视为分卷，
        # 避免误吞与压缩包无关的 .e01/.e02 杂项文件。
        exe_e_match = re.fullmatch(r'(?P<base>.+)\.e\d{2}', filename, re.IGNORECASE)
        if exe_e_match:
            base_name = exe_e_match.group('base')
            sibling_exe = os.path.join(os.path.dirname(file_path), f"{base_name}.exe")
            if os.path.exists(sibling_exe):
                logger.debug(
                    f"[FileProcessor] 跳过自解压分卷非首卷文件，等待 *.exe 主文件: {filename}"
                )
                return False

        # 7z 分卷: .7z.001, .7z.002, ... (.7z.001 是首卷)
        sevenzip_match = re.search(r'\.7z\.(\d{3})$', filename)
        if sevenzip_match:
            vol_num = int(sevenzip_match.group(1))
            if vol_num > 1:  # .7z.002, .7z.003... 是非首卷
                logger.debug(f"[FileProcessor] 跳过 7z 分卷非首卷文件: {filename}")
                return False
            # .7z.001 是首卷，通过魔数检测
            return self._detect_archive_by_magic(file_path)

        # RAR/ZIP 分卷: .part2.rar, .part3.rar, ... (非首卷)
        part_match = re.search(r'\.part(\d+)\.(rar|zip|7z|exe)$', filename, re.IGNORECASE)
        if part_match and int(part_match.group(1)) > 1:
            logger.debug(f"[FileProcessor] 跳过分卷压缩的非首卷文件: {filename}")
            return False
        # RAR 分卷: .part2, .part3, ... (无扩展名格式，非首卷)
        # 注意: .part1 是首卷，需要通过魔数检测来识别
        part_match_no_ext = re.search(r'\.part(\d+)$', filename, re.IGNORECASE)
        if part_match_no_ext:
            part_num = int(part_match_no_ext.group(1))
            if part_num > 1:
                logger.debug(f"[FileProcessor] 跳过无扩展名的分卷压缩非首卷文件: {filename}")
                return False
            else:
                # .part1 首卷，通过魔数检测确定是否是压缩包
                return self._detect_archive_by_magic(file_path)

        # 常见压缩格式
        archive_extensions = {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.exe'}

        # 检查后缀
        if ext in archive_extensions:
            # 检查是否是自解压文件
            if ext == '.exe':
                archive_keywords = ['rar', 'zip', '7z', 'archive', 'setup', 'install', 'self-extract']
                if any(keyword in filename for keyword in archive_keywords):
                    return True
            return True

        # 对于没有后缀名或后缀名不在列表中的文件，尝试通过魔数检测
        if not ext or ext not in archive_extensions:
            if self._detect_archive_by_magic(file_path):
                return True
            if has_embedded_zip_archive(file_path):
                logger.info(f"[FileProcessor] 检测到带前缀伪装的 ZIP 压缩包: {file_path}")
                return True
            if detect_embedded_7z_offset(file_path) is not None:
                logger.info(f"[FileProcessor] 检测到带前缀伪装的 7z 压缩包: {file_path}")
                return True
            embedded = find_embedded_archive(file_path)
            if embedded is not None:
                logger.info(
                    f"[FileProcessor] 检测到 polyglot 内嵌压缩包: {file_path} "
                    f"(type={embedded[0]} offset={embedded[1]})"
                )
                return True
            return False

    def detect_volume_set(self, file_path: str) -> Optional[VolumeSet]:
        """检测分卷组

        复用 ExtractService._detect_volume_set 逻辑

        Args:
            file_path: 文件路径（通常是首卷或潜在的分卷文件）

        Returns:
            VolumeSet 对象，如果不是分卷文件则返回 None
        """
        directory = os.path.dirname(file_path)
        filename = os.path.basename(file_path)

        zip_main_match = re.search(r'^(?P<base>.+)\.zip$', filename, re.IGNORECASE)
        zip_part_match = re.search(r'^(?P<base>.+)\.z\d{2}$', filename, re.IGNORECASE)
        zip_truncated_match = re.search(r'^(?P<base>.+)\.zi$', filename, re.IGNORECASE)
        if zip_main_match or zip_part_match or zip_truncated_match:
            base_name = (zip_main_match or zip_part_match or zip_truncated_match).group('base')
            volume_set = self._build_zip_volume_set(directory, base_name)
            if volume_set:
                logger.info(f"[FileProcessor] 检测到 ZIP 分卷组: {base_name}")
                return volume_set

        rar_main_match = re.search(r'^(?P<base>.+)\.rar$', filename, re.IGNORECASE)
        rar_part_match = re.search(r'^(?P<base>.+)\.r\d{2}$', filename, re.IGNORECASE)
        if rar_main_match or rar_part_match:
            base_name = (rar_main_match or rar_part_match).group('base')
            volume_set = self._build_rar_old_volume_set(directory, base_name)
            if volume_set:
                logger.info(f"[FileProcessor] 检测到旧式 RAR 分卷组: {base_name}")
                return volume_set

        # 自解压 .exe + .eNN 国产 SFX 分卷组（如 新建压缩.exe + 新建压缩.e01 + .e02 ...）
        exe_main_match = re.search(r'^(?P<base>.+)\.exe$', filename, re.IGNORECASE)
        exe_part_match = re.search(r'^(?P<base>.+)\.e\d{2}$', filename, re.IGNORECASE)
        if exe_main_match or exe_part_match:
            base_name = (exe_main_match or exe_part_match).group('base')
            volume_set = self._build_exe_e_volume_set(directory, base_name)
            if volume_set:
                logger.info(f"[FileProcessor] 检测到自解压分卷组(.exe + .eNN): {base_name}")
                return volume_set

        # 分卷模式识别（按优先级排序，更具体的模式在前）
        # WinRAR 自解压分卷首卷常用 .part1.exe，后续卷继续用 .partN.rar/.exe，
        # 这里把 .exe 一并纳入 partN 模式，避免首卷被当成普通 SFX 单体解压。
        patterns = [
            (r'\.7z\.(\d{3})$', '7z_volume_with_ext'),  # .7z.001, .7z.002 (7z分卷，带.7z扩展名)
            (r'\.part(\d+)\.(rar|zip|7z|exe)$', 'part'),
            (r'\.part(\d+)$', 'part_no_ext'),  # 无扩展名的RAR分卷格式
            (r'\.(\d{3})$', '7z_volume'),  # 纯数字分卷（如 .001, .002）
            (r'\.(\d{2})$', 'generic'),
        ]

        for pattern, vtype in patterns:
            match = re.search(pattern, filename, re.IGNORECASE)
            if match:
                # 提取 base_name，只移除分卷后缀
                base_name = re.sub(pattern, '', filename)
                logger.debug(f"[FileProcessor] 检测到分卷模式: {filename}, base_name={base_name}")

                # 查找所有分卷
                volumes = self._find_all_volumes(directory, base_name, pattern)
                logger.debug(f"[FileProcessor] 找到 {len(volumes)} 个分卷: {[os.path.basename(v) for v in volumes]}")

                # 分卷组需要至少2个文件
                if len(volumes) > 1:
                    return VolumeSet(base_name, volumes, vtype, entry_path=volumes[0])

        return None

    async def wait_file_stable(
        self,
        file_path: str,
        max_wait: int = 300,
        max_total_wait: Optional[float] = None,
    ):
        """等待文件稳定（委托 file_stability 共享实现，含 Windows 占用软放行）

        size+mtime 双维度稳定判定 + open 探测 1 字节检测锁定；
        Windows 上文件被下载器/杀软占用时 PermissionError 会以 WARNING
        日志可见并软放行，不再静默重置到超时。

        Args:
            file_path: 文件路径
            max_wait: 无进展超时（秒），只要文件还在变化就不会触发
            max_total_wait: 绝对总时长上限（秒），为 None 时取 max_wait 的 6 倍

        Raises:
            TimeoutError: 等待超时
        """
        from .file_stability import wait_file_stable_robust

        await wait_file_stable_robust(
            file_path,
            max_wait=max_wait,
            required_stable_checks=3,
            check_interval=2.0,
            log_prefix="[FileProcessor]",
            max_total_wait=max_total_wait,
        )

    # ========== 私有方法 ==========

    async def _process_volume_set(
        self,
        file_path: str,
        volume_set: VolumeSet,
        is_processed: Optional[Callable[[str], bool]] = None,
        mark_processed: Optional[Callable[[str], None]] = None
    ) -> Optional[str]:
        """处理分卷压缩组

        1. 等待所有分卷稳定
        2. 标记所有分卷为已处理
        3. 返回首卷路径

        Args:
            file_path: 当前文件路径
            volume_set: 分卷组信息
            is_processed: 检查文件是否已处理的回调
            mark_processed: 标记文件为已处理的回调

        Returns:
            首卷路径，如果处理失败返回 None
        """
        logger.info(f"[FileProcessor] 处理分卷组: {volume_set.base_name}")

        # 标记所有分卷为已处理
        for volume in volume_set.volumes:
            if mark_processed and not (is_processed and is_processed(volume)):
                mark_processed(volume)

        # 等待所有分卷稳定
        for volume in volume_set.volumes:
            if volume != file_path:
                try:
                    await self.wait_file_stable(volume, max_wait=300)
                    logger.debug(f"[FileProcessor] 分卷已稳定: {volume}")
                except TimeoutError:
                    logger.error(f"[FileProcessor] 等待分卷稳定超时: {volume}")
                    return None

        return volume_set.entry_path or file_path

    async def _handle_potential_volume(
        self,
        file_path: str,
        is_processed: Optional[Callable[[str], bool]] = None,
        mark_processed: Optional[Callable[[str], None]] = None
    ) -> Optional[str]:
        """处理潜在的分卷文件

        检查文件名是否匹配分卷模式，如果是则等待其他分卷出现

        Args:
            file_path: 文件路径
            is_processed: 检查文件是否已处理的回调
            mark_processed: 标记文件为已处理的回调

        Returns:
            文件路径，如果不是分卷文件或已处理返回 None
        """
        part_patterns = [
            r'\.part(\d+)\.(rar|zip|7z|exe)$',  # 带扩展名的分卷（含 WinRAR SFX 首卷 .part1.exe）
            r'\.part(\d+)$',                  # 无扩展名的分卷
            r'\.r\d{2}$',                     # 旧式 RAR 分卷
            r'\.7z\.\d{3}$',                  # 7z 分卷首尾格式
        ]
        basename = os.path.basename(file_path)
        main_volume_patterns = [
            r'^.+\.part1(?:\.(rar|zip|7z|exe))?$',
            r'^.+\.7z\.001$',
        ]

        is_zip_volume = bool(
            re.search(r'\.z\d{2}$', basename, re.IGNORECASE)
            and self._has_zip_volume_companion(file_path)
        )
        is_truncated_zip_main = bool(
            basename.lower().endswith('.zi')
            and self._has_zip_volume_companion(file_path)
        )
        is_potential_volume = any(
            re.search(p, basename, re.IGNORECASE)
            for p in part_patterns
        ) or any(
            re.search(p, basename, re.IGNORECASE)
            for p in main_volume_patterns
        ) or is_zip_volume or is_truncated_zip_main

        if is_potential_volume:
            logger.debug(f"[FileProcessor] 检测到可能是分卷文件，等待其他分卷: {basename}")
            # 等待一段时间让其他分卷文件出现
            await asyncio.sleep(10)

            # 重新检测分卷组
            volume_set = self.detect_volume_set(file_path)
            if volume_set:
                logger.debug(f"[FileProcessor] 等待后检测到分卷组: {volume_set.base_name}")
                return await self._process_volume_set(
                    file_path, volume_set,
                    is_processed=is_processed,
                    mark_processed=mark_processed
                )
            else:
                # 孤立非首卷分卷成员（首卷迟迟不出现）直接跳过，避免每个分卷各
                # 产生一个独立任务、各写一条 ProcessedArchive 记录。包括：
                # - .zXX / .rXX：明显非主卷
                # - .7z.NNN 中 NNN != 001：非首卷 7z 分卷
                # - .partN（N>=2）：非首卷 RAR 多卷成员
                if re.search(r'\.(z\d{2}|zi|r\d{2})$', basename, re.IGNORECASE):
                    logger.warning(f"[FileProcessor] 分卷主文件尚未出现，暂不创建任务: {basename}")
                    return None
                seven_z_member = re.search(r'\.7z\.(\d{3})$', basename, re.IGNORECASE)
                if seven_z_member and int(seven_z_member.group(1)) != 1:
                    logger.warning(
                        f"[FileProcessor] 7z 分卷首卷 (.7z.001) 尚未出现，暂不创建任务: {basename}"
                    )
                    return None
                part_member = re.search(r'\.part(\d+)(?:\.(?:rar|zip|7z|exe))?$', basename, re.IGNORECASE)
                if part_member and int(part_member.group(1)) >= 2:
                    logger.warning(
                        f"[FileProcessor] 分卷首卷 (.part1) 尚未出现，暂不创建任务: {basename}"
                    )
                    return None
                logger.debug(f"[FileProcessor] 等待后仍未检测到分卷组，作为普通文件处理: {basename}")
                return file_path

        return file_path

    async def _normalize_file(
        self,
        file_path: str,
        pause_fn: Optional[Callable[[], None]] = None,
        resume_fn: Optional[Callable[[], None]] = None,
        mark_processed: Optional[Callable[[str], None]] = None
    ) -> str:
        """规范化文件名

        调用 ExtractService 进行文件名规范化

        Args:
            file_path: 文件路径
            pause_fn: 暂停文件监听的回调（用于重命名操作）
            resume_fn: 恢复文件监听的回调
            mark_processed: 标记文件为已处理的回调

        Returns:
            规范化后的文件路径
        """
        from .extract_service import ExtractService

        extract_service = ExtractService()

        # 直接调用 normalize_archive_filename，它会处理所有情况
        # 包括：文件名规范化、添加缺失的后缀、分卷文件处理等

        # 暂停监听（避免重命名触发重复事件）
        if pause_fn:
            pause_fn()

        try:
            normalized_path = await extract_service.normalize_archive_filename(file_path)

            if normalized_path != file_path:
                logger.debug(f"[FileProcessor] 文件已规范化: {file_path} -> {normalized_path}")
                # 标记新路径为已处理
                if mark_processed:
                    mark_processed(normalized_path)

            return normalized_path
        finally:
            # 恢复监听
            if resume_fn:
                resume_fn()

    def _build_zip_volume_set(self, directory: str, base_name: str) -> Optional[VolumeSet]:
        try:
            entries = os.listdir(directory)
        except Exception as exc:
            logger.error(f"[FileProcessor] 查找 ZIP 分卷失败: {exc}")
            return None
        by_casefold = {str(name).casefold(): str(name) for name in entries}

        def existing(name: str) -> str:
            return os.path.join(directory, by_casefold.get(name.casefold(), name))

        zip_path = existing(f"{base_name}.zip")
        has_zip_main = os.path.isfile(zip_path)
        zi_path = existing(f"{base_name}.zi")
        has_truncated_main = os.path.isfile(zi_path)

        z_pattern = re.compile(rf'^{re.escape(base_name)}\.z(\d{{2}})$', re.IGNORECASE)
        z_volumes: List[tuple[int, str]] = []
        for file in entries:
            match = z_pattern.fullmatch(file)
            if match:
                z_volumes.append((int(match.group(1)), os.path.join(directory, file)))
        z_volumes.sort(key=lambda item: item[0])

        # 1. 标准 WinRAR ZIP 分卷 (.zXX)：X.zip + X.z01 + X.z02 + ...
        if has_zip_main and z_volumes:
            ordered = [path for _, path in z_volumes] + [zip_path]
            ordered = sorted(ordered, key=self._volume_sort_key)
            return VolumeSet(base_name, ordered, 'zip_volume_main', entry_path=zip_path)

        # 2. 无标准 .zip 主卷的伪装/截断分卷：
        #    X.z01 + X.z02 + ...，或 X.z01 + X.zi。
        #    只认从 01 开始的连续序号；真正是否可解压仍由 ExtractService 的
        #    魔数与尺寸闸门决定，本层不自动改源文件名。
        if not has_zip_main and z_volumes:
            indices = [index for index, _ in z_volumes]
            if indices == list(range(1, len(indices) + 1)):
                ordered = [path for _, path in z_volumes]
                if has_truncated_main:
                    ordered.append(zi_path)
                if len(ordered) > 1:
                    volume_type = (
                        'zip_truncated_main'
                        if has_truncated_main and len(z_volumes) == 1
                        else 'zip_disguised_volumes'
                    )
                    logger.info(
                        f"[FileProcessor] 检测到无标准主卷的 ZIP 分卷组: {base_name}, "
                        f"volumes={[os.path.basename(p) for p in ordered]}"
                    )
                    return VolumeSet(base_name, ordered, volume_type, entry_path=ordered[0])

        if not has_zip_main:
            return None

        # 3. 非标准 .zip 主卷 + .NNN 纯数字分卷：X.zip + X.002 + X.003 + ...
        #    7-Zip / 国内分卷工具创建多卷时首卷 .001 被改名为 .zip 留下的格式。
        #    后续 remap 流程在 ExtractService 里完成，这里仅识别为分卷组。
        numeric_volumes: List[str] = []
        for file in entries:
            if re.fullmatch(rf'{re.escape(base_name)}\.\d{{3}}', file, re.IGNORECASE):
                numeric_volumes.append(os.path.join(directory, file))

        if numeric_volumes:
            def _numeric_key(path: str) -> int:
                match = re.search(r'\.(\d{3})$', os.path.basename(path))
                return int(match.group(1)) if match else 0

            ordered = [zip_path] + sorted(numeric_volumes, key=_numeric_key)
            logger.info(
                f"[FileProcessor] 检测到 .zip + .NNN 非标准分卷组: {base_name}, "
                f"volumes={[os.path.basename(p) for p in ordered]}"
            )
            return VolumeSet(base_name, ordered, 'zip_numeric_split', entry_path=zip_path)

        return None

    def _build_exe_e_volume_set(self, directory: str, base_name: str) -> Optional[VolumeSet]:
        """构建自解压 .exe + .eNN 分卷组。

        触发条件：同名 .exe 必须存在，且至少有一个 .eNN 伴随文件。
        否则视为普通单体 SFX，由 7z 自行处理。
        """
        exe_path = os.path.join(directory, f"{base_name}.exe")
        if not os.path.exists(exe_path):
            return None

        try:
            siblings = os.listdir(directory)
        except Exception as exc:
            logger.error(f"[FileProcessor] 查找自解压分卷失败: {exc}")
            return None

        e_volumes: List[tuple] = []
        e_pattern = re.compile(rf'^{re.escape(base_name)}\.e(\d{{2}})$', re.IGNORECASE)
        for file in siblings:
            match = e_pattern.fullmatch(file)
            if match:
                e_volumes.append((int(match.group(1)), os.path.join(directory, file)))

        if not e_volumes:
            return None

        e_volumes.sort(key=lambda item: item[0])
        ordered = [exe_path] + [path for _, path in e_volumes]
        return VolumeSet(base_name, ordered, 'exe_e_sequence', entry_path=exe_path)

    def _build_rar_old_volume_set(self, directory: str, base_name: str) -> Optional[VolumeSet]:
        rar_path = os.path.join(directory, f"{base_name}.rar")
        if not os.path.exists(rar_path):
            return None

        volumes = [rar_path]
        try:
            for file in os.listdir(directory):
                if re.fullmatch(rf'{re.escape(base_name)}\.r\d{{2}}', file, re.IGNORECASE):
                    volumes.append(os.path.join(directory, file))
        except Exception as exc:
            logger.error(f"[FileProcessor] 查找旧式 RAR 分卷失败: {exc}")
            return None

        if len(volumes) <= 1:
            return None

        ordered = sorted(volumes, key=self._volume_sort_key)
        return VolumeSet(base_name, ordered, 'rar_volume_main', entry_path=rar_path)

    def _volume_sort_key(self, path: str):
        filename = os.path.basename(path).lower()

        part_match = re.search(r'\.part(\d+)(?:\.(?:rar|zip|7z|exe))?$', filename, re.IGNORECASE)
        if part_match:
            return (0, int(part_match.group(1)), filename)

        sevenzip_match = re.search(r'\.7z\.(\d{3})$', filename, re.IGNORECASE)
        if sevenzip_match:
            return (1, int(sevenzip_match.group(1)), filename)

        pure_numeric_match = re.search(r'\.(\d{3})$', filename, re.IGNORECASE)
        if pure_numeric_match:
            return (2, int(pure_numeric_match.group(1)), filename)

        zip_split_match = re.search(r'\.z(\d{2})$', filename, re.IGNORECASE)
        if zip_split_match:
            return (3, int(zip_split_match.group(1)), filename)

        rar_old_match = re.search(r'\.r(\d{2})$', filename, re.IGNORECASE)
        if rar_old_match:
            return (4, int(rar_old_match.group(1)), filename)

        if filename.endswith('.zip'):
            return (5, 0, filename)
        if filename.endswith('.rar'):
            return (5, 1, filename)

        two_digit_match = re.search(r'\.(\d{2})$', filename, re.IGNORECASE)
        if two_digit_match:
            return (6, int(two_digit_match.group(1)), filename)

        return (9, 0, filename)

    def _find_all_volumes(self, directory: str, base_name: str, pattern: str) -> List[str]:
        """查找所有分卷文件

        Args:
            directory: 目录路径
            base_name: 基础文件名
            pattern: 分卷模式正则

        Returns:
            排序后的分卷文件路径列表
        """
        volumes = []
        try:
            files = os.listdir(directory)
            logger.debug(f"[FileProcessor] _find_all_volumes: directory={directory}, base_name={base_name}, pattern={pattern}")
            logger.debug(f"[FileProcessor] 目录中的文件: {files}")
            for file in files:
                if file.startswith(base_name) and re.search(pattern, file, re.IGNORECASE):
                    volumes.append(os.path.join(directory, file))
                    logger.debug(f"[FileProcessor] 匹配到分卷: {file}")
        except Exception as e:
            logger.error(f"[FileProcessor] 列出目录失败: {e}")

        result = sorted(volumes, key=self._volume_sort_key)
        logger.debug(f"[FileProcessor] 最终分卷列表: {[os.path.basename(v) for v in result]}")
        return result

    def _detect_archive_by_magic(self, path: str) -> bool:
        """通过文件魔数检测是否为压缩文件

        Args:
            path: 文件路径

        Returns:
            是否是压缩文件
        """
        # 定义压缩文件的魔数
        magic_bytes = {
            b'PK\x03\x04': 'zip',  # ZIP
            b'PK\x05\x06': 'zip',  # 空 ZIP
            b'PK\x07\x08': 'zip',  # ZIP64
            b'Rar!': 'rar',        # RAR
            b'7z\xBC\xAF\x27\x1C': '7z',  # 7Z
            b'\x1f\x8b': 'gz',      # GZIP
            b'BZh': 'bz2',         # BZIP2
            b'\xFD7zXZ': 'xz',     # XZ
        }

        try:
            if not os.path.exists(path) or not os.path.isfile(path):
                return False

            file_size = os.path.getsize(path)
            if file_size < 4:
                return False

            with open(path, 'rb') as f:
                header = f.read(8)

            for magic, file_type in magic_bytes.items():
                if header.startswith(magic):
                    logger.debug(f"[FileProcessor] 通过魔数检测到压缩文件: {path} (类型: {file_type})")
                    return True

            return False
        except (PermissionError, IOError) as e:
            logger.debug(f"[FileProcessor] 无法读取文件进行魔数检测: {path}, 错误: {e}")
            return False
        except Exception as e:
            logger.warning(f"[FileProcessor] 魔数检测失败: {path}, 错误: {e}")
            return False


# 全局 FileProcessor 实例
_file_processor: Optional[FileProcessor] = None


def get_file_processor() -> FileProcessor:
    """获取 FileProcessor 实例"""
    global _file_processor
    if _file_processor is None:
        _file_processor = FileProcessor()
    return _file_processor
