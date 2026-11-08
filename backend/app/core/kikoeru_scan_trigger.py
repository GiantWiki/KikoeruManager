"""归档入库后的 Kikoeru「定向扫描」触发（v2.6 功能4）

背景：Kikoeru 0.6.x 的扫描没有 REST 接口，只能发 socket.io 事件
``PERFORM_SCAN``，payload 是 ``{"rootFolders": [媒体库名, ...]}``（空数组=全库）。
前端「媒体库」多选里的每一项就是媒体库名。

本项目的库存按 RJ 区间分桶（``RJ015xxxxx`` / ``RJ016xxxxx`` …），而每个桶在
Kikoeru 里正好被注册成一个独立媒体库。所以作品落盘后只需要扫它所在的那个桶，
不必每次把 5000+ 条作品的库整体重扫一遍。

解析顺序（都对 ``/api/config/admin`` 的 ``rootFolders`` 求值）：

1. **落盘路径**：把作品父目录的尾部片段与媒体库 ``path`` 的尾部片段做最长匹配。
   容器里的 ``/usr/src/kikoeru/VoiceWork/汉化/RJ016xxxxx`` 与本机看到的
   ``/汉化/RJ016xxxxx`` 前缀不同，但尾部 ``汉化/RJ016xxxxx`` 是一致的。
2. **RJ 号模式**：路径匹配不到时，用媒体库名 ``RJ016xxxxx`` 的位数模式反推
   （前缀位数 + x 位数必须与 RJ 号总位数一致，且唯一命中）。
3. 两者都不中 → 放弃并记日志，**绝不回退成全库扫描**（全库扫描正是要避免的代价）。

触发是「合并 + 防抖」的：一批入库（多 RJ 拆分、连续任务）会合并成一次
``PERFORM_SCAN``；若 Kikoeru 当前正在扫描，会等这一轮结束再发。
"""
import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set

from ..config.settings import get_config

logger = logging.getLogger(__name__)

# 媒体库列表缓存时长：Kikoeru 改媒体库不频繁，避免每次入库都打一次 HTTP
_ROOT_CACHE_TTL_SECONDS = 300.0
# 合并窗口：同一批入库落盘时间可能相差几秒到十几秒
_DEBOUNCE_SECONDS = 15.0
# 等当前扫描结束的上限，超时后仍然发（Kikoeru 侧会排队）
_SCAN_BUSY_WAIT_SECONDS = 180.0

_RJ_CODE_RE = re.compile(r"^(R?V?J)(\d+)$", re.IGNORECASE)
_RJ_BUCKET_RE = re.compile(r"^(R?V?J)(\d+)(x+)$", re.IGNORECASE)


def _split_segments(path: Any) -> List[str]:
    """把 POSIX / Windows 路径统一拆成非空片段。"""
    raw = str(path or "").strip().replace("\\", "/")
    return [seg for seg in raw.split("/") if seg]


