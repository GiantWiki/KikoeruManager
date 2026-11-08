"""Kikoeru 数据库管理服务（v2.6）

直接管理远程 Kikoeru（闭源新版 0.6.x）的 SQLite 数据库文件：
- 只读分页查询（功能1 浏览）
- 带写前快照的 UNC/本地直写事务（功能1 编辑；写后 integrity_check 校验，失败回滚）
- 写入路由：t_work 的 title/circle_id 优先走 Kikoeru 官方 API（POST /api/edit/work/{id}），
  其余或 API 不可达时自动降级 UNC 直写
- 备份（activate/auto/manual/snapshot/pre-restore 五类）与恢复
- 删除带外键预检：r_* 纯关系表先清关联行；其余未声明级联的外键给可读 409
- 功能2：dir 反解 work_name 后批量/单条套用标题

表权限：
- 可编辑：t_work / t_circle / t_tag / t_va / r_tag_work / r_va_work / t_review
- 只读：t_play_histroy / t_translate_task
- 隐藏（API 也拒绝）：t_user / knex_migrations

编码约定：方法内需要配置时显式 ``config = get_config()``。
"""
import gzip
import logging
import json
import os
import re
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config.settings import get_config
from .kikoeru_folder_parser import compile_user_regex, parse_work_name_by_regex, parse_work_name_by_template

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 表权限白名单
TABLE_EDITABLE = {"t_work", "t_circle", "t_tag", "t_va", "r_tag_work", "r_va_work", "t_review"}
# t_track：Kikoeru 0.6.18 新增的逐音频文件轨道表。只读浏览，禁止直写
# （hash/时长由 Kikoeru 自己维护，外部改写会破坏其缓存与总时长汇总）。
TABLE_READONLY = {"t_play_histroy", "t_translate_task", "t_track"}
TABLE_HIDDEN = {"t_user", "knex_migrations"}
# 已知的「可选表」：新版库有、旧版库没有，因此不参与必需表缺失判定。
TABLE_KNOWN_OPTIONAL = {"t_track"}
# 必需表 11 张（不含可选表）；旧版/新版库都应齐备。
TABLE_EXPECTED = (TABLE_EDITABLE | TABLE_READONLY | TABLE_HIDDEN) - TABLE_KNOWN_OPTIONAL
TABLE_KNOWN = TABLE_EXPECTED | TABLE_KNOWN_OPTIONAL

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")

# 进程内 DB 操作互斥：真实 Kikoeru 库为 rollback-journal 模式，同进程的并发
# 备份（长读持 SHARED 锁）与写事务（COMMIT 需 EXCLUSIVE）会互相制造
# "database is locked"。所有备份/写/恢复操作在此锁上串行化；
# 用 RLock 是因为 _write_with_snapshot 内部要重入 create_backup（同线程）。
_DB_OP_LOCK = threading.RLock()

# t_work 中可走 Kikoeru 官方编辑 API（POST /api/edit/work/{id}）的字段映射
# （t_work 列名 → API payload 字段名；API 为整体替换语义，调用方必须带全四项）
_API_EDITABLE_FIELDS = {"title": "title", "circle_id": "circle"}


