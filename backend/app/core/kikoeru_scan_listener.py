"""Kikoeru 扫描监听器（v2.6 功能3）

以 socket.io 客户端身份连接新版 Kikoeru（0.6.x）的管理事件流：
- SCAN_TASKS / SCAN_FAILED_TASKS：仅记日志（个别任务失败**不影响**触发）
- SCAN_FINISHED：扫描完成 → 对 created_at 晚于检查点的新增作品自动套用文件命名（功能2）

同一条连接还负责**发出**扫描请求（功能4）：``request_scan(["RJ016xxxxx"])``
即 ``emit("PERFORM_SCAN", {"rootFolders": [...]})``，让入库后只扫落地的那个
媒体库，不必全库重扫。payload 形态取自 Kikoeru 前端 ``scanPayload()``：
全选时 ``rootFolders`` 为空数组（=全库），所以本模块拒绝空列表。

兼容性：新版 Kikoeru 前端捆绑 socket.io-client v2（EIO=3 协议），
因此钉版 python-socketio 4.x + python-engineio 3.x；JWT 只认连接查询串
`?auth_token=`（0.6.19 的 socket 鉴权中间件读 ``opts.query.auth_token``），
同时带 `Authorization: Bearer` 兜底。

断线兜底：socket 不可用时按 scan_poll_interval_minutes 轮询数据库增量，
重连/恢复后自动补处理，检查点推进保证不重不漏。

首次运行只建立基线（checkpoint=当前最大 created_at），不会把存量库整批改名。
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

from ..config.settings import get_config, save_config
from .kikoeru_db_service import KikoeruDbError, get_kikoeru_db_service

logger = logging.getLogger(__name__)

_DB_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_EPOCH = "1970-01-01 00:00:00"


class KikoeruScanListener:
    """Kikoeru 扫描事件监听 + 轮询兜底（单例）"""

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
        self._task: Optional[asyncio.Task] = None
        self._sio = None
        self._connected = False
        self._had_connection = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._processing = False
        self._scan_active = False
        self._last_scan_request_at = ""
        # SCAN_MAIN_LOGS 是累计数组，按文本去重后每轮扫描只记一次关键统计行
        self._logged_main_lines: set = set()

    def _reset_main_log_dedup(self) -> None:
        self._logged_main_lines = set()

    # ------------------------------------------------------------ 生命周期
    def _should_run(self) -> bool:
        config = get_config()
        kb = config.kikoeru_db
        ks = config.kikoeru_server
        return bool(
            kb.enabled
            and kb.scan_listen_enabled
            and ks.enabled
            and str(ks.server_url or "").strip()
        )

    async def start(self):
        if self._task is not None and not self._task.done():
            logger.warning("[KIKOERU-SCAN] 监听已在运行")
            return
        if not self._should_run():
            logger.info("[KIKOERU-SCAN] 未满足启动条件（kikoeru_db.enabled / scan_listen_enabled / kikoeru_server），不启动")
            return
        # PING/PONG 心跳日志（engineio.client INFO）每 25 秒刷屏，静音到 WARNING
        logging.getLogger("engineio.client").setLevel(logging.WARNING)
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name="kikoeru-scan-listener")
        logger.info("[KIKOERU-SCAN] 监听任务已启动")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._connected = False
        logger.info("[KIKOERU-SCAN] 监听已停止")

    async def restart(self):
        await self.stop()
        await self.start()

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_connected(self) -> bool:
        return self._connected

    def is_scanning(self) -> bool:
        return self._scan_active

    async def request_scan(
        self,
        root_folders: list,
        reason: str = "",
        wait_timeout: float = 180.0,
    ) -> dict:
        """向 Kikoeru 发 ``PERFORM_SCAN``，只扫指定媒体库。

        ``root_folders`` 是媒体库**名**（不是路径）。空列表在 Kikoeru 里等于
        「全库扫描」，这里直接拒绝——定向扫描的整个意义就是避免全库重扫。
        当前正在扫描时先等这一轮结束（上限 ``wait_timeout`` 秒）再发，
        避免把两个 PERFORM_SCAN 叠在一起。
        """
        folders = [str(item or "").strip() for item in (root_folders or [])]
        folders = [item for item in folders if item]
        if not folders:
            return {"started": False, "reason": "未指定媒体库（拒绝全库扫描）"}

        sio = self._sio
        if not self._connected or sio is None:
            return {"started": False, "reason": "socket 未连接"}

        waited = 0.0
        deadline = time.monotonic() + max(float(wait_timeout or 0.0), 0.0)
        while self._scan_active and time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            waited += 1.5

        try:
            # 新的一轮扫描：清空累计日志去重表，保证本轮统计行能打出来
            self._reset_main_log_dedup()
            # python-socketio 4.x 的 Client.emit 是同步阻塞的，放到线程里发
            await asyncio.to_thread(sio.emit, "PERFORM_SCAN", {"rootFolders": folders})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[KIKOERU-SCAN] 发送 PERFORM_SCAN 失败: %s", exc)
            return {"started": False, "reason": f"emit 失败: {exc}"}

        self._last_scan_request_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            "[KIKOERU-SCAN] 已请求 Kikoeru 定向扫描（%s）: 媒体库=%s%s",
            reason or "-", "、".join(folders), f"，等待 {int(waited)}s" if waited else "",
        )
        return {
            "started": True,
            "root_folders": folders,
            "waited_seconds": int(waited),
            "requested_at": self._last_scan_request_at,
        }

    # ------------------------------------------------------------ 主循环
    async def _run(self):
        backoff = 5
        # 负偏移：启动后第一次循环立即执行增量处理（"重连/恢复后自动补处理"
        # 的设计意图），否则要等 poll_interval（默认 30 分钟）才第一次跑，
        # 用户扫描后立刻看会以为功能没生效
        last_poll = -10**9
        while True:
            if not self._should_run():
                self._connected = False
                await asyncio.sleep(30)
                continue

            self._had_connection = False
            # 阻塞直到断线（connect + wait 跑在独立线程）
            await self._try_connect()
            logger.info("[KIKOERU-SCAN] socket 断开，转入轮询兜底")

            if not self._had_connection:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
            else:
                backoff = 5

            # 断线期间轮询兜底：按检查点增量处理
            now = asyncio.get_event_loop().time()
            poll_interval = max(int(get_config().kikoeru_db.scan_poll_interval_minutes or 30), 1) * 60
            if now - last_poll >= poll_interval:
                last_poll = now
                try:
                    await self._process_new_works(trigger="poll_fallback")
                except Exception:
                    logger.warning("[KIKOERU-SCAN] 轮询兜底处理失败", exc_info=True)

    # ------------------------------------------------------------ socket 连接
    async def _try_connect(self) -> bool:
        """建立 socket.io(EIO=3) 连接；成功后阻塞等待断线。返回是否连上过。"""
        import socketio  # python-socketio 4.x（EIO=3，已对真实 0.6.15 实例验证可连接）

        from .kikoeru_duplicate_service import get_kikoeru_service

        try:
            kikoeru = get_kikoeru_service()
            if not await kikoeru._ensure_valid_token():
                logger.warning("[KIKOERU-SCAN] 无法获取 Kikoeru JWT，本轮不连接")
                return False
            server = str(kikoeru.config.server_url or "").rstrip("/")
            token = kikoeru.config.api_token
        except Exception:
            logger.warning("[KIKOERU-SCAN] 获取 Kikoeru 连接信息失败", exc_info=True)
            return False

        sio = socketio.Client(logger=False, engineio_logger=False)
        self._sio = sio

        @sio.on("connect")
        def _on_connect():  # noqa: ANN001
            self._connected = True
            self._had_connection = True
            logger.info("[KIKOERU-SCAN] 已连接 Kikoeru 事件流: %s", server)

        @sio.on("disconnect")
        def _on_disconnect():  # noqa: ANN001
            self._connected = False
            self._scan_active = False
            logger.info("[KIKOERU-SCAN] 连接已断开")

        @sio.on("SCAN_TASKS")
        def _on_scan_tasks(data):  # noqa: ANN001
            self._scan_active = True
            try:
                tasks = (data or {}).get("tasks") or []
                logger.info("[KIKOERU-SCAN] Kikoeru 开始扫描，任务数=%s", len(tasks))
            except Exception:
                logger.debug("[KIKOERU-SCAN] SCAN_TASKS payload 解析失败", exc_info=True)

        @sio.on("SCAN_FAILED_TASKS")
        def _on_scan_failed(data):  # noqa: ANN001
            # 个别任务失败只记日志；SCAN_FINISHED 仍会触发，不影响自动套用
            try:
                failed = (data or {}).get("failedTasks") or []
                logger.warning("[KIKOERU-SCAN] 扫描存在失败任务数=%s（不阻断自动套用）", len(failed))
            except Exception:
                logger.debug("[KIKOERU-SCAN] SCAN_FAILED_TASKS payload 解析失败", exc_info=True)

        @sio.on("SCAN_MAIN_LOGS")
        def _on_scan_main_logs(data):  # noqa: ANN001
            # 只挑关键统计行入日志：扫描范围与「共找到 N 个」，用于判断
            # Kikoeru 到底看没看到本次落盘的目录。Kikoeru 每次推的是**累计**
            # 数组，同一条会反复出现，所以按文本去重，一轮扫描只记一次。
            try:
                for item in (data or {}).get("mainLogs") or []:
                    message = str((item or {}).get("message") or "").strip()
                    if not message or message in self._logged_main_lines:
                        continue
                    if "扫描范围" not in message and "共找到" not in message:
                        continue
                    self._logged_main_lines.add(message)
                    logger.info("[KIKOERU-SCAN] Kikoeru: %s", message)
            except Exception:
                logger.debug("[KIKOERU-SCAN] SCAN_MAIN_LOGS payload 解析失败", exc_info=True)

        @sio.on("SCAN_FINISHED")
        def _on_scan_finished(data):  # noqa: ANN001
            self._scan_active = False
            message = str((data or {}).get("message") or "").strip()
            logger.info(
                "[KIKOERU-SCAN] 收到 SCAN_FINISHED%s，开始增量套用文件命名",
                f": {message}" if message else "",
            )
            self._logged_main_lines.clear()
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._process_new_works("scan_finished"), self._loop)

        def _socket_blocking():
            try:
                # EIO=3：0.6.19 的 socket.io 鉴权中间件只认查询串里的 auth_token
                #（前端 App.vue 写的是 ``$socket.io.opts.query.auth_token``）。
                # 早期这里传 token，服务端一律回 connect_error: No auth token，
                # 现场日志里从未出现过一次「已连接」。
                sio.connect(
                    f"{server}/?auth_token={token}",
                    headers={"Authorization": f"Bearer {token}"},
                    transports=["websocket", "polling"],
                )
                sio.wait()
            except Exception as exc:
                # 连接失败原因升级为 WARNING：现场从未成功连上过（9/7 起日志无一次"已连接"），
                # 一直在 5→300s 退避重试，需要 exc 内容定位（JWT/协议/网络）
                logger.warning("[KIKOERU-SCAN] socket 连接/等待结束，退避后重试: %s", exc)
            finally:
                self._connected = False
                self._scan_active = False
                try:
                    if sio.connected:
                        sio.disconnect()
                except Exception:  # noqa: BLE001
                    pass

        try:
            await asyncio.to_thread(_socket_blocking)
        except Exception:
            logger.warning("[KIKOERU-SCAN] socket 线程异常退出", exc_info=True)
        return self._had_connection

    # ------------------------------------------------------------ 增量处理
    async def _process_new_works(self, trigger: str) -> dict:
        if self._processing:
            logger.info("[KIKOERU-SCAN] 上一次增量处理尚未结束，跳过本次触发（%s）", trigger)
            return {"processed": 0, "busy": True}
        self._processing = True
        try:
            config = get_config().kikoeru_db
            service = get_kikoeru_db_service()
            try:
                conn = await asyncio.to_thread(service._connect, None, readonly=True)
            except KikoeruDbError as exc:
                logger.warning("[KIKOERU-SCAN] 数据库不可用，无法增量处理: %s", exc)
                return {"processed": 0, "error": str(exc)}

            checkpoint = str(config.scan_checkpoint or "").strip()
            try:
                if not checkpoint:
                    # 首次运行：只建立基线，绝不批量改动存量库
                    cursor = await asyncio.to_thread(
                        conn.execute, 'SELECT MAX(created_at) AS m FROM "t_work"'
                    )
                    max_row = await asyncio.to_thread(cursor.fetchone)
                    baseline = (max_row["m"] if max_row and max_row["m"] else None) or \
                        datetime.now().strftime(_DB_TIME_FMT)
                    self._save_checkpoint(str(baseline))
                    logger.info("[KIKOERU-SCAN] 首次运行，建立基线检查点: %s", baseline)
                    return {"processed": 0, "baseline": str(baseline)}

                cursor = await asyncio.to_thread(
                    conn.execute,
                    'SELECT id, dir, title, created_at FROM "t_work" WHERE created_at > ?',
                    (checkpoint,),
                )
                rows = await asyncio.to_thread(cursor.fetchall)
            finally:
                conn.close()

            if not rows:
                self._mark_scan_finished(trigger)
                # 空结果也要留日志：让用户能区分「功能没跑」和「没有新内容」
                logger.info(
                    "[KIKOERU-SCAN] 增量处理完成（%s）: 新增=0 checkpoint=%s（无新内容）",
                    trigger, checkpoint,
                )
                return {"processed": 0}

            applied, failed = 0, 0
            new_ids = [r["id"] for r in rows]
            for r in rows:
                try:
                    if await asyncio.to_thread(service.apply_rename_single, r["id"]):
                        applied += 1
                except Exception:
                    failed += 1
                    logger.warning("[KIKOERU-SCAN] 处理作品失败 id=%s", r["id"], exc_info=True)

            # title 重构 + 评分修复（监控链路补全：原先只做文件命名，
            # 新增内容的 title/评分从未做重构校验）
            title_result = {"updated": 0, "unchanged": 0, "missed": 0}
            rating_started = False
            rating_reason = ""
            try:
                title_result = await self._refresh_titles_for_new_works(rows)
            except Exception:
                logger.warning("[KIKOERU-SCAN] title 重构整体失败", exc_info=True)
            try:
                from .kikoeru_rating_fix_service import get_kikoeru_rating_fix_service

                # 用户要求：必须评分异常（NULL/0/≥5）才处理——正常评分的新作品跳过
                abnormal_ids = []
                conn2 = await asyncio.to_thread(service._connect, None, readonly=True)
                try:
                    marks = ",".join(["?"] * len(new_ids))
                    cursor2 = await asyncio.to_thread(
                        conn2.execute,
                        f'SELECT id FROM "t_work" WHERE id IN ({marks}) '
                        "AND (rate_average_2dp IS NULL OR rate_average_2dp = 0 OR rate_average_2dp >= 5)",
                        list(new_ids),
                    )
                    rows2 = await asyncio.to_thread(cursor2.fetchall)
                    abnormal_ids = [row["id"] for row in rows2]
                finally:
                    conn2.close()
                if not abnormal_ids:
                    rating_reason = "新作品评分全部正常"
                    rating_started = False
                else:
                    rating_result = await get_kikoeru_rating_fix_service().start_run(ids=abnormal_ids)
                    rating_started = bool(rating_result.get("started"))
                    rating_reason = str(rating_result.get("reason") or "")
                    if not rating_started:
                        logger.info("[KIKOERU-SCAN] 评分修复未启动: %s", rating_reason)
            except Exception:
                logger.warning("[KIKOERU-SCAN] 评分修复任务启动失败", exc_info=True)
                rating_reason = "启动异常"

            new_checkpoint = max(
                [str(r["created_at"]) for r in rows if r["created_at"]] + [checkpoint]
            )
            self._save_checkpoint(new_checkpoint)
            self._mark_scan_finished(trigger)
            logger.info(
                "[KIKOERU-SCAN] 增量处理完成（%s）: 新增=%s rename_applied=%s rename_failed=%s "
                "title_updated=%s title_missed=%s rating_started=%s%s checkpoint=%s",
                trigger, len(rows), applied, failed,
                title_result.get("updated"), title_result.get("missed"),
                rating_started, (f"（{rating_reason}）" if rating_reason else ""), new_checkpoint,
            )
            return {
                "processed": len(rows), "applied": applied, "failed": failed,
                "title_updated": title_result.get("updated"),
                "rating_started": rating_started,
            }
        finally:
            self._processing = False

    # ------------------------------------------------------------ title 重构
    async def _refresh_titles_for_new_works(self, rows: list) -> dict:
        """对新作品补全空缺 title：从文件夹名按重命名模板反解出作品名写入。

        用户库 title 多为维护好的中文汉化名（非空一律保留）；空缺时用
        dir 反解（模板严格匹配优先，通用形态回退）补 work_name 段，
        不是整条 dir。
        """
        from .kikoeru_folder_parser import parse_work_name_by_template, parse_work_name_from_dir

        config = get_config()
        rename_template = str(config.rename.template or "")
        service = get_kikoeru_db_service()
        updated, unchanged, missed = 0, 0, 0
        conn = await asyncio.to_thread(service._connect, None, readonly=False)
        try:
            for r in rows:
                work_id = r["id"]
                dir_name = str(r["dir"] or "")
                try:
                    current_title = str(r["title"] or "").strip()
                    if current_title:
                        unchanged += 1
                        continue
                    parsed = parse_work_name_by_template(dir_name, rename_template)
                    if not parsed.get("matched"):
                        parsed = parse_work_name_from_dir(dir_name)
                    new_title = str(parsed.get("work_name") or "").strip() if parsed.get("matched") else ""
                    if not new_title:
                        missed += 1
                        continue
                    await asyncio.to_thread(
                        conn.execute,
                        'UPDATE "t_work" SET title = ? WHERE id = ?',
                        (new_title, work_id),
                    )
                    await asyncio.to_thread(conn.commit)
                    updated += 1
                    logger.info(
                        "[KIKOERU-SCAN] title 补全（原为空，按文件夹名反解）: id=%s 新=%s",
                        work_id, new_title[:60],
                    )
                except Exception:
                    missed += 1
                    logger.warning("[KIKOERU-SCAN] title 处理失败 id=%s", work_id, exc_info=True)
        finally:
            conn.close()
        return {"updated": updated, "unchanged": unchanged, "missed": missed}

    # ------------------------------------------------------------ 检查点持久化
    @staticmethod
    def _save_checkpoint(value: str) -> None:
        try:
            save_config({"kikoeru_db": {"scan_checkpoint": str(value)}})
        except Exception:
            logger.warning("[KIKOERU-SCAN] 检查点保存失败", exc_info=True)

    @staticmethod
    def _mark_scan_finished(trigger: str) -> None:
        if trigger != "scan_finished":
            return
        try:
            save_config({"kikoeru_db": {
                "last_scan_finished_at": datetime.now().strftime(_DB_TIME_FMT)
            }})
        except Exception:
            logger.warning("[KIKOERU-SCAN] 扫描完成时间保存失败", exc_info=True)


_listener: Optional[KikoeruScanListener] = None


def get_kikoeru_scan_listener() -> KikoeruScanListener:
    global _listener
    if _listener is None:
        _listener = KikoeruScanListener()
    return _listener