class KikoeruScanTrigger:
    """把「作品落盘」翻译成「只扫这个媒体库」的单例。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._roots_cache: List[Dict[str, Any]] = []
        self._roots_cached_at: float = 0.0
        self._pending: Dict[str, str] = {}  # 媒体库名 -> 触发它的 RJ 号（日志用）
        self._worker: Optional[asyncio.Task] = None
        self._tasks: Set[asyncio.Task] = set()
        self._last_result: Dict[str, Any] = {}

    # ------------------------------------------------------------ 对外入口
    def schedule(self, path: str, rjcode: str = "", reason: str = "auto_process") -> None:
        """同步入口：在 async 任务上下文里调用，立即返回，不阻塞主流程。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("[KIKOERU-SCAN] 无运行中的事件循环，跳过定向扫描调度")
            return
        task = loop.create_task(self._schedule_async(path, rjcode, reason))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def trigger_for_path(self, path: str, rjcode: str = "", reason: str = "manual") -> Dict[str, Any]:
        """解析并排队一次定向扫描，返回解析结果（供接口/测试直接等待）。"""
        kb = get_config().kikoeru_db
        if not (kb.enabled and kb.scan_trigger_enabled):
            return {"queued": False, "reason": "disabled", "root_folder": ""}
        name = await self.resolve_root_folder(path, rjcode)
        if not name:
            logger.info(
                "[KIKOERU-SCAN] 作品路径未匹配到 Kikoeru 媒体库，跳过定向扫描: path=%s rj=%s",
                path, rjcode or "-",
            )
            return {"queued": False, "reason": "no_root_folder_match", "root_folder": ""}
        self._pending[name] = rjcode or name
        self._ensure_worker()
        return {"queued": True, "root_folder": name, "rjcode": rjcode}

    def status(self) -> Dict[str, Any]:
        return {
            "pending": sorted(self._pending),
            "worker_running": bool(self._worker and not self._worker.done()),
            "last_result": dict(self._last_result),
            "roots_cached": len(self._roots_cache),
        }

    # ------------------------------------------------------------ 媒体库解析
    async def _load_roots(self, force: bool = False) -> List[Dict[str, Any]]:
        """读取 Kikoeru 的媒体库列表（带 5 分钟缓存）。失败时返回上次缓存。"""
        now = time.monotonic()
        if (
            not force
            and self._roots_cache
            and (now - self._roots_cached_at) < _ROOT_CACHE_TTL_SECONDS
        ):
            return self._roots_cache

        try:
            import aiohttp

            from .kikoeru_duplicate_service import get_kikoeru_service

            service = get_kikoeru_service()
            if not await service._ensure_valid_token():
                logger.info("[KIKOERU-SCAN] 无法获取 Kikoeru JWT，媒体库列表沿用缓存")
                return self._roots_cache
            server = str(service.config.server_url or "").rstrip("/")
            if not server:
                return self._roots_cache
            session = await service._get_session()
            async with session.get(
                f"{server}/api/config/admin",
                headers=service._get_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    logger.info(
                        "[KIKOERU-SCAN] 读取媒体库列表失败 status=%s，沿用缓存", response.status
                    )
                    return self._roots_cache
                payload = await response.json(content_type=None)
            config = (payload or {}).get("config") or {}
            roots = config.get("rootFolders") or []
            if isinstance(roots, list):
                self._roots_cache = [
                    {"name": str(r.get("name") or ""), "path": str(r.get("path") or "")}
                    for r in roots
                    if isinstance(r, dict) and str(r.get("name") or "").strip()
                ]
                self._roots_cached_at = now
        except Exception:
            logger.info("[KIKOERU-SCAN] 读取媒体库列表异常，沿用缓存", exc_info=True)
        return self._roots_cache

    @staticmethod
    def resolve_by_path(path: str, roots: List[Dict[str, Any]]) -> Optional[str]:
        """按落盘路径的尾部片段与媒体库路径做最长后缀匹配。"""
        parent = _split_segments(path)[:-1]
        if not parent:
            return None
        best_depth = 0
        best_name: Optional[str] = None
        ambiguous = False
        for root in roots or []:
            root_segments = _split_segments(root.get("path"))
            if not root_segments:
                continue
            depth = 0
            while (
                depth < len(root_segments)
                and depth < len(parent)
                and root_segments[-1 - depth].casefold() == parent[-1 - depth].casefold()
            ):
                depth += 1
            if depth == 0:
                continue
            name = str(root.get("name") or "").strip()
            if depth > best_depth:
                best_depth, best_name, ambiguous = depth, name, False
            elif depth == best_depth and name != best_name:
                # 两个媒体库路径尾部同样长且都匹配（例如同一桶名挂在两个库下）
                ambiguous = True
        if ambiguous:
            logger.info("[KIKOERU-SCAN] 路径 %s 命中多个同等深度的媒体库，放弃猜测", path)
            return None
        return best_name

    @staticmethod
    def resolve_by_rjcode(rjcode: str, roots: List[Dict[str, Any]]) -> Optional[str]:
        """按媒体库名的 ``RJ016xxxxx`` 位数模式反推（必须唯一命中）。"""
        match = _RJ_CODE_RE.match(str(rjcode or "").strip())
        if not match:
            return None
        prefix_code = match.group(1).upper()
        digits = match.group(2)
        candidates: List[tuple] = []
        for root in roots or []:
            name = str(root.get("name") or "").strip()
            bucket = _RJ_BUCKET_RE.match(name)
            if not bucket:
                continue
            if bucket.group(1).upper() != prefix_code:
                continue
            head, placeholders = bucket.group(2), bucket.group(3)
            if len(head) + len(placeholders) != len(digits):
                continue
            if not digits.startswith(head):
                continue
            candidates.append((len(head), name))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            logger.info("[KIKOERU-SCAN] RJ %s 命中多个同等具体的媒体库，放弃猜测", rjcode)
            return None
        return candidates[0][1]

    async def resolve_root_folder(self, path: str, rjcode: str = "") -> Optional[str]:
        roots = await self._load_roots()
        if not roots:
            return None
        name = self.resolve_by_path(path, roots)
        if name:
            return name
        name = self.resolve_by_rjcode(rjcode, roots)
        if name:
            logger.info(
                "[KIKOERU-SCAN] 路径匹配失败，按 RJ 号反推媒体库: rj=%s -> %s", rjcode, name
            )
        return name

    # ------------------------------------------------------------ 合并 + 发送
    async def _schedule_async(self, path: str, rjcode: str, reason: str) -> None:
        try:
            await self.trigger_for_path(path, rjcode=rjcode, reason=reason)
        except Exception:
            logger.warning("[KIKOERU-SCAN] 定向扫描解析失败: path=%s", path, exc_info=True)

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._worker = loop.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        try:
            # 合并窗口：等一批入库都落盘后再一次性发
            await asyncio.sleep(_DEBOUNCE_SECONDS)
            from .kikoeru_scan_listener import get_kikoeru_scan_listener

            targets = sorted(self._pending)
            works = dict(self._pending)
            self._pending.clear()
            if not targets:
                return
            listener = get_kikoeru_scan_listener()
            if not listener.is_connected():
                logger.info(
                    "[KIKOERU-SCAN] 定向扫描未执行：socket 未连接（目标媒体库 %s）",
                    "、".join(targets),
                )
                self._last_result = {"started": False, "reason": "socket_disconnected", "targets": targets}
                return
            result = await listener.request_scan(
                targets, reason="archive", wait_timeout=_SCAN_BUSY_WAIT_SECONDS
            )
            self._last_result = {**result, "targets": targets, "works": works}
            if result.get("started"):
                logger.info(
                    "[KIKOERU-SCAN] 入库定向扫描已触发: 媒体库=%s 作品=%s",
                    "、".join(targets),
                    "、".join(works.values()),
                )
            else:
                logger.info(
                    "[KIKOERU-SCAN] 入库定向扫描未发出（%s）: %s",
                    result.get("reason") or "未知原因",
                    "、".join(targets),
                )
        except Exception:
            logger.warning("[KIKOERU-SCAN] 定向扫描发送失败", exc_info=True)
        finally:
            self._worker = None
            if self._pending:
                self._ensure_worker()


_trigger: Optional[KikoeruScanTrigger] = None


def get_kikoeru_scan_trigger() -> KikoeruScanTrigger:
    global _trigger
    if _trigger is None:
        _trigger = KikoeruScanTrigger()
    return _trigger
