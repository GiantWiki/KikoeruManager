# 延后归档遗漏与伪装 ZIP 分卷误拆修复实施计划

> 面向后续执行者（DeepSeek）。本文是实施方案，不是本次已经执行的代码改动。
> 仓库：`D:\program\prekikoeru\kikoerumanager-sync`
> 当前基线：`8bec63e`，v2.6.58，工作区干净。
> 诊断日期：2026-10-24。
>
> **硬性约束**
>
> - 不要操作远程 Docker 文件，不要删除输入目录残留，不要直接修改线上数据库。
> - 不要自动重命名伪装分卷；伪装分卷必须继续走现有“问题作品确认/重命名”流程。
> - 不要把残缺的非首卷直接归档成完整压缩包。
> - 不推 `main`，不打 tag，不触发 EXE release。
> - 修改任何符号前先跑 GitNexus `impact`；提交前跑 `detect_changes`。GitNexus 当前 FTS 组件不可用，关键词查询可能为空；已知符号优先用 `context`，必要时再用文本检索补充。

---

## 1. 已确认事实

### 1.1 远程输入库存残留

只读盘点 `/input` 后共有 6 组、8 个残留分卷：

| 组 | 当前残留 | 已确认状态 | 处理原则 |
|---|---|---|---|
| `RJ01657792 RJ01657793.7z.*` | `.7z.001` + `.7z.002` | 两卷齐全；归档 job 失败 | 修复后整组恢复归档，不重新解压 |
| `RJ01615017 RJ01615018.7z.*` | 仅 `.7z.002` | 历史日志仅见首卷三次解压失败；首卷去向无法确认 | 补齐首卷后再处理；禁止单独归档尾卷 |
| `RJ01670634 RJ01670635.7z.*` | 仅 `.7z.002` | 当时因关联 RJ 重复进入人工冲突流程 | 先找回首卷或确认库存完整；禁止盲目归档 |
| `RJ01667842.part*.rar` | 仅 `.part2.rar` | 首卷缺失 | 补齐首卷后再处理 |
| `RJ01604180.part*.rar` | 仅 `.part3.rar` | 前两卷缺失 | 补齐 part1、part2 后再处理 |
| `01701267(southplus).* ` | 当前为 `.z01` + `.z02` | 用户已手工把原始 `.zi` 改为 `.z02` | 按伪装 ZIP 分卷组处理，不得再拆成两个任务 |

远程延后归档接口共返回 331 个 job：326 个 completed、5 个 failed。与当前残留直接相关的失败 job 只有：

- `b4cae342-150c-43e1-9853-eb962c648b96`
- RJ：`RJ01657792`
- attempt_count：5
- last_error：`归档源文件已变化或不存在: ...RJ01657792 RJ01657793.7z.002`

### 1.2 RJ01657792/93 归档缺陷证据链

历史日志：

- `2026-08-25 10:14:23`：迟到的 `.7z.002` 被单独入队归档。
- 之后多次执行失败：`归档源文件已变化或不存在`。
- 现有 job manifest 已经同时包含 `.002` 和 `.001`，但两个 target 都仍是 `pending`。

代码顺序：

1. `backend/app/core/deferred_archive_service.py:644` `_execute_claimed_job`
2. 先调用 `deferred_archive_service.py:828` `_refresh_job_manifest_sync`，把入队后才出现的 `.001` 补进 manifest。
3. 再调用 `deferred_archive_service.py:963` `_detect_stale_orphan_sync`。
4. `_detect_stale_orphan_sync` 看到 `.001` 已在 manifest，误判这不是孤儿尾卷任务，跳过取消。
5. `_refresh_job_manifest_sync` 只追加新成员，不刷新已有 `.002` 的 size/mtime/inode。
6. `deferred_archive_service.py:1215` `_move_member_sync` 做冻结身份校验时发现 `.002` 已变化。
7. `deferred_archive_service.py:1445` `_fail_job_sync` 每次递增 attempt；5 次后 job 进入终态 `failed`，没有自动恢复。

根因不是“拷贝失败”，而是：**孤儿尾卷任务在执行前被刷新成整组任务，导致孤儿重验失效；同时已有成员身份没有刷新，源变化被当成不可恢复错误。**

