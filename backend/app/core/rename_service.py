import asyncio
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

from ..config.settings import get_config
from ..core.task_engine import Task
from .ttl_cache import TTLCache

logger = logging.getLogger(__name__)

_TEMPLATE_FIELD_SPACE_RE = re.compile(r"[\s\u00a0\u2000-\u200b\u202f\u205f\u3000\u2423]+")

# 标题开头冗余 RJ 号前缀：允许前面有包裹性括号、后面跟分隔符（空格/下划线/连字符/点/中点）
_LEADING_RJ_PREFIX_RE = re.compile(
    r"^[\s\[\(（【]*((?i:RJ)\d{4,})[\]\)）】]*[\s_\-.·•]*"
)


def normalize_template_maker_name(raw_value: str) -> str:
    """只规整空白；社团名里的标点和方括号必须原样保留。"""
    return _TEMPLATE_FIELD_SPACE_RE.sub(" ", str(raw_value or "")).strip()


def strip_leading_rjcode(title: str, rjcode: str) -> str:
    """去掉标题开头冗余的 RJ 号前缀（与当前作品同号时才剥离）。

    部分库存的 work_name / 文件夹名是「RJ号 + 标题」或「[RJ号] 标题」格式，
    直接当 work_name 喂给 AI 翻译会把 RJ 号原样带回译名，模板里已有 {rjcode}
    时会渲染成「[RJ..][RJ.. 标题]」。开头是其它 RJ 号（合集标题）时不动。
    剥完只剩空串时返回空串，由调用方决定回退。
    """
    text = str(title or "").strip()
    code = str(rjcode or "").strip().upper()
    if not text or not code:
        return text
    m = _LEADING_RJ_PREFIX_RE.match(text)
    if not m:
        return text
    if m.group(1).upper() != code:
        return text
    return text[m.end():].strip()


def build_ai_title_folder_name(rjcode: str, title: str, metadata: Optional[dict] = None) -> str:
    """生成 AI 标题汉化后的项目文件夹名。

    开启 rename.api_rename_follow_template 时按用户配置的重命名模板渲染
    （work_name 用传入的汉化标题，其余变量取 metadata，通常来自 work_metadata），
    保证项目文件夹格式与正常重命名链路一致；
    未开启或模板渲染结果为空时回退为「RJ号 + 标题」，保证 RJ 标识不丢失。
    """
    clean = re.sub(r'[<>:"/\\|?*]', '', str(title or '')).strip()
    if not clean:
        return ''
    # 标题开头带同号 RJ 前缀时先剥离，避免模板 {rjcode}+{work_name} 渲染出 [RJ..][RJ.. 标题]
    clean = strip_leading_rjcode(clean, rjcode)
    try:
        cfg = get_config()
        if getattr(cfg.rename, 'api_rename_follow_template', False):
            service = RenameService()
            meta = dict(metadata or {})
            meta['rjcode'] = str(rjcode or '').strip()
            meta['work_name'] = clean
            rendered = service._sanitize_filename(service._compile_name(meta)).strip(' .')
            # 元数据缺失时模板可能渲染出空段（如 [][RJ..][标题]），清掉空括号段保持格式整洁
            rendered = re.sub(r'\[\s*\]|【\s*】|\(\s*\)|（\s*）', '', rendered)
            rendered = re.sub(r'\s{2,}', ' ', rendered).strip(' .')
            if rendered:
                return rendered
            logger.warning("[AI标题] 模板渲染文件夹名为空，回退 RJ号+标题: rjcode=%s", rjcode)
    except Exception:
        logger.warning("[AI标题] 按模板生成文件夹名失败，回退 RJ号+标题: rjcode=%s", rjcode, exc_info=True)
    match = re.search(r"RJ(\d{4,})", str(rjcode or ''), re.IGNORECASE)
    if match and not re.search(r"RJ\d{4,}", clean, re.IGNORECASE):
        return f"RJ{match.group(1)} {clean}".strip()
    return clean