class KikoeruDbError(Exception):
    """带 HTTP 语义的错误：status 用于 API 层直接映射响应码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class KikoeruDbService:
    """Kikoeru SQLite 数据库管理服务（无持久连接，方法级短连接）。"""

    # ------------------------------------------------------------------ 路径
    @staticmethod
    def _resolve_db_path() -> Path:
        config = get_config()
        raw = str(config.kikoeru_db.db_path or "").strip().strip('"')
        if not raw:
            raise KikoeruDbError("尚未配置 Kikoeru 数据库路径（db_path）", 400)
        path = Path(raw)
        if not path.is_file():
            raise KikoeruDbError(f"数据库文件不存在或不可达: {raw}", 404)
        return path

    @staticmethod
    def _resolve_backup_dir() -> Path:
        config = get_config()
        raw = str(config.kikoeru_db.backup_dir or "").strip()
        if raw:
            base = Path(raw)
        else:
            from ..config.settings import get_config_file_path

            data_path = str(os.environ.get("DATA_PATH") or "").strip()
            if data_path:
                base = Path(data_path) / "kikoeru_db_backups"
            else:
                config_path = Path(get_config_file_path()).resolve()
                data_dir = config_path.parent.parent if config_path.parent.name == "config" else config_path.parent
                base = data_dir / "kikoeru_db_backups"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise KikoeruDbError(f"备份目录不可用: {base} ({exc})", 500)
        return base

    # ------------------------------------------------------------------ 表权限
    @staticmethod
    def _validate_table(table: str, *, need: str = "read") -> str:
        name = str(table or "").strip()
        if not _IDENTIFIER_RE.match(name):
            raise KikoeruDbError(f"非法表名: {table!r}", 400)
        if name in TABLE_HIDDEN:
            raise KikoeruDbError("该表受保护，不可访问", 403)
        if name.lower().startswith("sqlite_"):
            raise KikoeruDbError("SQLite 内部表不可访问", 403)
        # 未登记的新表（Kikoeru 升级新增）允许只读访问；写入仍只放行白名单。
        if need in ("write",) and name not in TABLE_EDITABLE:
            raise KikoeruDbError(f"表 {name} 为只读表，不可修改", 403)
        return name

    @staticmethod
    def _table_mode(table: str) -> str:
        if table in TABLE_EDITABLE:
            return "editable"
        if table in TABLE_READONLY:
            return "readonly"
        if table in TABLE_HIDDEN:
            return "hidden"
        # 未知表按只读暴露，避免新版新增表在页面上整表消失。
        return "readonly"

    # ------------------------------------------------------------------ 连接
    @staticmethod
    def _connect(db_path: Optional[Path] = None, *, readonly: bool = False,
                 busy_timeout_ms: int = 5000) -> sqlite3.Connection:
        path = db_path or KikoeruDbService._resolve_db_path()
        conn = sqlite3.connect(str(path), timeout=max(busy_timeout_ms / 1000, 1.0), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if readonly:
            # query_only 在 SQL 层禁写，比 mode=ro URI 更稳（规避 UNC/URI 兼容问题）
            conn.execute("PRAGMA query_only=ON")
        else:
            conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _columns_of(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [
            {"name": r["name"], "type": (r["type"] or "").upper(), "notnull": bool(r["notnull"]),
             "default": r["dflt_value"], "pk": int(r["pk"] or 0)}
            for r in rows
        ]

    @staticmethod
    def _pk_columns(conn: sqlite3.Connection, table: str) -> List[str]:
        cols = KikoeruDbService._columns_of(conn, table)
        pks = [c for c in cols if c["pk"]]
        pks.sort(key=lambda c: c["pk"])
        return [c["name"] for c in pks] or ["rowid"]

    # ------------------------------------------------------------------ 备份/快照（sqlite3 backup API，WAL 安全）
    @staticmethod
    def create_backup(kind: str = "manual") -> Dict[str, Any]:
        """对当前数据库做一致性备份（sqlite3 backup API，可安全在 Kikoeru 运行时执行）。"""
        if kind not in ("activate", "auto", "manual", "pre-restore", "snapshot"):
            raise KikoeruDbError(f"非法备份类型: {kind}", 400)
        with _DB_OP_LOCK:
            config = get_config()
            if not str(config.kikoeru_db.db_path or "").strip():
                raise KikoeruDbError("尚未配置 Kikoeru 数据库路径（db_path）", 400)
            backup_dir = KikoeruDbService._resolve_backup_dir()
            db_path = KikoeruDbService._resolve_db_path()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = backup_dir / f"{db_path.stem}_{stamp}_{kind}.sqlite3"
            # 先做抢锁探测（sqlite3 backup API 遇 BUSY 会无限重试，必须先快速失败）
            probe = KikoeruDbService._connect(db_path, readonly=False, busy_timeout_ms=2000)
            probe.isolation_level = None
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                raise KikoeruDbError(f"数据库被占用，无法备份（Kikoeru 可能在写入）: {exc}", 423)
            finally:
                probe.close()
            src = KikoeruDbService._connect(db_path, readonly=True)
            try:
                dst = sqlite3.connect(str(target))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            # snapshot 是内部回滚用的高频备份（每次写事务前一份）。t_track 让库
            # 体积翻倍后，未压缩快照会迅速吃满磁盘，这里落盘后立即 gzip。
            # 压缩失败则保留原始 .sqlite3（功能优先于体积）。
            final_path = target
            if kind == "snapshot":
                gz_path = target.parent / f"{target.name}.gz"
                try:
                    with open(target, "rb") as raw, gzip.open(gz_path, "wb", compresslevel=6) as gz:
                        shutil.copyfileobj(raw, gz, length=1024 * 1024)
                    target.unlink()
                    final_path = gz_path
                except OSError:
                    logger.warning("[KIKOERU-DB] snapshot 压缩失败，保留未压缩快照: %s", target, exc_info=True)
                    try:
                        gz_path.unlink()
                    except OSError:
                        pass
            # activate 原始备份只保留 1 份：删除更早的 activate
            if kind == "activate":
                for old in sorted(backup_dir.glob(f"{db_path.stem}_*_activate.sqlite3"))[:-1]:
                    try:
                        old.unlink()
                    except OSError:
                        logger.warning("[KIKOERU-DB] 清理旧 activate 备份失败: %s", old)
            logger.info("[KIKOERU-DB] 备份完成 kind=%s -> %s", kind, final_path)
            return {"filename": final_path.name, "kind": kind, "size": final_path.stat().st_size,
                    "created_at": datetime.fromtimestamp(final_path.stat().st_mtime).isoformat(timespec="seconds")}

    @staticmethod
    def list_backups(kind: Optional[str] = None) -> List[Dict[str, Any]]:
        backup_dir = KikoeruDbService._resolve_backup_dir()
        db_stem = KikoeruDbService._resolve_db_path().stem
        items: List[Dict[str, Any]] = []
        for f in backup_dir.glob("*.sqlite3*"):
            m = re.match(rf"^{re.escape(db_stem)}_\d{{8}}_\d{{6}}_([a-z\-]+)\.sqlite3(?:\.gz)?$", f.name)
            if not m:
                continue
            file_kind = m.group(1)
            if kind and file_kind != kind:
                continue
            stat = f.stat()
            items.append({"filename": f.name, "kind": file_kind, "size": stat.st_size,
                          "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")})
        items.sort(key=lambda x: x["created_at"], reverse=True)
        return items

    @staticmethod
    def cleanup_retention() -> Dict[str, int]:
        """按 retention 清理 auto（backup_retention）与 snapshot（snapshot_retention + 24h）。"""
        config = get_config()
        db_stem = KikoeruDbService._resolve_db_path().stem
        backup_dir = KikoeruDbService._resolve_backup_dir()
        removed = {"auto": 0, "snapshot": 0}

        autos = sorted(
            [f for f in backup_dir.glob(f"{db_stem}_*_auto.sqlite3")],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        for old in autos[max(int(config.kikoeru_db.backup_retention or 7), 1):]:
            try:
                old.unlink()
                removed["auto"] += 1
            except OSError:
                pass

        now_ts = datetime.now().timestamp()
        max_keep = max(int(config.kikoeru_db.snapshot_retention or 20), 1)
        snaps = sorted(
            [f for f in backup_dir.glob(f"{db_stem}_*_snapshot.sqlite3*")],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        for idx, old in enumerate(snaps):
            expired = (now_ts - old.stat().st_mtime) > 24 * 3600
            if idx >= max_keep or expired:
                try:
                    old.unlink()
                    removed["snapshot"] += 1
                except OSError:
                    pass
        return removed

    @staticmethod
    def _table_names(path: Path) -> set:
        """读取库内用户表名集合（排除 sqlite 内部表）。"""
        conn = sqlite3.connect(str(path))
        try:
            return {
                str(r[0]) for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
        finally:
            conn.close()

    @staticmethod
    def restore_backup(filename: str, confirm: bool = False) -> Dict[str, Any]:
        """用指定备份整文件替换当前库。恢复前自动做 pre-restore 备份。

        - 支持普通 .sqlite3 与 snapshot 的 .sqlite3.gz；
        - 跨版本护栏：备份缺少当前库已有的表（例如新版 t_track）时默认拒绝，
          必须由调用方显式 confirm=True 才继续，避免误把新库恢复成旧结构。
        """
        with _DB_OP_LOCK:
            backup_dir = KikoeruDbService._resolve_backup_dir()
            db_path = KikoeruDbService._resolve_db_path()

            safe_name = Path(str(filename or "")).name
            if not re.match(rf"^{re.escape(db_path.stem)}_\d{{8}}_\d{{6}}_[a-z\-]+\.sqlite3(?:\.gz)?$", safe_name):
                raise KikoeruDbError(f"非法备份文件名: {filename}", 400)
            source = backup_dir / safe_name
            if not source.is_file():
                raise KikoeruDbError(f"备份文件不存在: {safe_name}", 404)

            # snapshot 是 gzip 存储，恢复前先解到临时文件
            extract_path: Optional[Path] = None
            check_path = source
            if safe_name.endswith(".gz"):
                extract_path = backup_dir / f".restore_extract_{datetime.now().strftime('%Y%m%d_%H%M%S%f')}.sqlite3"
                try:
                    with gzip.open(source, "rb") as src_gz, open(extract_path, "wb") as raw:
                        shutil.copyfileobj(src_gz, raw, length=1024 * 1024)
                except (OSError, EOFError) as exc:
                    try:
                        extract_path.unlink()
                    except OSError:
                        pass
                    raise KikoeruDbError(f"备份压缩包解压失败，已中止恢复: {exc}", 500) from exc
                check_path = extract_path

            try:
                # 校验备份文件本身可用
                check = sqlite3.connect(str(check_path))
                try:
                    result = check.execute("PRAGMA integrity_check").fetchone()
                    if not result or str(result[0]).lower() != "ok":
                        raise KikoeruDbError(
                            f"备份文件损坏（integrity_check={result[0] if result else 'n/a'}），已中止恢复", 500
                        )
                finally:
                    check.close()

                # 跨版本护栏：备份少表说明会回退结构（如 t_track），默认拒绝
                dropped_tables = sorted(
                    KikoeruDbService._table_names(db_path) - KikoeruDbService._table_names(check_path)
                )
                if dropped_tables and not confirm:
                    raise KikoeruDbError(
                        "该备份缺少当前库已有的表: "
                        f"{', '.join(dropped_tables)}。恢复后这些表的数据会回退"
                        "（Kikoeru 重启可能重建空表，但历史数据需要重新扫描）。"
                        "确认继续请携带 confirm=true 重试。",
                        409,
                    )

                # 校验通过后再拍 pre-restore，避免失败时留下无用备份
                KikoeruDbService.create_backup("pre-restore")

                # 写入临时文件后原子替换；一并清掉旧 -wal/-shm，防止旧 WAL 污染新主库
                tmp = db_path.with_suffix(".sqlite3.restore_tmp")
                shutil.copy2(check_path, tmp)
                os.replace(tmp, db_path)
                for suffix in ("-wal", "-shm"):
                    stale = Path(str(db_path) + suffix)
                    try:
                        if stale.exists():
                            stale.unlink()
                    except OSError:
                        logger.warning("[KIKOERU-DB] 清理旧 %s 失败（建议确认 Kikoeru 状态）", stale.name)

                verify = KikoeruDbService._connect(readonly=True)
                try:
                    row = verify.execute("PRAGMA integrity_check").fetchone()
                    ok = bool(row) and str(row[0]).lower() == "ok"
                finally:
                    verify.close()
                if not ok:
                    raise KikoeruDbError("恢复后 integrity_check 未通过", 500)
                logger.info("[KIKOERU-DB] 恢复完成: %s -> %s", safe_name, db_path)
                return {"restored": safe_name, "integrity": "ok", "dropped_tables": dropped_tables}
            finally:
                if extract_path is not None:
                    try:
                        extract_path.unlink()
                    except OSError:
                        pass

    # ------------------------------------------------------------------ 只读查询
    def query_table(self, table: str, page: int = 1, size: int = 50,
                    search: str = "", sort: str = "") -> Dict[str, Any]:
        table = self._validate_table(table, need="read")
        page = max(int(page or 1), 1)
        size = min(max(int(size or 50), 1), 200)
        offset = (page - 1) * size

        conn = self._connect(readonly=True)
        try:
            columns = self._columns_of(conn, table)
            col_names = [c["name"] for c in columns]
            where_sql, params = "", []
            search = str(search or "").strip()
            if search:
                text_cols = [
                    c["name"] for c in columns
                    if not c["type"] or any(t in c["type"] for t in ("CHAR", "CLOB", "TEXT"))
                ]
                if text_cols:
                    where_sql = " WHERE " + " OR ".join(
                        [f'"{c}" LIKE ?' for c in text_cols]
                    )
                    like = f"%{search}%"
                    params = [like] * len(text_cols)

            order_sql = ""
            sort_field, _, sort_dir = str(sort or "").partition(":")
            if sort_field and sort_field in col_names:
                direction = "DESC" if sort_dir.strip().lower() == "desc" else "ASC"
                order_sql = f' ORDER BY "{sort_field}" {direction}'
            else:
                order_sql = " ORDER BY rowid"

            total = conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"{where_sql}', params).fetchone()["n"]
            rows = conn.execute(
                f'SELECT * FROM "{table}"{where_sql}{order_sql} LIMIT ? OFFSET ?',
                [*params, size, offset],
            ).fetchall()
            return {
                "table": table,
                "mode": self._table_mode(table),
                "columns": columns,
                "rows": [dict(r) for r in rows],
                "total": total,
                "page": page,
                "size": size,
            }
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                raise KikoeruDbError(f"表不存在: {table}", 404) from exc
            raise
        finally:
            conn.close()

    def list_tables(self) -> List[Dict[str, Any]]:
        conn = self._connect(readonly=True)
        try:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()]
        finally:
            conn.close()
        result = []
        for name in sorted(names):
            mode = self._table_mode(name)
            if mode == "hidden":
                continue
            result.append({"name": name, "mode": mode, "known": name in TABLE_KNOWN})
        return result

    # ------------------------------------------------------------------ 写事务（UNC/本地直写，写前快照）
    def _write_with_snapshot(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """写前拍 snapshot 备份 → BEGIN IMMEDIATE 重试 → 写后 integrity_check 校验。

        fn(conn) 在 BEGIN IMMEDIATE 之后的同一连接/事务内执行，异常时 ROLLBACK。
        全程持有进程内 _DB_OP_LOCK：与定时备份/恢复/其它写操作串行，避免
        rollback-journal 模式下同进程读锁挡 COMMIT 的自锁。
        """
        with _DB_OP_LOCK:
            import asyncio as _asyncio

            db_path = self._resolve_db_path()
            # 写前一致性快照（backup API 生成独立完整副本）
            snapshot = self.create_backup("snapshot")

            conn = self._connect(db_path, readonly=False, busy_timeout_ms=5000)
            conn.isolation_level = None  # 手动事务
            try:
                last_err: Optional[Exception] = None
                import time
                for attempt in range(5):
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        last_err = None
                        break
                    except sqlite3.OperationalError as exc:
                        last_err = exc
                        time.sleep(min(0.2 * (2 ** attempt), 2.0))
                if last_err is not None:
                    raise KikoeruDbError(
                        f"数据库被占用（Kikoeru 可能在写入），请稍后重试或先停止 Kikoeru: {last_err}", 423
                    )
                try:
                    result = fn(conn)
                    row = conn.execute("PRAGMA integrity_check").fetchone()
                    if not row or str(row[0]).lower() != "ok":
                        raise KikoeruDbError(f"写入后完整性校验失败: {row[0] if row else 'n/a'}", 500)
                    conn.execute("COMMIT")
                    return result
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            except KikoeruDbError:
                raise
            except sqlite3.Error as exc:
                raise KikoeruDbError(f"数据库写入失败: {exc}", 500)
            finally:
                conn.close()
                try:
                    self.cleanup_retention()
                except Exception:  # noqa: BLE001 - 清理失败不影响写入结果
                    logger.debug("[KIKOERU-DB] 快照清理失败", exc_info=True)

    # ---- 行级写操作（含组合主键支持） ----
    @staticmethod
    def _normalize_bind_value(value: Any) -> Any:
        """sqlite3 不支持 dict/list 绑定：JSON 语义的复合值序列化为字符串兜底。"""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return value

    def _pk_where(self, conn: sqlite3.Connection, table: str, row_id: Any) -> tuple:
        pk_cols = self._pk_columns(conn, table)
        if isinstance(row_id, dict):
            missing = [c for c in pk_cols if c not in row_id]
            if missing:
                raise KikoeruDbError(f"缺少主键字段: {missing}", 400)
            values = [row_id[c] for c in pk_cols]
        else:
            if len(pk_cols) > 1:
                raise KikoeruDbError(f"表 {table} 为组合主键 {pk_cols}，id 需传对象", 400)
            values = [row_id]
        where = " AND ".join([f'"{c}" = ?' for c in pk_cols])
        return where, values

    def update_row(self, table: str, row_id: Any, patch: Dict[str, Any]) -> Dict[str, Any]:
        table = self._validate_table(table, need="write")
        if not patch:
            raise KikoeruDbError("patch 为空", 400)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            columns = {c["name"] for c in self._columns_of(conn, table)}
            unknown = [k for k in patch if k not in columns]
            if unknown:
                raise KikoeruDbError(f"未知字段: {unknown}", 400)
            where, values = self._pk_where(conn, table, row_id)
            set_sql = ", ".join([f'"{k}" = ?' for k in patch])
            bind_values = [self._normalize_bind_value(v) for v in patch.values()]
            cur = conn.execute(
                f'UPDATE "{table}" SET {set_sql} WHERE {where}',
                [*bind_values, *values],
            )
            if cur.rowcount == 0:
                raise KikoeruDbError("未找到目标行", 404)
            return {"table": table, "updated": cur.rowcount}

        return self._write_with_snapshot(_do)

    def insert_row(self, table: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        table = self._validate_table(table, need="write")
        if not payload:
            raise KikoeruDbError("payload 为空", 400)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            columns = {c["name"] for c in self._columns_of(conn, table)}
            unknown = [k for k in payload if k not in columns]
            if unknown:
                raise KikoeruDbError(f"未知字段: {unknown}", 400)
            cols = ", ".join([f'"{k}"' for k in payload])
            marks = ", ".join(["?"] * len(payload))
            cur = conn.execute(
                f'INSERT INTO "{table}" ({cols}) VALUES ({marks})',
                [self._normalize_bind_value(v) for v in payload.values()],
            )
            return {"table": table, "lastrowid": cur.lastrowid, "inserted": cur.rowcount}

        return self._write_with_snapshot(_do)

    # ---- 外键依赖预检（任何表的删除都先查引用方） ----
    def _fetch_row(self, conn: sqlite3.Connection, table: str, row_id: Any) -> Dict[str, Any]:
        where, values = self._pk_where(conn, table, row_id)
        row = conn.execute(f'SELECT * FROM "{table}" WHERE {where}', values).fetchone()
        return dict(row) if row else {}

    @staticmethod
    def _is_junction_table(conn: sqlite3.Connection, table: str) -> bool:
        """判断是否为 r_* 纯关系表（主键由外键列组成，如 r_tag_work / r_va_work）。

        这类行只表达两个实体之间的关联，父行删除后没有任何独立含义，可以在同一
        事务里先清掉。Kikoeru 建库时 r_tag_work 的两个外键都没写 on delete
        cascade，不先清就会 FOREIGN KEY constraint failed。
        """
        if not str(table or "").lower().startswith("r_"):
            return False
        try:
            cols = {str(c["name"]).lower() for c in KikoeruDbService._columns_of(conn, table)}
            fk_cols = {
                str(r["from"]).lower()
                for r in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            }
            pk_cols = {str(c).lower() for c in KikoeruDbService._pk_columns(conn, table)}
        except sqlite3.Error:
            return False
        if not cols or not fk_cols or len(pk_cols) < 2:
            return False
        return pk_cols.issubset(fk_cols) and pk_cols.issubset(cols)

    @staticmethod
    def _inspect_fk_dependents(
        conn: sqlite3.Connection, table: str, parent_row: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """列出所有引用 ``table`` 的外键子表，以及命中该行的行数与 on_delete 动作。

        不依赖写死的表名：Kikoeru 升级新增表、旧库缺表都能正确工作；父列取
        ``PRAGMA foreign_key_list`` 的 ``to``（未声明时按父表主键 seq 对应）。
        """
        if not parent_row:
            return []
        lowered = {str(k).lower(): v for k, v in parent_row.items()}
        parent_pk = KikoeruDbService._pk_columns(conn, table)
        try:
            child_names = [
                str(r["name"])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
        except sqlite3.Error:
            return []

        deps: List[Dict[str, Any]] = []
        for child in child_names:
            if child.lower() == str(table).lower():
                continue
            try:
                fk_rows = conn.execute(f'PRAGMA foreign_key_list("{child}")').fetchall()
            except sqlite3.Error:
                continue

            # 同一 id 的多行是复合外键，必须一起匹配
            groups: Dict[int, List[sqlite3.Row]] = {}
            for fk in fk_rows:
                if str(fk["table"] or "").lower() != str(table).lower():
                    continue
                groups.setdefault(int(fk["id"]), []).append(fk)

            for fk_cols in groups.values():
                fk_cols.sort(key=lambda r: int(r["seq"]))
                pairs: List[tuple] = []
                for fk in fk_cols:
                    from_col = str(fk["from"] or "")
                    to_col = str(fk["to"] or "")
                    if not to_col:
                        idx = int(fk["seq"]) if len(parent_pk) > 1 else 0
                        to_col = parent_pk[idx] if idx < len(parent_pk) else ""
                    value = lowered.get(to_col.lower()) if to_col else None
                    # 外键列含 NULL 时不参与约束，按 SQLite 语义跳过
                    if not from_col or value is None:
                        pairs = []
                        break
                    pairs.append((from_col, value))
                if not pairs:
                    continue

                child_where = " AND ".join([f'"{c}" = ?' for c, _ in pairs])
                try:
                    row = conn.execute(
                        f'SELECT COUNT(*) AS n FROM "{child}" WHERE {child_where}',
                        [v for _, v in pairs],
                    ).fetchone()
                except sqlite3.Error:
                    continue
                count = int(row["n"] if row else 0)
                if count <= 0:
                    continue
                deps.append(
                    {
                        "table": child,
                        "columns": [c for c, _ in pairs],
                        "values": [v for _, v in pairs],
                        "on_delete": str(fk_cols[0]["on_delete"] or "").upper(),
                        "count": count,
                        "junction": KikoeruDbService._is_junction_table(conn, child),
                    }
                )
        deps.sort(key=lambda d: (-int(d["count"]), str(d["table"]).lower()))
        return deps

    @staticmethod
    def _fk_block_message(table: str, blockers: List[Dict[str, Any]]) -> str:
        details = []
        for dep in blockers:
            cols = "、".join(dep["columns"])
            action = dep["on_delete"] or "未声明"
            note = ""
            if str(dep["table"]).lower() == "t_track":
                note = "，t_track 为只读表，需先在 Kikoeru 侧移除该作品或手动清理"
            details.append(f"{dep['table']}.{cols} {dep['count']} 行（ON DELETE {action}{note}）")
        return (
            f"无法删除 {table} 的该行：仍有 {len(blockers)} 类关联记录引用它，"
            "外键不允许级联删除：" + "；".join(details) + "。请先删除或改写这些关联记录后重试。"
        )

    def delete_row(self, table: str, row_id: Any) -> Dict[str, Any]:
        table = self._validate_table(table, need="write")

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            parent_row = self._fetch_row(conn, table, row_id)
            where, values = self._pk_where(conn, table, row_id)
            deps = self._inspect_fk_dependents(conn, table, parent_row)

            cascaded: List[Dict[str, Any]] = []
            blocking: List[Dict[str, Any]] = []
            cleanup: List[Dict[str, Any]] = []
            for dep in deps:
                # 声明了级联动作的，交给 SQLite 自己处理
                if dep["on_delete"] in ("CASCADE", "SET NULL", "SET DEFAULT"):
                    continue
                if dep["junction"]:
                    cleanup.append(dep)
                    continue
                blocking.append(dep)
            # 先判阻断，避免"清了一半再回滚"的无谓写入
            if blocking:
                raise KikoeruDbError(self._fk_block_message(table, blocking), 409)
            for dep in cleanup:
                child_where = " AND ".join([f'"{c}" = ?' for c in dep["columns"]])
                child_cur = conn.execute(
                    f'DELETE FROM "{dep["table"]}" WHERE {child_where}', dep["values"]
                )
                cascaded.append(
                    {
                        "table": dep["table"],
                        "columns": dep["columns"],
                        "deleted": int(child_cur.rowcount or 0),
                    }
                )

            try:
                cur = conn.execute(f'DELETE FROM "{table}" WHERE {where}', values)
            except sqlite3.IntegrityError as exc:
                if "foreign key" not in str(exc).lower():
                    raise
                # 预检没覆盖到的引用（升级新增表、父列名缺失等）回查后给可读结论
                late = [
                    d
                    for d in self._inspect_fk_dependents(conn, table, parent_row)
                    if d["on_delete"] not in ("CASCADE", "SET NULL", "SET DEFAULT")
                ]
                if late:
                    raise KikoeruDbError(self._fk_block_message(table, late), 409) from exc
                raise KikoeruDbError(
                    f"无法删除 {table} 的该行：仍被其他表的外键引用（SQLite: {exc}）", 409
                ) from exc

            if cur.rowcount == 0:
                raise KikoeruDbError("未找到目标行", 404)
            result: Dict[str, Any] = {"table": table, "deleted": cur.rowcount}
            if cascaded:
                result["cascaded"] = cascaded
            if table == "t_work":
                track_dep = next(
                    (d for d in deps if str(d["table"]).lower() == "t_track"), None
                )
                result["related_tracks"] = int(track_dep["count"]) if track_dep else 0
                result["track_on_delete"] = track_dep["on_delete"] if track_dep else None
                result["cache_note"] = "Kikoeru 音频缓存按作品归档且不再自动淘汰，删除作品不会清理已缓存文件。"
            return result

        return self._write_with_snapshot(_do)

    # ------------------------------------------------------------------ 功能2：套用文件命名
    def _parse_dir_for_rename(self, dir_name: str, mode: str = "template",
                              compiled_regex=None) -> Dict[str, Any]:
        """按识别方式反解文件夹名。

        mode=template（默认）：按用户当前重命名模板结构匹配——文件夹名不符合
        模板结构时直接 skipped，绝不靠启发式猜标题，避免把
        「RJ号 [社团]【副标题】正标题」里的社团名当成标题。
        mode=regex：用户自定义正则，第 1 个捕获组（$1）为标题，无捕获组用整体
        匹配；compiled_regex 必须先经 _compile_rename_regex 编译。
        """
        if mode == "regex":
            return parse_work_name_by_regex(dir_name, compiled_regex)
        config = get_config()
        return parse_work_name_by_template(dir_name, config.rename.template)

    @staticmethod
    def _compile_rename_regex(regex: str):
        """编译用户自定义正则；非法/为空时 400（预览阶段就报错，不逐行静默跳过）。"""
        try:
            return compile_user_regex(regex)
        except ValueError as exc:
            raise KikoeruDbError(str(exc), 400) from exc

    def preview_rename(self, ids: Optional[List[Any]] = None, mode: str = "template",
                       regex: str = "") -> Dict[str, Any]:
        compiled = None
        if str(mode or "template") == "regex":
            compiled = self._compile_rename_regex(regex)
        conn = self._connect(readonly=True)
        try:
            if ids:
                marks = ",".join(["?"] * len(ids))
                rows = conn.execute(
                    f'SELECT id, dir, title FROM "t_work" WHERE id IN ({marks})', list(ids)
                ).fetchall()
            else:
                rows = conn.execute('SELECT id, dir, title FROM "t_work"').fetchall()
        finally:
            conn.close()

        items = []
        for r in rows:
            parsed = self._parse_dir_for_rename(r["dir"], mode, compiled)
            changed = bool(parsed["matched"] and parsed["work_name"] and parsed["work_name"] != r["title"])
            items.append({
                "id": r["id"],
                "dir": r["dir"],
                "old_title": r["title"],
                "new_title": parsed["work_name"],
                "rjcode": parsed.get("rjcode"),
                "changed": changed,
                "skipped": not parsed["matched"] or not parsed["work_name"],
                "reason": parsed.get("reason", ""),
            })
        return {"total": len(items), "changed": sum(1 for i in items if i["changed"]),
                "skipped": sum(1 for i in items if i["skipped"]), "items": items}

    def apply_rename(self, ids: Optional[List[Any]] = None, mode: str = "template",
                     regex: str = "") -> Dict[str, Any]:
        """批量套用：单事务内 UPDATE title 且 is_custom_meta=1（防 Kikoeru 重扫描覆盖）。"""
        preview = self.preview_rename(ids, mode=mode, regex=regex)
        targets = [i for i in preview["items"] if i["changed"]]

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            applied = 0
            for item in targets:
                conn.execute(
                    'UPDATE "t_work" SET "title" = ?, "is_custom_meta" = 1 WHERE "id" = ?',
                    (item["new_title"], item["id"]),
                )
                applied += 1
            return {"applied": applied, "skipped": preview["skipped"],
                    "total": preview["total"]}

        result = self._write_with_snapshot(_do)
        logger.info("[KIKOERU-DB] 套用文件命名完成: applied=%s skipped=%s", result["applied"], result["skipped"])
        return result

    def apply_rename_single(self, work_id: Any) -> bool:
        """单条套用（功能3 复用）：仅在模板反解出 work_name 时更新。"""
        conn = self._connect(readonly=True)
        try:
            row = conn.execute('SELECT id, dir, title FROM "t_work" WHERE id = ?', (work_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return False
        parsed = self._parse_dir_for_rename(row["dir"])
        if not (parsed["matched"] and parsed["work_name"]) or parsed["work_name"] == row["title"]:
            return False
        self.update_row("t_work", work_id, {"title": parsed["work_name"], "is_custom_meta": 1})
        return True

    # ------------------------------------------------------------------ 诊断
    def diagnose(self) -> Dict[str, Any]:
        """四步检测：路径可达 → 结构+完整性 → 锁可用 → 副本试写。"""
        steps: List[Dict[str, Any]] = []
        table_report: Dict[str, Any] = {}

        def _step(name: str, ok: bool, detail: str) -> None:
            steps.append({"step": name, "ok": ok, "detail": detail})

        # ① 路径可达
        config = get_config()
        raw = str(config.kikoeru_db.db_path or "").strip().strip('"')
        db_path: Optional[Path] = None
        if not raw:
            _step("路径可达", False, "未配置 db_path")
        else:
            path = Path(raw)
            if path.is_file():
                _step("路径可达", True, str(path))
                db_path = path
            else:
                _step("路径可达", False, f"文件不存在或不可达: {raw}")

        if db_path is None:
            return {"ok": False, "steps": steps}

        # ② 结构 + 完整性
        try:
            conn = self._connect(db_path, readonly=True)
            try:
                all_names = {r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
                names = {n for n in all_names if not str(n).lower().startswith("sqlite_")}
                missing = sorted(TABLE_EXPECTED - names)
                known_new = sorted(TABLE_KNOWN_OPTIONAL & names)
                unknown_new = sorted(names - TABLE_KNOWN)
                table_report.update({
                    "required": len(TABLE_EXPECTED),
                    "total": len(names),
                    "missing": missing,
                    "known_new": known_new,
                    "unknown": unknown_new,
                })
                row = conn.execute("PRAGMA integrity_check").fetchone()
                integrity = str(row[0]).lower() if row else "unknown"
                if missing:
                    _step("结构匹配", False, f"缺少必需表: {missing}（可能为旧版或异常库）")
                else:
                    extras = []
                    if known_new:
                        extras.append(f"新增表 {', '.join(known_new)}")
                    if unknown_new:
                        extras.append(f"未识别表 {', '.join(unknown_new)}")
                    suffix = f"；检测到{'、'.join(extras)}" if extras else ""
                    _step(
                        "结构匹配", True,
                        f"{len(TABLE_EXPECTED)} 张必需表齐全{suffix}，integrity_check={integrity}",
                    )
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            _step("结构匹配", False, f"读取失败: {exc}")

        # ③ 锁可用（BEGIN IMMEDIATE 后立即 ROLLBACK，不动数据）
        try:
            conn = self._connect(db_path, readonly=False, busy_timeout_ms=2000)
            try:
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
                _step("锁可用", True, "BEGIN IMMEDIATE 抢锁成功（当前无写入方占用）")
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            _step("锁可用", False, f"数据库被占用（Kikoeru 可能在扫描/写入）: {exc}")
        except Exception as exc:  # noqa: BLE001
            _step("锁可用", False, f"未知错误: {exc}")

        # ④ 写入能力（拷贝副本到备份目录试写，验证文件系统写权限）
        # 锁不可用时跳过（backup API 遇 BUSY 会无限重试，且占锁状态下试写无意义）
        if not steps[-1]["ok"] and steps[-1]["step"] == "锁可用":
            _step("写入能力", False, "已跳过：数据库被占用，请先释放写锁（如停止 Kikoeru 扫描）")
        else:
            try:
                backup_dir = KikoeruDbService._resolve_backup_dir()
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S%f")
                probe = backup_dir / f"{db_path.stem}_diagnose_{stamp}.sqlite3"
                src = self._connect(db_path, readonly=True)
                try:
                    dst = sqlite3.connect(str(probe))
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                finally:
                    src.close()
                try:
                    test = sqlite3.connect(str(probe))
                    try:
                        test.execute("CREATE TABLE _km_diagnose_probe (v TEXT)")
                        test.execute("INSERT INTO _km_diagnose_probe (v) VALUES ('ok')")
                        got = test.execute("SELECT v FROM _km_diagnose_probe").fetchone()[0]
                        test.execute("DROP TABLE _km_diagnose_probe")
                        test.commit()
                        _step("写入能力", got == "ok", f"副本试写成功: {probe.name}")
                    finally:
                        test.close()
                finally:
                    try:
                        probe.unlink()
                    except OSError:
                        pass
            except Exception as exc:  # noqa: BLE001
                _step("写入能力", False, f"副本试写失败（检查写权限/只读挂载）: {exc}")

        return {"ok": all(s["ok"] for s in steps), "db_path": str(db_path),
                "steps": steps, "tables": table_report}


_service: Optional[KikoeruDbService] = None


def get_kikoeru_db_service() -> KikoeruDbService:
    global _service
    if _service is None:
        _service = KikoeruDbService()
    return _service