### 1.3 RJ01701267 误拆证据链

用户确认：远程现在的 `.z02` 是手工改名，原始下载文件名是：

- `01701267(southplus).z01`，4,294,967,295 bytes
- `01701267(southplus).zi`，1,687,968,016 bytes

17:07:38 系统创建了两个独立任务：

- `.zi` → `296f4380-7782-407b-bfd3-7a0fcb15826a`
  - polyglot 在偏移 19,789,926 识别为 ZIP。
  - `7zz l` 报 `Cannot open the file as archive`。
  - 最终错误被归为 `archive_corrupt`。
- `.z01` → `9c33eef7-8952-4d3f-868c-47e8680abdaa`
  - polyglot 在偏移 4 识别为 ZIP。
  - 解压时报 `Missing volume : 01701267(southplus).zip`。
  - 最终归为 `volume_incomplete`。

代码原因：

- `backend/app/core/file_processor.py:844` `_build_zip_volume_set` 强制要求同名 `.zip` 存在。
- `file_processor.py:469` `is_archive` 在没有 `.zip` 时允许 `.z01` 继续走魔数/polyglot 检测。
- `.zi` 不在已知压缩后缀里，但会被 polyglot 检测为内嵌 ZIP。
- 因此扫描入口没有先聚合成一组，直接提交了两个任务。
- 现有的伪装分卷探测发生在 `backend/app/core/extract_service.py:866` `_detect_disguised_volume_set`，时机太晚，只能补救单个已进入解压阶段的任务。

---

## 2. 总体设计原则

1. **扫描层先聚合，解压层再确认识别**。
   - FileProcessor 只负责把高置信度同组分卷聚成一个任务。
   - ExtractService 仍负责伪装分卷的魔数、连续序号、尺寸闸门将误判挡在人工确认前。
2. **不自动改源文件名**。
   - 对 `.z01 + .zi`、`.z01 + .z02` 这类非标准组，先生成一个任务和伪装分卷冲突；
   - 用户在问题作品页确认后才改成 `.zip.001/.zip.002`。
3. **归档必须按整组收口**。
   - 同一组只允许一个归档 job；
   - 未完成下载造成的源变化是可重试状态，不是终态失败；
   - 已发布过成员的旧 job 不允许静默换成另一组源文件。
4. **残缺尾卷永不自动当作完整压缩包**。
   - 孤立的 `.002/.part2/.part3` 只能进入诊断/人工处理；
   - 不得为其创建 `ProcessedArchive` 完成记录。

---

## 3. 修复一：延后归档孤儿尾卷与源变化恢复

### 3.1 修改 `_execute_claimed_job`

文件：`backend/app/core/deferred_archive_service.py`

当前顺序在约 644–721 行：

```python
refreshed = _refresh_job_manifest_sync(...)
if refreshed: ...
if _detect_stale_orphan_sync(claim): cancel
for source in manifest: _move_member(...)
```

改成：

```python
# 1. 先用“入队时的原始 manifest”判断孤儿任务是否仍有首卷。
if await asyncio.to_thread(self._detect_stale_orphan_sync, claim):
    raise _ArchiveCancelled()

# 2. 只有确认不是误孤儿任务后，才允许补全后续分卷。
refreshed = await asyncio.to_thread(self._refresh_job_manifest_sync, job_id, epoch)
```

同时在 `enqueue_sync` 给孤儿任务留下持久标记，避免只依赖执行顺序：

- 不需要新增数据库列；
- 在 `source_manifest` 的 JSON item 中增加可选字段即可，例如：

```python
item["late_volume_orphan"] = True
```

判定方式：

- 输入源是非首卷；
- 当前 manifest 不包含对应首卷；
- 同组当前只识别到该尾卷，或 `_build_source_manifest` 返回的 `group_base` 为空。

`_detect_stale_orphan_sync` 接受原始 manifest 快照；即使有 late marker，也只在“首卷仍存在”时取消。

### 3.2 新增 `_ArchiveSourceChanged`

在现有 `_YieldToForeground`、`_ArchiveCancelled` 附近新增：

```python
class _ArchiveSourceChanged(RuntimeError):
    """归档源仍在下载或被同路径替换，等待稳定后重试。"""
```

在 `_move_member_sync` 中，以下检查不要再抛普通 `RuntimeError`：