class RenameService:
    """重命名服务"""

    def __init__(self):
        # 不缓存配置，每次都获取最新配置
        # 因为 save_config 会创建新的 AppConfig 对象
        # 原裸 dict 每个 RJ 永久驻留；TTL+LRU 限制上限，避免长期运行下内存持续增长
        self._japanese_metadata_cache: TTLCache = TTLCache(max_size=512, ttl_seconds=86400, name="rename.jp_metadata")

    @property
    def config(self):
        """动态获取最新配置"""
        return get_config()

    async def rename(self, path: str, task: Task):
        """
        重命名文件夹
        """
        metadata = task.task_metadata
        logger.info(f"重命名服务 - 原始路径: {path}")
        logger.debug(
            "重命名服务 - 任务元数据字段: keys=%s rjcode=%s",
            sorted((metadata or {}).keys()),
            str((metadata or {}).get("rjcode") or ""),
        )

        if not metadata:
            raise Exception("缺少元数据，无法重命名")

        fallback_rjcode = str(
            metadata.get('rjcode')
            or metadata.get('inferred_rjcode')
            or getattr(task, 'rjcode', '')
            or ''
        ).strip().upper()
        if fallback_rjcode:
            metadata['rjcode'] = fallback_rjcode

        if not metadata.get('rjcode'):
            raise Exception(f"元数据中缺少RJ号，无法重命名。可用字段: {list(metadata.keys())}")

        task.update_progress(60, "重命名文件夹")

        # 生成新名称（日语元数据 + 降级保护 + 空段清理都在这里面）
        new_name = await self.resolve_target_name(metadata, progress=task.update_progress)
        logger.info(f"重命名服务 - 生成的新名称: {new_name}")
        
        # 获取目标路径
        dir_path = Path(path)
        parent = dir_path.parent
        new_path = parent / new_name
        
        # 如果名称相同，跳过
        if dir_path.name == new_name:
            logger.info(f"重命名服务 - 名称相同，跳过重命名: {new_name}")
            return path
        
        # 处理重名 - 只有当目标路径与当前路径不同时才添加后缀
        if new_path.exists() and new_path != dir_path:
            # 检查是否是同一个文件夹（大小写不同的情况）
            if new_path.resolve() == dir_path.resolve():
                # 只是大小写不同，直接重命名
                pass
            else:
                # 真正的重名冲突，添加后缀
                logger.warning(f"重命名服务 - 发现同名文件夹: {new_path}，这可能导致重复")
                counter = 1
                original_new_path = new_path
                while new_path.exists() and new_path.resolve() != dir_path.resolve():
                    new_name_with_suffix = f"{original_new_path.name}({counter})"
                    new_path = parent / new_name_with_suffix
                    counter += 1
                    if counter > 100:  # 防止无限循环
                        logger.error(f"重命名服务 - 无法找到可用的名称，跳过重命名")
                        return path
                logger.info(f"重命名服务 - 使用新名称避免冲突: {new_path.name}")
        
        # 执行重命名
        await asyncio.to_thread(shutil.move, str(dir_path), str(new_path))
        logger.info(f"重命名: {dir_path} -> {new_path}")

        return str(new_path)

    async def resolve_target_name(self, metadata: dict, progress=None) -> str:
        """按重命名模板算出目标文件夹名（只算名字，不动文件系统）。

        两处兜底与 ``rename`` 共用：
        - ``metadata_source == 'minimal'`` 时 work_name 是从解压临时目录名猜出来的，
          可能带 task_id 片段和随机串，不参与渲染，只保留 RJ 号；
        - 变量为空留下的 ``[]`` / ``【】`` / ``()`` 空段统一清掉，
          清完什么都不剩时回退 RJ 号，避免产出空目录名。
        """
        japanese_metadata = None
        if self.config.rename.use_japanese_metadata:
            if callable(progress):
                progress(61, "获取日语元数据")
            japanese_metadata = await self._get_japanese_metadata(metadata.get('rjcode'))
            if japanese_metadata:
                japanese_maker_name = str(japanese_metadata.get('maker_name') or '').strip()
                if japanese_maker_name:
                    metadata['classification_maker_name'] = japanese_maker_name
                    metadata['original_maker_name'] = japanese_maker_name

        template_metadata = metadata
        if str(metadata.get('metadata_source') or '').strip().lower() == 'minimal':
            # 降级元数据的 work_name 是解压目录名残留，绝不能写进库存目录名。
            template_metadata = {**metadata, 'work_name': ''}
            logger.warning(
                "[RENAME] 元数据为降级兜底(minimal)，忽略不可信 work_name，仅按 RJ 号命名: "
                "rjcode=%s work_name=%s",
                metadata.get('rjcode'),
                metadata.get('work_name'),
            )

        new_name = self._clean_empty_template_segments(
            self._compile_name(template_metadata, japanese_metadata)
        )
        new_name = self._sanitize_filename(new_name)
        if not new_name.strip(' ._-'):
            fallback = str(metadata.get('rjcode') or '').strip()
            logger.warning("[RENAME] 模板渲染结果为空，回退 RJ 号: %s", fallback)
            new_name = fallback
        return new_name

    @staticmethod
    def _clean_empty_template_segments(name: str) -> str:
        """清掉模板变量为空留下的空括号段，顺带压掉多余空白。"""
        cleaned = re.sub(r'\[\s*\]|【\s*】|\(\s*\)|（\s*）', '', str(name or ''))
        cleaned = re.sub(r'\s{2,}', ' ', cleaned)
        return cleaned.strip()

    async def _get_japanese_metadata(self, rjcode: str) -> Optional[dict]:
        """
        获取日语元数据（带缓存）

        Args:
            rjcode: RJ号

        Returns:
            日语元数据字典
        """
        # 检查缓存
        if rjcode in self._japanese_metadata_cache:
            return self._japanese_metadata_cache[rjcode]

        # 从 MetadataService 获取
        from .metadata_service import MetadataService
        metadata_service = MetadataService()

        japanese_metadata = await metadata_service.fetch_japanese_metadata(rjcode)

        # 缓存结果
        self._japanese_metadata_cache[rjcode] = japanese_metadata

        return japanese_metadata

    def _flatten_single_subfolder(
        self,
        path: str,
        operation_sink: Optional[list[dict[str, str]]] = None,
    ) -> str:
        """
        扁平化单一层级文件夹
        递归检查所有子文件夹，如果某个文件夹只有一个子文件夹（没有文件或其他内容），
        则将子文件夹内容移出。支持配置扁平化深度。
        """
        root_path = Path(path)
        max_depth = self.config.rename.flatten_depth

        def flatten_single_path(current_path: Path, current_depth: int) -> bool:
            """
            对单个路径进行扁平化，返回是否执行了扁平化
            current_depth: 当前已经扁平化的层数
            """
            if current_depth >= max_depth:
                return False

            if not current_path.is_dir():
                return False

            try:
                # 获取当前目录的所有内容
                items = list(current_path.iterdir())

                # 如果只有一个项目且是文件夹，则扁平化
                if len(items) == 1 and items[0].is_dir():
                    subfolder = items[0]
                    if operation_sink is not None:
                        try:
                            parent_relative = current_path.relative_to(root_path).as_posix()
                        except ValueError:
                            parent_relative = ""
                        operation_sink.append({
                            "parent_relative_path": "" if parent_relative == "." else parent_relative,
                            "removed_segment": subfolder.name,
                        })
                    logger.info(f"扁平化 (层 {current_depth + 1}/{max_depth}): {current_path.name} 只有一个子文件夹 {subfolder.name}，正在合并...")

                    # 创建临时路径
                    temp_path = current_path.parent / f"{current_path.name}_temp_{os.urandom(4).hex()}"

                    # 先将子文件夹移动到临时位置
                    shutil.move(str(subfolder), str(temp_path))

                    # 删除空的原文件夹
                    current_path.rmdir()

                    # 将临时文件夹重命名为原文件夹名
                    shutil.move(str(temp_path), str(current_path))

                    logger.info(f"扁平化完成 (层 {current_depth + 1}): {current_path}")

                    # 继续检查是否还需要扁平化（同一链条继续，深度+1）
                    flatten_single_path(current_path, current_depth + 1)
                    return True
                return False

            except Exception as e:
                logger.error(f"扁平化文件夹失败 {current_path}: {e}")
                return False

        def flatten_recursive(current_path: Path) -> None:
            """
            递归遍历所有文件夹，对每个文件夹尝试扁平化
            每个分支的扁平化深度独立计算
            """
            if not current_path.is_dir():
                return

            try:
                # 首先尝试扁平化当前路径（从深度0开始）
                flatten_single_path(current_path, 0)

                # 然后递归处理所有子文件夹
                items = list(current_path.iterdir())
                for item in items:
                    if item.is_dir():
                        flatten_recursive(item)

            except Exception as e:
                logger.error(f"递归扁平化失败 {current_path}: {e}")

        # 从根目录开始递归扁平化
        flatten_recursive(root_path)

        return str(root_path)

    def plan_directory_cleanup(self, path: str, max_depth: Optional[int] = None) -> dict:
        """
        目录整理干跑（过滤删除后的收尾整理）：
        1. 递归收集「空目录」（无文件且所有子目录同样为空——典型的被过滤后残留壳）；
        2. 在模拟删除这些空目录之后，扫描剩余结构中的「单子目录链」合并变换。

        返回 {empty_dirs: [相对路径，深层在前], operations: [单链合并变换]}。
        """
        root_path = Path(path)
        depth_limit = int(max_depth if max_depth is not None else self.config.rename.flatten_depth)
        empty_relative: list[str] = []

        def walk_empty(current: Path, rel: str) -> bool:
            """后序遍历：目录是否可整删（无文件且所有子目录均可整删）。"""
            try:
                entries = list(current.iterdir())
            except OSError:
                return False
            if not entries:
                if rel:
                    empty_relative.append(rel)
                return True
            deletable = True
            for entry in entries:
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                if entry.is_dir():
                    if not walk_empty(entry, child_rel):
                        deletable = False
                else:
                    deletable = False
            if deletable and rel:
                empty_relative.append(rel)
            return deletable

        walk_empty(root_path, "")
        empty_set = set(empty_relative)

        # 模拟空目录已删除，再扫描剩余结构的单链。
        # 只压「因删除而收敛」的链：parent 的子项中存在将被删除的空目录
        # （说明是过滤删除后只剩一个子目录）才产生合并变换；
        # 原生单链（如 04_CG集/无台词 这类有意义命名的分类目录）保持原样。
        operations: list[dict[str, str]] = []

        def scan(current: Path, depth: int, parent_rel: str) -> None:
            if depth >= depth_limit or not current.is_dir():
                return
            try:
                raw_entries = list(current.iterdir())
            except OSError:
                return

            def rel_of(entry: Path) -> str:
                return f"{parent_rel}/{entry.name}" if parent_rel else entry.name

            kept_entries = [entry for entry in raw_entries if rel_of(entry) not in empty_set]
            deleted_sibling_exists = len(kept_entries) != len(raw_entries)
            kept_subdirs = [entry for entry in kept_entries if entry.is_dir()]
            if len(kept_entries) == 1 and len(kept_subdirs) == 1:
                subfolder = kept_subdirs[0]
                if deleted_sibling_exists:
                    operations.append({
                        "parent_relative_path": parent_rel,
                        "removed_segment": subfolder.name,
                    })
                next_rel = rel_of(subfolder)
                scan(subfolder, depth + 1, next_rel)
                return
            for subfolder in kept_subdirs:
                scan(subfolder, depth + 1, rel_of(subfolder))

        scan(root_path, 0, "")
        empty_relative.sort(key=lambda rel: -rel.count("/"))
        return {"empty_dirs": empty_relative, "operations": operations}

    def apply_directory_cleanup(self, path: str, empty_dirs: list[str], operations: list[dict[str, str]]) -> dict:
        """
        执行目录整理（用户在预审弹窗勾选的子集）：
        1. 删除空目录（深层优先；rmdir 只能删空目录，非空/缺失自动跳过）；
        2. 执行单链合并（深层优先、同名冲突跳过，复用 apply_single_chain_flattens）；
        3. remove_empty_folders 兜底清理。
        """
        root_path = Path(path)
        removed_dirs: list[str] = []
        skipped_dirs: list[dict[str, str]] = []
        for rel in sorted(empty_dirs or [], key=lambda item: -str(item).count("/")):
            rel_clean = str(rel or "").strip("/")
            if not rel_clean:
                continue
            target = root_path.joinpath(*rel_clean.split("/"))
            if not target.is_dir():
                skipped_dirs.append({"path": rel_clean, "reason": "missing"})
                continue
            try:
                target.rmdir()
                removed_dirs.append(rel_clean)
            except OSError as exc:
                skipped_dirs.append({"path": rel_clean, "reason": str(exc)})
        result = self.apply_single_chain_flattens(str(root_path), operations or [])
        logger.info(
            "目录整理完成: 删除空目录 %d 个（跳过 %d），合并单链 %d 条",
            len(removed_dirs), len(skipped_dirs), result.get("applied_count", 0),
        )
        return {
            "removed_empty_dirs": removed_dirs,
            "removed_empty_dir_count": len(removed_dirs),
            "skipped_empty_dirs": skipped_dirs,
            **result,
        }

    def plan_single_chain_flattens(self, path: str, max_depth: Optional[int] = None) -> list[dict[str, str]]:
        """
        干跑扫描：返回目标目录下所有「单子目录链」的合并变换清单（不移动任何文件）。
        变换格式与 _flatten_single_subfolder 的 operation_sink 一致：
        {parent_relative_path, removed_segment}——把 parent 下的 removed_segment
        子目录内容提升到 parent。沿链深入自动产生链式变换（A→B→C 产生两条）。
        """
        root_path = Path(path)
        depth_limit = int(max_depth if max_depth is not None else self.config.rename.flatten_depth)
        operations: list[dict[str, str]] = []

        def scan(current: Path, depth: int, parent_rel: str) -> None:
            if depth >= depth_limit or not current.is_dir():
                return
            try:
                entries = list(current.iterdir())
            except OSError:
                return
            subdirs = [entry for entry in entries if entry.is_dir()]
            if len(entries) == 1 and len(subdirs) == 1:
                subfolder = subdirs[0]
                operations.append({
                    "parent_relative_path": parent_rel,
                    "removed_segment": subfolder.name,
                })
                # 沿链深入：子目录成为新的 parent（继续找链的下一节）
                next_rel = f"{parent_rel}/{subfolder.name}" if parent_rel else subfolder.name
                scan(subfolder, depth + 1, next_rel)
                return
            for subfolder in subdirs:
                next_rel = f"{parent_rel}/{subfolder.name}" if parent_rel else subfolder.name
                scan(subfolder, depth + 1, next_rel)

        scan(root_path, 0, "")
        # 深层优先排序（执行顺序：先合并深层，浅层合并才能一次到位）
        def _depth_key(op: dict[str, str]) -> int:
            parent = str(op.get("parent_relative_path") or "")
            return -(len([part for part in parent.split("/") if part]) + 1)
        operations.sort(key=_depth_key)
        return operations

    def apply_single_chain_flattens(self, path: str, operations: list[dict[str, str]]) -> dict:
        """
        按给定的变换清单执行单链合并（用户在预审弹窗中勾选的子集）。
        深层优先执行；目标冲突（同名已存在）或目录缺失的变换跳过并记录。
        执行完成后顺带清理残留的空目录。
        """
        root_path = Path(path)
        applied: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []

        def _depth_key(op: dict[str, str]) -> int:
            parent = str(op.get("parent_relative_path") or "")
            return -(len([part for part in parent.split("/") if part]) + 1)

        for op in sorted(operations or [], key=_depth_key):
            parent_rel = str(op.get("parent_relative_path") or "").strip("/")
            segment = str(op.get("removed_segment") or "").strip("/")
            if not segment:
                continue
            parent = root_path.joinpath(*parent_rel.split("/")) if parent_rel else root_path
            subfolder = parent / segment
            if not parent.is_dir() or not subfolder.is_dir():
                skipped.append({**op, "reason": "missing"})
                continue
            conflict = [entry for entry in subfolder.iterdir() if (parent / entry.name).exists()]
            if conflict:
                skipped.append({**op, "reason": f"同名冲突: {conflict[0].name}"})
                continue
            for entry in list(subfolder.iterdir()):
                shutil.move(str(entry), str(parent / entry.name))
            subfolder.rmdir()
            applied.append(op)
            logger.info(f"单链扁平化: {parent / segment} -> {parent}")
        try:
            self.remove_empty_folders(str(root_path), remove_root=False)
        except Exception:
            logger.warning("扁平化后空目录清理失败", exc_info=True)
        return {"applied": applied, "skipped": skipped, "applied_count": len(applied)}

    def remove_empty_folders(self, path: str, remove_root: bool = False) -> None:
        """
        递归移除空文件夹
        :param path: 要处理的目录路径
        :param remove_root: 是否移除根目录（如果为空），默认为False保留根目录
        """
        if not os.path.isdir(path):
            return

        # 先递归处理子目录
        try:
            items = list(Path(path).iterdir())
        except Exception as e:
            logger.warning(f"无法读取目录内容 {path}: {e}")
            return

        for item in items:
            if item.is_dir():
                self.remove_empty_folders(str(item), remove_root=True)

        # 重新检查当前目录是否为空
        try:
            items = list(Path(path).iterdir())
            if len(items) == 0:
                if remove_root:
                    try:
                        Path(path).rmdir()
                        logger.info(f"移除空文件夹: {path}")
                    except Exception as e:
                        logger.warning(f"移除空文件夹失败 {path}: {e}")
        except Exception as e:
            logger.warning(f"检查空文件夹失败 {path}: {e}")
    
    def _compile_name(self, metadata: dict, japanese_metadata: Optional[dict] = None) -> str:
        """根据模板编译名称

        Args:
            metadata: 当前语言的元数据
            japanese_metadata: 日语元数据（可选），当启用 use_japanese_metadata 时使用
        """
        template = self.config.rename.template
        logger.info(f"[RENAME] 原始模板: '{template}' (长度: {len(template)})")

        # 确定用于填充模板的数据源
        # rjcode 和 work_name 始终使用当前语言的元数据
        # 其他字段在启用日语元数据时使用日语版本
        use_japanese = self.config.rename.use_japanese_metadata and japanese_metadata

        # 替换变量
        name = template

        # rjcode 和 work_name 始终使用当前语言版本
        rjcode = metadata.get('rjcode', '')
        work_name = metadata.get('work_name', '')

        logger.info(f"[RENAME] 替换前 - rjcode='{rjcode}', work_name='{work_name[:30] if len(work_name) > 30 else work_name}'")

        name = name.replace('{rjcode}', rjcode)
        logger.info(f"[RENAME] 替换rjcode后: '{name}'")

        name = name.replace('{work_name}', work_name)
        logger.info(f"[RENAME] 替换work_name后: '{name}'")

        # maker_id、原社团：日语元数据优先。旧 maker_name 模板继续映射到原社团。
        if use_japanese:
            maker_id = japanese_metadata.get('maker_id', metadata.get('maker_id', ''))
            original_maker_name = normalize_template_maker_name(
                japanese_metadata.get('original_maker_name')
                or japanese_metadata.get('maker_name')
                or metadata.get('original_maker_name')
                or metadata.get('maker_name', '')
            )
            logger.info(f"[RENAME] 使用日语元数据 - original_maker_name='{original_maker_name}'")
        else:
            maker_id = metadata.get('maker_id', '')
            original_maker_name = normalize_template_maker_name(
                metadata.get('original_maker_name')
                or metadata.get('maker_name', '')
            )
        translator_name = normalize_template_maker_name(
            metadata.get('translator_name', '')
        )

        name = name.replace('{maker_id}', maker_id)
        name = name.replace('{maker_name}', original_maker_name)
        name = name.replace('{original_maker_name}', original_maker_name)
        name = name.replace('{translator_name}', translator_name)

        # 日期：日语元数据优先
        if '{release_date}' in name:
            date_str = japanese_metadata.get('release_date', '') if use_japanese else metadata.get('release_date', '')
            if not date_str:
                date_str = metadata.get('release_date', '')
            if date_str:
                try:
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                    name = name.replace('{release_date}', date_obj.strftime(self.config.rename.date_format))
                except:
                    name = name.replace('{release_date}', '')
            else:
                name = name.replace('{release_date}', '')

        # CV列表：日语元数据优先
        if '{cvs}' in name:
            if use_japanese and japanese_metadata.get('cvs'):
                cvs = japanese_metadata.get('cvs', [])
                logger.info(f"[RENAME] 使用日语CV列表: {cvs[:3]}{'...' if len(cvs) > 3 else ''}")
            else:
                cvs = metadata.get('cvs', [])

            if cvs:
                cv_str = self.config.rename.delimiter.join(cvs)
                cv_str = f"{self.config.rename.cv_list_left}{cv_str}{self.config.rename.cv_list_right}"
                name = name.replace('{cvs}', cv_str)
            else:
                name = name.replace('{cvs}', '')

        # 标签列表：日语元数据优先
        if '{tags}' in name:
            if use_japanese and japanese_metadata.get('tags'):
                tags = japanese_metadata.get('tags', [])
                logger.info(f"[RENAME] 使用日语标签列表: {tags[:3]}{'...' if len(tags) > 3 else ''}")
            else:
                tags = metadata.get('tags', [])

            if tags:
                # 限制标签数量
                tags = tags[:self.config.rename.tags_max_number]
                tag_str = self.config.rename.delimiter.join(tags)
                name = name.replace('{tags}', tag_str)
            else:
                name = name.replace('{tags}', '')

        # 移除work_name中的方括号内容
        if self.config.rename.exclude_square_brackets:
            name = re.sub(r'【.*?】', '', name)

        return name.strip()
    
    def _sanitize_filename(self, filename: str) -> str:
        """清理文件名中的非法字符"""
        # Windows保留字符: < > : " / \ | ? *
        reserved_chars = r'[<>"/\\|?*]'
        
        if self.config.rename.illegal_char_to_full_width:
            # 转换为全角字符
            replace_map = {
                '<': '＜', '>': '＞', ':': '：', '"': '＂',
                '/': '／', '\\': '＼', '|': '｜', '?': '？', '*': '＊'
            }
            for char, replacement in replace_map.items():
                filename = filename.replace(char, replacement)
        else:
            # 直接移除
            filename = re.sub(reserved_chars, '', filename)
        
        # 移除首尾空格和点
        filename = filename.strip(' .')
        
        # 限制长度
        if len(filename) > 200:
            filename = filename[:200]
        
        return filename