- 开始前 `_matches_source_identity(...)` 为 false；
- 复制后源身份变化；
- 发布前源身份变化。

统一抛 `_ArchiveSourceChanged(source_path)`。

> 注意：目标已存在但 checksum/内容不一致，仍属于真正失败，继续抛 `RuntimeError`，不能混入 source-changed 分支。

### 3.3 源变化不消耗终态重试次数

新增 `_defer_source_changed_sync(job_id, epoch, error)`：

语义：

- `status = "waiting_retry"`
- `available_at = now + archive_retry_delay_seconds`
- `attempt_count` **不递增**
- `last_error` 保留源变化说明
- 清空 lease owner / lease_until
- `completed_at = None`

在 `_execute_claimed_job` 中，把该异常捕获放在 generic `Exception` 之前：

```python
except _ArchiveSourceChanged as exc:
    deferred = await asyncio.to_thread(
        self._defer_source_changed_sync, job_id, epoch, str(exc)
    )
    ...
```

`_fail_job_sync` 保持原逻辑，只处理真实失败。

### 3.4 刷新已有成员的身份

`_refresh_job_manifest_sync` 当前只追加新分卷。增加一段“仅 pending 成员可刷新”的逻辑：

允许刷新条件：

- 所有相关 target 都没有 `state == "completed"`；
- 没有已记录 sha256 的已发布成员；
- 源路径仍存在；
- 不存在同名 `.aria2` sidecar。

满足时，对已有 `source_manifest` 中同路径 pending 成员重新 `os.stat` 并更新：

- `size`
- `inode`
- `device`
- `ctime_ns`
- `mtime_ns`

如果已有任一成员已发布，则**不刷新旧成员身份**；源变化必须保留为人工处理的失败，避免把两次下载的不同版本混进同一个归档。

### 3.5 人工 retry 先重建 manifest

`retry_failed_sync` 当前只做状态重置。修改为：

1. 读取 failed job；
2. 如果没有任何已发布成员：
   - 基于首个仍存在的 source 重新执行 `_build_source_manifest`；
   - 补齐整组并刷新 pending 源身份；
   - 同步重建/保留 target manifest；
3. 再设置 `status="pending"`、`attempt_count=0`。

这样旧 job `b4cae342-...` 修复后通过现有接口恢复即可：

```http
POST /api/deferred-archive-jobs/b4cae342-150c-43e1-9853-eb962c648b96/retry
```

不需要手工移动文件。

---

## 4. 修复二：扫描层聚合伪装 ZIP 分卷

### 4.1 支持的输入形态

必须同时支持：

| 形态 | 说明 | 入口 |
|---|---|---|
| `X.zip + X.z01` | 标准 WinRAR ZIP 分卷 | `.zip` 主卷 |
| `X.zip + X.002` | 已回滚/非标准数字分卷 | `.zip` 主卷 |
| `X.z01 + X.z02` | 无 `.zip` 主卷的伪装/截断分卷 | `.z01` |
| `X.z01 + X.zi` | RJ01701267 的原始命名 | `.z01` |
| `X.z01 + X.z02 + X.zi` | 多 zNN 后接截断主卷 | `.z01` |
| `X.z77` 单体伪装压缩包 | 历史兼容场景 | 保持原行为 |

### 4.2 修改 FileProcessor 的分卷识别

文件：`backend/app/core/file_processor.py`

建议在 `_build_zip_volume_set` 中扩展，不要改变标准 `.zip + .zNN` 分支：

1. 先保留现有逻辑：
   - 如果 `X.zip` 存在，仍返回 `entry_path = X.zip`。
2. 新增无标准主卷的候选分支：

```python
z_volumes = collect(f"{base}.zNN")
zi_path = existing(f"{base}.zi")

if len(z_volumes) >= 2:
    ordered = sort(z_volumes)
    if zi_path exists:
        ordered.append(zi_path)
    return VolumeSet(base, ordered, "zip_disguised_volumes", entry_path=ordered[0])

if len(z_volumes) == 1 and basename(z_volumes[0]).endswith(".z01") and zi_path exists:
    return VolumeSet(base, [z_volumes[0], zi_path], "zip_truncated_main", entry_path=z_volumes[0])
```

要求：

- `.zNN` 序号必须连续，从 01 开始；
- `.zi` 只能作为最后一卷；
- 不允许 `.zi` 自己成为 entry；
- 没有 `.z01` 时，单独 `.zi` 不进入该逻辑，保持 polyglot 单体兼容；
- 没有相邻分卷时，单独 `.z77` 不进入该逻辑，保持历史单体伪装兼容。

### 4.3 修改 `is_archive`

文件：`backend/app/core/file_processor.py`

当前 `_has_zip_volume_main` 只识别 `X.zip`。新增一个候选判断，而不是改变原函数语义：

```python
def _has_zip_volume_entry(file_path):
    base = strip .zNN / .zi
    return exists(base + ".z01") or exists(base + ".zip")
```

规则：

- `X.z02/X.z03...` 如果同目录有 `X.z01`，返回 False，等待 `.z01` 创建任务；
- `X.zi` 如果同目录有 `X.z01`，返回 False；
- `X.z01` 保持可检测；
- 没有 `X.z01` 的单独 `.zi` 保持原有 polyglot 单体检测；
- 没有组关系的单独 `.z77` 保持原有魔数检测。

### 4.4 修改 `_handle_potential_volume`

文件：`backend/app/core/file_processor.py`

让存在组关系的 `.zNN/.zi` 进入 10 秒等待窗口：

- 只有出现 `.z01`、`.zip` 或 `.zi` 组候选时才等待；
- 没有组关系的 `.z77` 不因该改动被永久跳过；
- 等待后 `detect_volume_set` 命中则 `_process_volume_set` 标记整组并等待所有分卷稳定；
- 非首卷仍未成组时返回 None，不创建独立任务；
- `.z01` 单卷在等待后仍未成组，可以继续走魔数/polyglot，兼容单体伪装包。

### 4.5 修改 ExtractService 的伪装分卷扫描

文件：`backend/app/core/extract_service.py`

`_scan_disguised_volume_siblings` 当前只识别“同 prefix + 数字 + 同 suffix”，所以：

- `.z01` 能匹配 `.z02`；
- 不能匹配 `.zi`。

在通用策略前增加专门的 ZIP 截断主卷策略：

```python
# target: base.z01
# candidates: base.zNN ... + optional base.zi
# zNN indices must be 1..N contiguous
# .zi index = N + 1
```

安全闸门继续在 `_detect_disguised_volume_set`：

- target 必须是 `.z01`；
- 首卷魔数/polyglot 必须识别为 zip；
- 至少两个候选；
- zNN 序号连续；
- 每卷 ≥ 1KB；
- 主体卷尺寸差异 < 5%，最后一卷允许较小；
- `.zi` 只能排最后。

`suggested_renames` 继续调用 `_build_standard_volume_name`：

- 原始 `.z01 + .zi` → `X.zip.001`、`X.zip.002`
- 手工改名后的 `.z01 + .z02` → 同样建议 `X.zip.001`、`X.zip.002`

在 `disguised_volume_set` metadata 中保留原始文件名，避免后续把用户手工 `.z02` 误认为下载源标准命名。

---

## 5. 冲突处理与归档一致性

### 5.1 不再维护第二套分卷规则

`backend/app/api/routes.py:8050` `_collect_split_archive_siblings` 与 `archive_volume_utils.detect_archive_volume_group` 是两套逻辑，支持格式会逐渐分叉。

建议做法：

1. 优先让 `_collect_split_archive_siblings` 调 `detect_archive_volume_group(main_path)`；
2. 命中时返回 `group.volumes`；
3. 未命中时保留现有 fallback；
4. `archive_volume_utils` 同步支持 `.zNN + .zi` / 无标准主卷的候选组，但排序必须保证 `.zi` 最后。

如果担心一次改动过大，可以分两个提交：

- 第一提交修 FileProcessor + ExtractService；
- 第二提交统一 conflict sibling 收集逻辑。

### 5.2 SKIP/重命名必须整组生效

至少保证：

- SKIP 删除 `X.z01 + X.zi` 时两文件都列出；
- SKIP 删除 `X.z01 + X.z02` 时两文件都列出；
- 伪装分卷确认重命名后，后续归档 manifest 是 `.zip.001/.zip.002` 两个成员；
- 不允许只归档首卷。

---

## 6. 测试计划

### 6.1 新增 FileProcessor 测试

建议新建：`backend/tests/test_disguised_zip_volume_grouping.py`

用例：

1. `test_z01_zi_creates_single_volume_task`
   - 创建 `RJ01701267(southplus).z01` 和 `.zi`；
   - mock `TaskEngine.submit`；
   - 调用 `process_directory`；
   - 断言只提交一次；
   - 提交路径是 `.z01`；
   - 两文件都进入本次 `processed_volumes` 等价保护。

2. `test_z01_z02_without_zip_creates_single_volume_task`
   - 覆盖用户手工改名后的现状；
   - 只提交 `.z01`；
   - `.z02` 不单独提交。

3. `test_standard_zip_z01_keeps_zip_as_entry`
   - `X.zip + X.z01`；
   - entry 必须是 `X.zip`，保持原行为。

4. `test_single_zi_without_z01_keeps_polyglot_single_file_behavior`
   - 单独 `.zi` 不被分卷规则吞掉。

5. `test_single_z77_without_siblings_keeps_legacy_behavior`
   - 防止回归现有“单体压缩包伪装 z77”。

### 6.2 新增 ExtractService 测试

可放入现有 `test_conflict_resolution_volume_rename.py` 或新测试文件：

1. `.z01 + .zi` 命中 `zip` disguised set，建议 `.zip.001/.zip.002`。
2. `.z01 + .z02` 命中 `zip` disguised set，建议同上。
3. `.z01 + .z03` 序号不连续，不命中。
4. `.zi` 在中间或存在多个 `.zi`，不命中。
5. 只有 `.z01`，不命中 disguised set。
6. 非 zip 魔数的同名候选不命中。

### 6.3 延后归档测试

扩展 `backend/tests/test_deferred_archive_service.py`：

1. `test_stale_orphan_is_checked_before_manifest_refresh`
   - 入队 manifest 只有 `.7z.002`；
   - 执行前 `.7z.001` 已存在；
   - 断言 job cancelled，而不是把 `.001` 补入后继续搬运。

2. `test_source_change_defers_without_consuming_retry_count`
   - manifest 固定后改变源文件 size/mtime/inode；
   - 断言 `waiting_retry`；
   - `attempt_count` 不变。

3. `test_refresh_updates_pending_identity_only_when_nothing_published`
   - 无 completed member：允许刷新；
   - 有 completed member：拒绝刷新并报需人工处理。

4. `test_retry_failed_rebuilds_full_manifest`
   - failed job 只含尾卷；
   - retry 后 manifest 包含整组；
   - status pending，attempt_count 归零。

### 6.4 冲突 SKIP 测试

扩展 `test_conflict_resolution_volume_rename.py`：

- SKIP `X.z01 + X.zi` 删除两项，保留无关文件；
- SKIP `X.z01 + X.z02` 删除两项；
- 非连续序号不删除；
- 单独 `.zi` 不被误删。

### 6.5 测试命令

在仓库根目录执行：

```bash
cd backend
python -m pytest --noconftest tests/test_disguised_zip_volume_grouping.py -q
python -m pytest --noconftest tests/test_deferred_archive_service.py -q
python -m pytest --noconftest tests/test_archive_volume_utils.py -q
python -m pytest --noconftest tests/test_conflict_resolution_volume_rename.py -q
python -m pytest --noconftest tests/test_embedded_7z.py -q
```

注意：

- 必须使用 `python -m pytest --noconftest`，否则 conftest 导入期会连数据库。
- 已知存量失败基线不要混进本次结论：`test_linked_subtitle_import_service` / `test_extract_service` 4 例；`test_processed_archives_scan` 4 ERROR 缺 `db_session` fixture。

---

## 7. 部署后存量残留处置手册

**必须先部署修复，再处理存量。顺序不能反。**

### 7.1 RJ01657792/93

当前两卷齐全，修复后：

1. 确认无 `.aria2`，文件 size/mtime 稳定；
2. 调用现有 retry：
   `POST /api/deferred-archive-jobs/b4cae342-150c-43e1-9853-eb962c648b96/retry`；
3. 等系统空闲归档；
4. 验收：
   - job completed；
   - target manifest 有 `.001`、`.002` 两项且 checksum 非空；
   - `/input` 对应目录清空；
   - `/processed` 同时存在两卷；
   - 不重新触发解压任务。

### 7.2 RJ01701267

用户已手工把原始 `.zi` 改成 `.z02`。修复后：

1. 先取消/关闭旧的 `.zi` 问题项；它引用的路径已经不存在，不要再对它 retry。
2. 重新扫描所在目录：
   `/input/百度网盘_20261024_165743`。
3. 应只创建一个 `.z01` 任务。
4. 解压阶段应进入“疑似 zip 伪装多卷压缩包”人工确认，而不是：
   - `.zi Cannot open the file as archive`
   - `.z01 Missing volume .zip`
5. 确认建议重命名为：
   - `01701267(southplus).zip.001`
   - `01701267(southplus).zip.002`
6. 用密码 `southplus` 重试。
7. 成功后归档 manifest 必须包含两卷。

### 7.3 残缺旧组

以下组**不要直接归档**：

- `RJ01615017/18` 仅 `.002`
- `RJ01670634/35` 仅 `.002`
- `RJ01667842` 仅 `.part2`
- `RJ01604180` 仅 `.part3`

处理顺序：

1. 先确认 `/processed` 或下载源是否还能找齐缺失首卷；
2. 若找齐：放回同目录，重新按整组扫描/重试；
3. 若找不到：重新下载整组；
4. 只有确认库存中已有完整作品且这些尾卷明确是废弃残片时，才由用户手工删除；
5. 任何自动化逻辑不得把 lone tail 标记为 completed。

---

## 8. 验收标准

### 8.1 归档修复

- RJ01657792/93 的旧 failed job retry 后 completed；
- 两个分卷都发布到 `/processed`；
- 源目录不再残留任一卷；
- 源仍变化时 job 进入 `waiting_retry`，attempt_count 不增长；
- 孤儿尾卷在首卷仍存在时于 manifest 刷新前取消。

### 8.2 伪装 ZIP 分卷

- 原始 `.z01 + .zi` 只创建一个任务；
- 手工改名后的 `.z01 + .z02` 只创建一个任务；
- `.zi` 不再单独产生 `archive_corrupt`；
- `.z01` 不再单独产生 `Missing volume .zip`；
- 人工确认后统一改为 `.zip.001/.zip.002`；
- 解压成功后两卷一起归档。

### 8.3 不回归

- 标准 `.zip + .z01` 仍由 `.zip` 主卷处理；
- 单独 `.zi` / 单独 `.z77` 的 polyglot 单体兼容不被吞掉；
- 非连续序号不误识别；
- 已发布过成员的归档 job 不允许静默换源。

---

## 9. 回滚方案

1. 代码按功能拆分提交：
   - 提交 A：延后归档孤儿判定与源变化重试；
   - 提交 B：伪装 ZIP 分卷扫描聚合；
   - 提交 C：冲突处理统一分卷收集（若单独做）。
2. 每个提交保留独立 revert 能力。
3. 若线上伪装分卷误识别增多，可只回滚提交 B，保留归档修复 A。
4. 若归档恢复异常，回滚提交 A；不要在线上手工移动残留文件来掩盖问题。

---

## 10. DeepSeek 执行顺序

1. 读取本文和当前代码基线，先跑 GitNexus `context` 核对所有锚点仍存在。
2. 对以下符号分别跑 `impact`：
   - `_execute_claimed_job`
   - `_refresh_job_manifest_sync`
   - `_detect_stale_orphan_sync`
   - `_move_member_sync`
   - `retry_failed_sync`
   - `_build_zip_volume_set`
   - `is_archive`
   - `_handle_potential_volume`
   - `_scan_disguised_volume_siblings`
   - `_collect_split_archive_siblings`
3. 先写失败测试，再改实现。
4. 先完成归档修复及测试，再完成伪装 ZIP 聚合及测试。
5. 跑第 6.5 节测试命令。
6. 跑 `detect_changes`；如返回 `partial` 或 `truncated`，必须重跑到干净结果。
7. 只提交本地 commit，不推 main，不打 tag，不触发发布。
8. 把测试结果、变更文件清单、风险点返回给用户审核。
